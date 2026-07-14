"""One-time browser authorization for a Xero Web app (standard auth code flow).

Scopes are chosen at run time, not hardcoded. Pass them with --scopes:

    python authorize.py --scopes accounting.invoices.read accounting.contacts.read
    python authorize.py --scopes "accounting.invoices accounting.invoices.read"

offline_access is added automatically (mandatory for a refresh token). Valid scope
strings are validated against scopes.txt. With no --scopes, a read-only accounting
default is used.

The script opens your browser, you consent, it catches the redirect on the loopback
port, swaps the code for tokens, fetches your tenantId, and writes tokens.json.
Run it on a machine with a browser (i.e. your own computer, not a headless server).
"""

import argparse
import base64
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()  # load .env now so XERO_REDIRECT_PORT below can override the default

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
STORE_PATH = "tokens.json"
SCOPES_FILE = Path(__file__).parent / "scopes.txt"

# The loopback port for the redirect. Arbitrary, but MUST match the redirect URI
# registered in the Xero app portal exactly. 8080 is heavily used, so default to
# something quieter. Override with XERO_REDIRECT_PORT in .env.
REDIRECT_PORT = int(os.environ.get("XERO_REDIRECT_PORT", "8723"))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

# Always allowed, even though they are not in scopes.txt.
ALWAYS_VALID = {"offline_access", "openid", "profile", "email"}

# Used only when --scopes is omitted: a safe read-only accounting starter set.
DEFAULT_SCOPES = [
    "accounting.settings.read",
    "accounting.contacts.read",
    "accounting.invoices.read",
    "accounting.payments.read",
    "accounting.banktransactions.read",
    "accounting.manualjournals.read",
    "accounting.reports.profitandloss.read",
    "accounting.reports.balancesheet.read",
    "accounting.reports.trialbalance.read",
]


def load_catalog():
    lines = SCOPES_FILE.read_text().splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


class _CallbackHandler(BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        _CallbackHandler.result = dict(urllib.parse.parse_qsl(parsed.query))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h2>Xero authorization received.</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
        )

    def log_message(self, *args):
        pass  # keep the terminal quiet


def _basic_auth(client_id, client_secret):
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _save_store(store):
    with open(STORE_PATH, "w") as f:
        json.dump(store, f, indent=2)
    os.chmod(STORE_PATH, 0o600)


def resolve_scopes(raw_scopes, catalog):
    """Flatten, de-dupe, validate, and prepend the mandatory offline_access."""
    if raw_scopes:
        tokens = []
        for chunk in raw_scopes:
            tokens += [s for s in re.split(r"[,\s]+", chunk) if s]
    else:
        tokens = list(DEFAULT_SCOPES)

    unknown = [s for s in tokens if s not in catalog and s not in ALWAYS_VALID]
    if unknown:
        sys.exit(
            "Unknown scope(s): " + ", ".join(unknown) + "\n"
            "Check the spelling against scopes.txt (the full catalog)."
        )

    # offline_access first, then requested scopes in order, de-duplicated.
    ordered = ["offline_access"] + [s for s in tokens if s != "offline_access"]
    seen, result = set(), []
    for s in ordered:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def main():
    parser = argparse.ArgumentParser(description="One-time Xero authorization.")
    parser.add_argument(
        "--scopes",
        nargs="+",
        default=None,
        help="Scopes to request (space or comma separated). See scopes.txt. "
        "offline_access is added automatically.",
    )
    args = parser.parse_args()

    client_id = os.environ.get("XERO_CLIENT_ID")
    client_secret = os.environ.get("XERO_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("XERO_CLIENT_ID / XERO_CLIENT_SECRET missing. Copy .env.example to .env and fill it in.")

    scopes = resolve_scopes(args.scopes, load_catalog())
    print("Requesting scopes:")
    for s in scopes:
        print(f"  - {s}")
    print()

    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(scopes),
        "state": state,
    }
    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)

    print("Opening your browser to authorize. If it does not open, paste this URL:\n")
    print(auth_url + "\n")
    webbrowser.open(auth_url)

    # Wait for the single redirect back to the loopback callback.
    HTTPServer.allow_reuse_address = True  # avoid TIME_WAIT errors on repeated runs
    try:
        server = HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    except OSError as e:
        sys.exit(
            f"Could not bind port {REDIRECT_PORT}: {e}\n"
            "Something else is using it. Set XERO_REDIRECT_PORT in .env to a free port, "
            "and add the matching redirect URI in the Xero app portal."
        )
    print(f"Waiting for the redirect on {REDIRECT_URI} ...")
    server.handle_request()
    result = _CallbackHandler.result

    if "error" in result:
        sys.exit(f"Authorization failed: {result.get('error')} {result.get('error_description', '')}")
    if result.get("state") != state:
        sys.exit("State mismatch. Aborting (possible CSRF). Run the script again.")
    code = result.get("code")
    if not code:
        sys.exit("No authorization code in the redirect. Run the script again.")

    # Exchange the code (valid ~5 min) for tokens.
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed ({resp.status_code}): {resp.text}")
    tok = resp.json()

    store = {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + tok["expires_in"] - 120,
        "scope": tok.get("scope", " ".join(scopes)),
    }

    # Fetch the tenant(s) this authorization can reach.
    conns = requests.get(
        CONNECTIONS_URL,
        headers={
            "Authorization": f"Bearer {store['access_token']}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if conns.status_code != 200:
        sys.exit(f"/connections failed ({conns.status_code}): {conns.text}")
    connections = conns.json()
    if not connections:
        sys.exit("No connections returned. Did you select an organisation during consent?")

    store["connections"] = [
        {"tenantId": c["tenantId"], "tenantName": c.get("tenantName"), "tenantType": c.get("tenantType")}
        for c in connections
    ]

    # Prefer the Demo Company for first testing (free, consumes no subscription).
    demo = next((c for c in connections if "demo" in (c.get("tenantName") or "").lower()), None)
    if demo:
        chosen = demo
    elif len(connections) == 1:
        chosen = connections[0]
    else:
        print("\nMultiple organisations found:")
        for i, c in enumerate(connections):
            print(f"  [{i}] {c.get('tenantName')}  ({c.get('tenantType')})  {c['tenantId']}")
        idx = int(input("Pick the number to use as the active tenant: ").strip())
        chosen = connections[idx]

    store["tenant_id"] = chosen["tenantId"]
    store["tenant_name"] = chosen.get("tenantName")
    _save_store(store)

    print(f"\nSaved {STORE_PATH}. Active tenant: {store['tenant_name']} ({store['tenant_id']}).")
    if demo:
        print("This is the Demo Company, good for first tests. Re-run and pick another tenant for live data.")
    print("Next: python demo.py")


if __name__ == "__main__":
    main()
