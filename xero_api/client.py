"""Xero client with safe, rotating-refresh-token handling and multi-profile support.

The one failure mode that permanently kills a Xero connection: the refresh token
rotates on every use, and if the NEW token is not persisted before anything else
happens, a crash leaves you holding a dead token. So refresh() writes the store
atomically (temp file + fsync + os.replace) the instant it has the new token,
before any API call runs.

The store lives in a user config directory, not the working directory --
see `config_dir()` for how that path is resolved.

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

Several processes share one store (the CLI, scripts, long-running services), and
Xero rotates the refresh token on every refresh. So every write to the store is a
read-modify-write under an exclusive lock on a sibling `tokens.json.lock` file:
re-read the store from disk, change only this profile's fields, write atomically.
refresh() holds that lock across the token request too, and first checks whether
another process has already refreshed (a different refresh token on disk with a
still-valid access token), adopting that instead of spending a consumed token.

Each profile is an INDEPENDENT authorization with its own refresh token, so
refreshing one never affects another. A single profile's authorization can also
reach several organisations (see `connections`); `tenant_id` picks the active one
and `switch_tenant()` changes it without re-authorizing.
"""

import base64
import contextlib
import json
import os
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no flock, fall back to no lock
    fcntl = None

import requests
from dotenv import load_dotenv

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

# Refresh proactively if the access token expires within this many seconds.
EXPIRY_BUFFER = 120

# The fields refresh() owns. Only these move between disk and memory when tokens
# are adopted or saved, so a refresh never undoes another process's tenant switch.
TOKEN_FIELDS = ("access_token", "refresh_token", "expires_at", "scope")


class XeroError(Exception):
    pass


# ---------- config directory resolution ----------
#
# Precedence, highest first:
#   1. an explicit path passed in (the CLI's --store / --env flags)
#   2. $XERO_HOME
#   3. $XDG_CONFIG_HOME/xero-api, else ~/.config/xero-api
#
# There is deliberately NO fallback to a file in the current working directory: a
# stale tokens.json in some checkout silently shadowing the real credentials is a
# genuinely confusing bug, so a missing store is an error that names `xero auth`.

APP_DIR_NAME = "xero-api"
STORE_FILENAME = "tokens.json"
ENV_FILENAME = ".env"


def config_dir():
    """The directory holding .env and tokens.json. Not created by this call."""
    home = os.environ.get("XERO_HOME")
    if home:
        return Path(home).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / APP_DIR_NAME


def ensure_dir_0700(path):
    """Create `path` (and parents) if absent and force it to 0700.

    mkdir(mode=...) is masked by the umask, so chmod explicitly: this directory
    holds a client secret and a refresh token and must not be group/world readable.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def ensure_config_dir():
    """Create the config directory 0700 if absent, and return it."""
    return ensure_dir_0700(config_dir())


def default_store_path():
    return config_dir() / STORE_FILENAME


def default_env_path():
    return config_dir() / ENV_FILENAME


def resolve_store_path(store_path=None):
    return Path(store_path).expanduser() if store_path else default_store_path()


def resolve_env_path(env_path=None):
    return Path(env_path).expanduser() if env_path else default_env_path()


def load_credentials(env_path=None):
    """Load XERO_CLIENT_ID / XERO_CLIENT_SECRET from the .env file.

    Values already present in the real environment win, so a caller can export
    them instead of keeping a .env at all.
    """
    path = resolve_env_path(env_path)
    if path.exists():
        load_dotenv(path)
    client_id = os.environ.get("XERO_CLIENT_ID")
    client_secret = os.environ.get("XERO_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise XeroError(
            "XERO_CLIENT_ID / XERO_CLIENT_SECRET not found in the environment or "
            f"{path}.\n"
            f"Create {path} with those two values (see .env.example in the repo), "
            "or export them, then run: xero auth --profile <name>"
        )
    return client_id, client_secret


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


def load_store(store_path=None):
    """Load the version-2 profile token store."""
    store_path = resolve_store_path(store_path)
    if not store_path.exists():
        raise XeroError(
            f"No Xero token store at {store_path}.\n"
            "Authorize first:  xero auth --profile <name>\n"
            "(Set XERO_HOME or pass --store if your store lives elsewhere.)"
        )
    with open(store_path) as f:
        store = json.load(f)
    if not isinstance(store, dict) or "profiles" not in store:
        raise XeroError(
            f"{store_path} is not a profile store (no 'profiles' key). "
            "Run 'xero auth --profile <name>' to create one."
        )
    return store


def save_store(store, store_path=None):
    """Write a whole store. Prefer update_store(): this overwrites whatever another
    process wrote since `store` was read."""
    with store_lock(store_path):
        _atomic_write_json(resolve_store_path(store_path), store)


def lock_path(store_path=None):
    store_path = resolve_store_path(store_path)
    return store_path.with_name(store_path.name + ".lock")


@contextlib.contextmanager
def store_lock(store_path=None):
    """Hold an exclusive lock on the store, across processes.

    The lock is flock() on a sibling `<store>.lock` file, never on tokens.json
    itself, because tokens.json is replaced (os.replace) by every write and a lock
    on the old inode would protect nothing. flock locks belong to the open file, so
    two clients in one process exclude each other too; do not nest this.
    """
    path = lock_path(store_path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock


def update_store(fn, store_path=None):
    """Read-modify-write the store under the lock: fn(store) mutates it in place.

    The store is re-read from disk inside the lock, so a change made by another
    process since this one last looked is never overwritten. Returns the store as
    written.
    """
    with store_lock(store_path):
        store = load_store(store_path)
        fn(store)
        _atomic_write_json(resolve_store_path(store_path), store)
        return store


def list_profiles(store):
    """Return [(name, tenant_name, is_active), ...] for display."""
    active = store.get("active")
    out = []
    for name, prof in store.get("profiles", {}).items():
        out.append((name, prof.get("tenant_name"), name == active))
    return out


class XeroClient:
    def __init__(self, profile=None, env_path=None, store_path=None):
        """Open a Xero connection using stored credentials.

        profile     -- named authorization to use; defaults to the store's active one
        env_path    -- override the .env location (default: config_dir()/.env)
        store_path  -- override the token store (default: config_dir()/tokens.json)
        """
        self.client_id, self.client_secret = load_credentials(env_path)
        self.store_path = resolve_store_path(store_path)
        self.store = load_store(self.store_path)

        profiles = self.store.get("profiles", {})
        if not profiles:
            raise XeroError(
                f"No profiles in {self.store_path}. Run: xero auth --profile <name>"
            )
        self.profile = profile or self.store.get("active") or next(iter(profiles))
        if self.profile not in profiles:
            available = ", ".join(profiles) or "(none)"
            raise XeroError(
                f"Profile '{self.profile}' not found. Available: {available}. "
                "Run 'xero auth --profile <name>' to add it."
            )

    # ---------- active profile ----------

    @property
    def prof(self):
        """The active profile's token dict (mutated in place, persisted on save)."""
        return self.store["profiles"][self.profile]

    def _save_store(self, fields=None):
        """Persist this profile into the store ON DISK, merging rather than
        overwriting: re-read under the lock, copy in only `fields` of this profile
        (all of it when None), write atomically. Other profiles and the active
        pointer stay exactly as some other process may have left them."""
        def merge(store):
            profiles = store.setdefault("profiles", {})
            if fields is None or self.profile not in profiles:
                profiles[self.profile] = dict(self.prof)
            else:
                for k in fields:
                    if k in self.prof:
                        profiles[self.profile][k] = self.prof[k]
        update_store(merge, self.store_path)

    @staticmethod
    def _access_valid(prof, now=None):
        now = time.time() if now is None else now
        return bool(prof.get("access_token")) and now < prof.get("expires_at", 0)

    def _adopt_tokens(self, disk_prof):
        for k in TOKEN_FIELDS:
            if k in disk_prof:
                self.prof[k] = disk_prof[k]

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
        self._save_store(fields=("tenant_id", "tenant_name"))
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

        Safe with other processes on the same store: the whole thing runs under the
        store lock, starting from the store as it is on disk now. If another process
        has already rotated this profile's refresh token and its access token is
        still valid, that is adopted and no refresh is made; otherwise the refresh
        uses the on-disk refresh token, never a stale in-memory one.
        """
        with store_lock(self.store_path):
            disk = load_store(self.store_path)
            disk_prof = disk.get("profiles", {}).get(self.profile)
            if disk_prof is not None:
                if (disk_prof.get("refresh_token") != self.prof.get("refresh_token")
                        and self._access_valid(disk_prof)):
                    self._adopt_tokens(disk_prof)
                    return self.prof["access_token"]
                rt = disk_prof.get("refresh_token")
            else:
                rt = self.prof.get("refresh_token")
            if not rt:
                raise XeroError(
                    f"No refresh_token in profile '{self.profile}'. "
                    f"Run 'xero auth --profile {self.profile}' again."
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
                    "(unused >60 days, revoked, or a rotation was lost). Run 'xero auth' again."
                )

            tok = resp.json()
            # Persist BEFORE anything else (and before releasing the lock). This is
            # the whole point.
            self.prof["access_token"] = tok["access_token"]
            self.prof["refresh_token"] = tok["refresh_token"]  # rotated value
            self.prof["expires_at"] = time.time() + tok["expires_in"] - EXPIRY_BUFFER
            if tok.get("scope"):
                self.prof["scope"] = tok["scope"]
            profiles = disk.setdefault("profiles", {})
            if disk_prof is None:
                profiles[self.profile] = dict(self.prof)
            else:
                for k in TOKEN_FIELDS:
                    if k in self.prof:
                        disk_prof[k] = self.prof[k]
            _atomic_write_json(self.store_path, disk)
            return self.prof["access_token"]

    def ensure_token(self):
        """Return a valid access token, refreshing (and persisting) only if needed.

        When the in-memory token has expired, look at disk first: another process
        may have refreshed already, and its access token is ours to use.
        """
        if self._access_valid(self.prof):
            return self.prof["access_token"]
        try:
            disk_prof = load_store(self.store_path).get("profiles", {}).get(self.profile)
        except (XeroError, OSError, ValueError):
            disk_prof = None
        if disk_prof and self._access_valid(disk_prof):
            self._adopt_tokens(disk_prof)
            return self.prof["access_token"]
        return self.refresh()

    # ---------- API ----------

    def get(self, path, params=None, accept="application/json"):
        """GET a Xero endpoint. `path` may be a full URL or a path under api.xro/2.0.

        Handles: 401 -> one refresh + retry; 429 -> respect Retry-After, retry once.
        Per-tenant limits: 60 calls/min, 5,000/day.

        Returns parsed JSON, or text for any other Accept. Use download() for a
        binary body: decoding one to str corrupts it.
        """
        resp = self._get(path, params=params, accept=accept)
        return resp.json() if "json" in accept else resp.text

    def download(self, path, params=None, accept="application/octet-stream"):
        """GET a Xero endpoint and return the response body as bytes.

        For the endpoints that answer with a file rather than JSON, attachments
        above all:

            pdf = client.download(f"Invoices/{invoice_id}/Attachments/{quote(name)}",
                                  accept="application/pdf")

        Same 401/429 handling as get(), and like get() it only ever reads.
        """
        return self._get(path, params=params, accept=accept).content

    def _get(self, path, params=None, accept="application/json"):
        """The shared GET, returning the response itself so callers decide on decoding."""
        token = self.ensure_token()
        tenant = self.prof.get("tenant_id")
        if not tenant:
            raise XeroError("No tenant_id in profile. Run 'xero auth' first.")
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
            return resp

    def post(self, path, json_body, idempotency_key=None):
        """POST/create against a Xero endpoint. Same 401/429 handling as get().

        `idempotency_key` is sent as Xero's `Idempotency-Key` header: Xero answers a
        repeat of the same key with the first response instead of writing again, so a
        caller that retries after a timeout cannot create the thing twice.
        """
        return self._write("POST", path, json_body, idempotency_key)

    def put(self, path, json_body, idempotency_key=None):
        """PUT/create against a Xero endpoint (several create endpoints, Payments
        among them, are documented as PUT). Same handling as post()."""
        return self._write("PUT", path, json_body, idempotency_key)

    def _write(self, method, path, json_body, idempotency_key=None):
        token = self.ensure_token()
        tenant = self.prof.get("tenant_id")
        if not tenant:
            raise XeroError("No tenant_id in profile. Run 'xero auth' first.")
        url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"
        headers = {
            "Xero-tenant-id": tenant,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            if len(idempotency_key) > 128:
                raise XeroError("Idempotency-Key is limited to 128 characters")
            headers["Idempotency-Key"] = idempotency_key

        attempts = 0
        while True:
            attempts += 1
            resp = requests.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}", **headers},
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
                raise XeroError(f"{method} {url} failed ({resp.status_code}): {resp.text}")
            # Some endpoints answer a success with no body at all -- emailing an
            # invoice (POST Invoices/{id}/Email) returns 204 No Content. Parsing
            # that as JSON raises, which makes a send that worked look like a crash.
            if resp.status_code == 204 or not resp.content.strip():
                return {}
            return resp.json()

    def create_payment(self, invoice, account, amount, date, reference=None,
                       idempotency_key=None):
        """Record a payment against an invoice (PUT Payments). Needs a write scope
        that covers payments (`accounting.payments`).

        invoice    -- the invoice's number (a str), or a dict such as {"InvoiceID": ...}
        account    -- the bank account's code (a str), or a dict such as {"AccountID": ...}
        amount     -- the amount paid, in the invoice's currency
        date       -- the payment date, a datetime.date or "YYYY-MM-DD"
        reference  -- optional text shown on the payment and used by bank reconciliation
        idempotency_key -- optional; see post()

        Xero refuses a payment on a DRAFT invoice or one larger than the amount due,
        and says why in the XeroError. Returns the created payment (a dict with
        PaymentID, Amount, Reference, Invoice, Account, ...).
        """
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            raise XeroError(f"amount must be a positive number, got {amount!r}")
        payment = {
            "Invoice": invoice if isinstance(invoice, dict) else {"InvoiceNumber": str(invoice)},
            "Account": account if isinstance(account, dict) else {"Code": str(account)},
            "Date": date if isinstance(date, str) else date.isoformat(),
            "Amount": amount,
        }
        if reference:
            payment["Reference"] = reference
        resp = self.put("Payments", {"Payments": [payment]}, idempotency_key=idempotency_key)
        created = (resp or {}).get("Payments") or []
        if not created:
            raise XeroError(f"PUT Payments returned no payment: {resp!r}")
        errors = created[0].get("ValidationErrors") or []
        if errors:
            raise XeroError("Xero refused the payment: " + "; ".join(e.get("Message", "?") for e in errors))
        return created[0]

    def upload(self, path, data, content_type="application/octet-stream", params=None,
               method="PUT"):
        """Upload a raw binary body -- Xero's attachment endpoints, in practice.

        `path` carries the file name as its last segment and `data` is the file's
        bytes, e.g.

            client.upload(f"Invoices/{invoice_id}/Attachments/{quote(name)}",
                          pdf_bytes, "application/pdf",
                          params={"IncludeOnline": "true"})

        PUT creates a new attachment; POST replaces one that already has that file
        name. Same 401/429 handling as get().
        """
        if method not in ("PUT", "POST"):
            raise XeroError(f"upload() takes PUT or POST, not {method}")
        token = self.ensure_token()
        tenant = self.prof.get("tenant_id")
        if not tenant:
            raise XeroError("No tenant_id in profile. Run 'xero auth' first.")
        url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"

        attempts = 0
        while True:
            attempts += 1
            resp = requests.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Xero-tenant-id": tenant,
                    "Accept": "application/json",
                    "Content-Type": content_type,
                },
                params=params,
                data=data,
                timeout=60,
            )
            if resp.status_code == 401 and attempts == 1:
                token = self.refresh()
                continue
            if resp.status_code == 429 and attempts <= 2:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                time.sleep(retry_after + 1)
                continue
            if resp.status_code >= 400:
                raise XeroError(f"{method} {url} failed ({resp.status_code}): {resp.text}")
            return resp.json()
