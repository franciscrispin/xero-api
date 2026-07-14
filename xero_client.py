"""Xero client with safe, rotating-refresh-token handling.

The one failure mode that permanently kills a Xero connection: the refresh token
rotates on every use, and if the NEW token is not persisted before anything else
happens, a crash leaves you holding a dead token. So refresh() writes the store
atomically (temp file + fsync + os.replace) the instant it has the new token,
before any API call runs.
"""

import base64
import json
import os
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

# Refresh proactively if the access token expires within this many seconds.
EXPIRY_BUFFER = 120


class XeroError(Exception):
    pass


class XeroClient:
    def __init__(self, env_path=".env", store_path="tokens.json"):
        load_dotenv(env_path)
        self.client_id = os.environ.get("XERO_CLIENT_ID")
        self.client_secret = os.environ.get("XERO_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise XeroError("XERO_CLIENT_ID / XERO_CLIENT_SECRET missing from .env")
        self.store_path = Path(store_path)
        self.store = self._load_store()

    # ---------- token store (atomic) ----------

    def _load_store(self):
        if not self.store_path.exists():
            raise XeroError(
                f"Token store {self.store_path} not found. Run authorize.py first."
            )
        with open(self.store_path) as f:
            return json.load(f)

    def _save_store(self):
        """Write the token store atomically. A crash never leaves a torn/empty file."""
        data = json.dumps(self.store, indent=2)
        directory = self.store_path.parent
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tokens.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.store_path)  # atomic rename on the same filesystem
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------- auth ----------

    def _basic_auth(self):
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def refresh(self):
        """Refresh the access token and PERSIST the rotated refresh token immediately.

        Order matters: get new tokens -> write them to disk -> only then return.
        Nothing (no API call) happens between receiving and persisting the new
        refresh token.
        """
        rt = self.store.get("refresh_token")
        if not rt:
            raise XeroError("No refresh_token in store. Run authorize.py again.")

        resp = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": rt},
            timeout=30,
        )
        if resp.status_code != 200:
            # Do NOT touch the store on failure: the old refresh token may still be
            # valid (e.g. transient network/5xx). Only invalid_grant means it is dead.
            raise XeroError(
                f"Refresh failed ({resp.status_code}): {resp.text}\n"
                "If this is 'invalid_grant', the refresh token is dead "
                "(unused >60 days, revoked, or a rotation was lost). Run authorize.py again."
            )

        tok = resp.json()
        # Persist BEFORE anything else. This is the whole point.
        self.store["access_token"] = tok["access_token"]
        self.store["refresh_token"] = tok["refresh_token"]  # rotated value
        self.store["expires_at"] = time.time() + tok["expires_in"] - EXPIRY_BUFFER
        if tok.get("scope"):
            self.store["scope"] = tok["scope"]
        self._save_store()
        return self.store["access_token"]

    def ensure_token(self):
        """Return a valid access token, refreshing (and persisting) only if needed."""
        exp = self.store.get("expires_at", 0)
        if not self.store.get("access_token") or time.time() >= exp:
            return self.refresh()
        return self.store["access_token"]

    # ---------- API ----------

    def get(self, path, params=None, accept="application/json"):
        """GET a Xero endpoint. `path` may be a full URL or a path under api.xro/2.0.

        Handles: 401 -> one refresh + retry; 429 -> respect Retry-After, retry once.
        Per-tenant limits: 60 calls/min, 5,000/day.
        """
        token = self.ensure_token()
        tenant = self.store.get("tenant_id")
        if not tenant:
            raise XeroError("No tenant_id in store. Run authorize.py first.")
        url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"

        attempts = 0
        while True:
            attempts += 1
            resp = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Xero-tenant-id": tenant,
                    "Accept": accept,
                },
                params=params,
                timeout=30,
            )
            if resp.status_code == 401 and attempts == 1:
                token = self.refresh()  # token expired mid-flight; refresh + retry once
                continue
            if resp.status_code == 429 and attempts <= 2:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                time.sleep(retry_after + 1)
                continue
            if resp.status_code >= 400:
                raise XeroError(f"GET {url} failed ({resp.status_code}): {resp.text}")
            return resp.json() if "json" in accept else resp.text
