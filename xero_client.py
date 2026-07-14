"""Xero client with safe, rotating-refresh-token handling and multi-profile support.

The one failure mode that permanently kills a Xero connection: the refresh token
rotates on every use, and if the NEW token is not persisted before anything else
happens, a crash leaves you holding a dead token. So refresh() writes the store
atomically (temp file + fsync + os.replace) the instant it has the new token,
before any API call runs.

Token store layout (tokens.json), version 2:

    {
      "version": 2,
      "active": "demo",
      "profiles": {
        "demo": { access_token, refresh_token, expires_at, scope,
                  connections: [...], tenant_id, tenant_name },
        "real": { ... }
      }
    }

Each profile is an INDEPENDENT authorization with its own refresh token, so
refreshing one never affects another. A single profile's authorization can also
reach several organisations (see `connections`); `tenant_id` picks the active one
and `switch_tenant()` changes it without re-authorizing.

Legacy flat stores (a single token set at the top level, no "profiles" key) are
migrated in place on load: the old token set becomes one profile, named "demo" if
its tenant looks like the Demo Company, otherwise "default".
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


# ---------- token store helpers (module-level, shared by client + CLI) ----------


def _atomic_write_json(path, data):
    """Write JSON atomically. A crash never leaves a torn/empty file."""
    path = Path(path)
    payload = json.dumps(data, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tokens.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)  # atomic rename on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _migrate_store(raw):
    """Return a version-2 profile store. Wraps a legacy flat store into one profile.

    Returns (store, migrated) where `migrated` is True if a legacy store was upgraded.
    """
    if isinstance(raw, dict) and "profiles" in raw:
        raw.setdefault("version", 2)
        raw.setdefault("active", next(iter(raw["profiles"]), None))
        return raw, False
    # Legacy flat store: one token set at the top level.
    name = "demo" if "demo" in (raw.get("tenant_name") or "").lower() else "default"
    store = {"version": 2, "active": name, "profiles": {name: raw}}
    return store, True


def load_store(store_path="tokens.json"):
    """Load and migrate the token store. Persists the migration if one happened."""
    store_path = Path(store_path)
    if not store_path.exists():
        raise XeroError(
            f"Token store {store_path} not found. Run authorize.py first."
        )
    with open(store_path) as f:
        raw = json.load(f)
    store, migrated = _migrate_store(raw)
    if migrated:
        _atomic_write_json(store_path, store)
    return store


def save_store(store, store_path="tokens.json"):
    _atomic_write_json(store_path, store)


def list_profiles(store):
    """Return [(name, tenant_name, is_active), ...] for display."""
    active = store.get("active")
    out = []
    for name, prof in store.get("profiles", {}).items():
        out.append((name, prof.get("tenant_name"), name == active))
    return out


class XeroClient:
    def __init__(self, env_path=".env", store_path="tokens.json", profile=None):
        load_dotenv(env_path)
        self.client_id = os.environ.get("XERO_CLIENT_ID")
        self.client_secret = os.environ.get("XERO_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise XeroError("XERO_CLIENT_ID / XERO_CLIENT_SECRET missing from .env")
        self.store_path = Path(store_path)
        self.store = load_store(self.store_path)

        profiles = self.store.get("profiles", {})
        if not profiles:
            raise XeroError("No profiles in token store. Run authorize.py first.")
        self.profile = profile or self.store.get("active") or next(iter(profiles))
        if self.profile not in profiles:
            available = ", ".join(profiles) or "(none)"
            raise XeroError(
                f"Profile '{self.profile}' not found. Available: {available}. "
                "Run authorize.py --profile <name> to add it."
            )

    # ---------- active profile ----------

    @property
    def prof(self):
        """The active profile's token dict (mutated in place, persisted on save)."""
        return self.store["profiles"][self.profile]

    def _save_store(self):
        _atomic_write_json(self.store_path, self.store)

    def switch_tenant(self, tenant):
        """Point this profile at another organisation it is already connected to.

        `tenant` matches a tenantId exactly or a tenantName (case-insensitive).
        No re-authorization needed: the tenants live in this profile's `connections`.
        """
        conns = self.prof.get("connections", [])
        match = None
        for c in conns:
            if c["tenantId"] == tenant or (c.get("tenantName") or "").lower() == tenant.lower():
                match = c
                break
        if not match:
            names = ", ".join(f"{c.get('tenantName')}" for c in conns) or "(none)"
            raise XeroError(
                f"No connected tenant matches '{tenant}' in profile '{self.profile}'. "
                f"Connected: {names}."
            )
        self.prof["tenant_id"] = match["tenantId"]
        self.prof["tenant_name"] = match.get("tenantName")
        self._save_store()
        return match

    # ---------- auth ----------

    def _basic_auth(self):
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def refresh(self):
        """Refresh the active profile's access token and PERSIST the rotated refresh
        token immediately.

        Order matters: get new tokens -> write them to disk -> only then return.
        Nothing (no API call) happens between receiving and persisting the new
        refresh token.
        """
        rt = self.prof.get("refresh_token")
        if not rt:
            raise XeroError(
                f"No refresh_token in profile '{self.profile}'. Run authorize.py again."
            )

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
        self.prof["access_token"] = tok["access_token"]
        self.prof["refresh_token"] = tok["refresh_token"]  # rotated value
        self.prof["expires_at"] = time.time() + tok["expires_in"] - EXPIRY_BUFFER
        if tok.get("scope"):
            self.prof["scope"] = tok["scope"]
        self._save_store()
        return self.prof["access_token"]

    def ensure_token(self):
        """Return a valid access token, refreshing (and persisting) only if needed."""
        exp = self.prof.get("expires_at", 0)
        if not self.prof.get("access_token") or time.time() >= exp:
            return self.refresh()
        return self.prof["access_token"]

    # ---------- API ----------

    def get(self, path, params=None, accept="application/json"):
        """GET a Xero endpoint. `path` may be a full URL or a path under api.xro/2.0.

        Handles: 401 -> one refresh + retry; 429 -> respect Retry-After, retry once.
        Per-tenant limits: 60 calls/min, 5,000/day.
        """
        token = self.ensure_token()
        tenant = self.prof.get("tenant_id")
        if not tenant:
            raise XeroError("No tenant_id in profile. Run authorize.py first.")
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

    def post(self, path, json_body):
        """POST/create against a Xero endpoint. Same 401/429 handling as get()."""
        token = self.ensure_token()
        tenant = self.prof.get("tenant_id")
        if not tenant:
            raise XeroError("No tenant_id in profile. Run authorize.py first.")
        url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"

        attempts = 0
        while True:
            attempts += 1
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Xero-tenant-id": tenant,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=json_body,
                timeout=30,
            )
            if resp.status_code == 401 and attempts == 1:
                token = self.refresh()
                continue
            if resp.status_code == 429 and attempts <= 2:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                time.sleep(retry_after + 1)
                continue
            if resp.status_code >= 400:
                raise XeroError(f"POST {url} failed ({resp.status_code}): {resp.text}")
            return resp.json()
