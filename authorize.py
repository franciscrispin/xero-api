"""One-time browser authorization for a Xero Web app (standard auth code flow).

Scopes are chosen at run time, not hardcoded. Pass them with --scopes:

    python authorize.py --scopes accounting.invoices.read accounting.contacts.read
    python authorize.py --scopes "accounting.invoices accounting.invoices.read"

Or request everything in one go:

    python authorize.py --all-scopes

offline_access is added automatically (mandatory for a refresh token). Valid scope
strings are validated against scopes.txt. With no --scopes, a read-only accounting
default is used. --all-scopes requests every scope in scopes.txt except those in
EXCLUDED_FROM_ALL (see the comment there — Xero rejects some scopes outright for
this flow, regardless of what else is requested alongside them).

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

from xero_client import _atomic_write_json, _migrate_store

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

# Scopes present in scopes.txt that --all-scopes should NOT request.
#
# app.connections: confirmed empirically (2026-07-14) that Xero's authorize endpoint
# returns "access_denied: Requested wrong apps scopes" whenever this scope is present
# in the request -- alone, or combined with any other scopes. It's a non-tenanted
# scope for managing the Connections API directly, which this project's tenant-scoped
# XeroClient never calls, so there's nothing lost by excluding it. If a future app
# needs it, request it on its own with --scopes app.connections and expect it to be
# rejected until Xero support enables it for the app.
EXCLUDED_FROM_ALL = {"app.connections"}

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


def load_all_scopes_ordered():
    """Every scope in scopes.txt, file order, minus EXCLUDED_FROM_ALL."""
    lines = SCOPES_FILE.read_text().splitlines()
    scopes = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    return [s for s in scopes if s not in EXCLUDED_FROM_ALL]


# Callback success page, styled to the Agent Works deck design system
# (warm paper, copper accent, Fraunces + Instrument Sans, 6px copper top rule).
# Self-contained: Google Fonts load if online, with graceful serif/sans fallbacks.
SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Xero connected · Agent Works</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400..700;1,9..144,400..700&family=Instrument+Sans:ital,wght@0,400..700;1,400..700&display=swap" rel="stylesheet">
<style>
:root{
  --paper:#FBF7EF; --ink:#26211A; --muted:#71685A; --line:#E3D9C6;
  --copper:#B4522A; --copper-deep:#93401F; --copper-soft:#F6E3D7;
  --serif:"Fraunces",Georgia,"Times New Roman",serif;
  --sans:"Instrument Sans",system-ui,-apple-system,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{
  font-family:var(--sans);color:var(--ink);background:var(--paper);
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:32px;-webkit-font-smoothing:antialiased;
}
.topbar{position:fixed;top:0;left:0;right:0;height:6px;background:var(--copper);}
.card{
  background:#FFFDF8;border:1px solid var(--line);border-radius:18px;
  box-shadow:0 10px 34px rgba(60,45,20,.09);
  max-width:560px;width:100%;padding:56px 56px 40px;text-align:center;
}
.kicker{
  font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:2px;
  text-transform:uppercase;color:var(--copper);margin-bottom:28px;
}
.check{
  width:72px;height:72px;border-radius:50%;background:var(--copper);
  display:flex;align-items:center;justify-content:center;margin:0 auto 28px;
  box-shadow:0 4px 14px rgba(180,82,42,.28);
}
.check svg{width:34px;height:34px;}
h1{font-family:var(--serif);font-size:42px;font-weight:600;line-height:1.1;letter-spacing:-.5px;}
h1 .accent{color:var(--copper);font-style:italic;}
p.lead{color:var(--muted);font-size:19px;line-height:1.55;margin-top:18px;}
.hint{
  margin-top:30px;padding:14px 18px;border-radius:12px;
  background:var(--copper-soft);border:1px solid #E4C3AC;
  font-family:var(--mono);font-size:14px;color:var(--copper-deep);
}
.foot{
  margin-top:36px;padding-top:20px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:12px;font-weight:600;letter-spacing:1.5px;
  text-transform:uppercase;color:var(--muted);
}
</style>
</head>
<body>
<div class="topbar"></div>
<main class="card">
  <div class="kicker">Xero · Connected</div>
  <div class="check">
    <svg viewBox="0 0 24 24" fill="none" stroke="#FFF8EE" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
  </div>
  <h1>You're <span class="accent">connected</span>.</h1>
  <p class="lead">Xero authorization received. You can close this tab and return to the terminal.</p>
  <div class="hint">The setup script is finishing in your terminal.</div>
  <div class="foot">Agent Works</div>
</main>
</body>
</html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        _CallbackHandler.result = dict(urllib.parse.parse_qsl(parsed.query))
        body = SUCCESS_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the terminal quiet


def _basic_auth(client_id, client_secret):
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _load_store_or_empty():
    """Return the existing (migrated) multi-profile store, or a fresh empty one."""
    path = Path(STORE_PATH)
    if not path.exists():
        return {"version": 2, "active": None, "profiles": {}}
    with open(path) as f:
        raw = json.load(f)
    store, _ = _migrate_store(raw)
    return store


def _default_profile_name(chosen):
    """Infer a profile name from the chosen tenant if the user didn't pass one."""
    name = (chosen.get("tenantName") or "").lower()
    if "demo" in name:
        return "demo"
    return "real"


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
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--scopes",
        nargs="+",
        default=None,
        help="Scopes to request (space or comma separated). See scopes.txt. "
        "offline_access is added automatically.",
    )
    scope_group.add_argument(
        "--all-scopes",
        action="store_true",
        help="Request every scope in scopes.txt except EXCLUDED_FROM_ALL "
        "(currently just app.connections, which Xero rejects for this flow).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Name to store this authorization under (e.g. demo, real). Existing "
        "profiles are preserved. Defaults to 'demo' for the Demo Company, else 'real'.",
    )
    parser.add_argument(
        "--tenant",
        default=None,
        help="Preselect the active organisation by tenantName (case-insensitive) or "
        "tenantId, skipping the interactive prompt when the auth reaches several orgs.",
    )
    args = parser.parse_args()

    client_id = os.environ.get("XERO_CLIENT_ID")
    client_secret = os.environ.get("XERO_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("XERO_CLIENT_ID / XERO_CLIENT_SECRET missing. Copy .env.example to .env and fill it in.")

    if args.all_scopes:
        print(
            f"--all-scopes: requesting every scope in scopes.txt except "
            f"{', '.join(sorted(EXCLUDED_FROM_ALL))} (see EXCLUDED_FROM_ALL in "
            "authorize.py for why).\n"
        )
        scopes = resolve_scopes([" ".join(load_all_scopes_ordered())], load_catalog())
    else:
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
        err = result.get("error", "")
        desc = result.get("error_description", "")
        msg = f"Authorization failed: {err} {desc}"
        if err == "access_denied" and "scope" in desc.lower():
            msg += (
                "\n\nXero rejected the whole request because of one or more scopes in "
                "it -- this is a scope-level problem (not enabled for this app/org, or "
                "needs certification), not an OAuth config problem. To find the culprit, "
                "bisect: split the scope list in half, run --scopes with just the first "
                "half, then the second half, and recurse into whichever half fails. "
                "(app.connections is a known offender -- see EXCLUDED_FROM_ALL in this "
                "file -- so try dropping it first if it's in your list.)"
            )
        sys.exit(msg)
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

    profile = {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + tok["expires_in"] - 120,
        "scope": tok.get("scope", " ".join(scopes)),
    }

    # Fetch the tenant(s) this authorization can reach.
    conns = requests.get(
        CONNECTIONS_URL,
        headers={
            "Authorization": f"Bearer {profile['access_token']}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if conns.status_code != 200:
        sys.exit(f"/connections failed ({conns.status_code}): {conns.text}")
    connections = conns.json()
    if not connections:
        sys.exit("No connections returned. Did you select an organisation during consent?")

    profile["connections"] = [
        {"tenantId": c["tenantId"], "tenantName": c.get("tenantName"), "tenantType": c.get("tenantType")}
        for c in connections
    ]

    # Choose the active tenant for this profile. If the user named the profile
    # 'real' (or anything non-demo), prefer a real org; otherwise prefer the Demo
    # Company for safe first testing. The user can still switch later without
    # re-authorizing (xero_profiles.py tenant ...).
    demo = next((c for c in connections if "demo" in (c.get("tenantName") or "").lower()), None)
    real = next((c for c in connections if c is not demo), None)
    prefer_real = args.profile is not None and args.profile.lower() != "demo"

    if prefer_real and real:
        chosen = real
    elif demo:
        chosen = demo
    elif len(connections) == 1:
        chosen = connections[0]

    # --tenant preselects non-interactively (needed when this runs unattended).
    if args.tenant:
        match = next(
            (
                c
                for c in connections
                if c["tenantId"] == args.tenant
                or (c.get("tenantName") or "").lower() == args.tenant.lower()
            ),
            None,
        )
        if not match:
            names = ", ".join(c.get("tenantName") or "?" for c in connections)
            sys.exit(f"--tenant '{args.tenant}' matched no connected org. Reachable: {names}.")
        chosen = match
    elif len(connections) > 1:
        print("\nOrganisations this authorization can reach:")
        for i, c in enumerate(connections):
            marker = " <- default" if c["tenantId"] == chosen["tenantId"] else ""
            print(f"  [{i}] {c.get('tenantName')}  ({c.get('tenantType')})  {c['tenantId']}{marker}")
        raw = input(f"Pick the active tenant [{connections.index(chosen)}]: ").strip()
        if raw:
            chosen = connections[int(raw)]

    profile["tenant_id"] = chosen["tenantId"]
    profile["tenant_name"] = chosen.get("tenantName")

    profile_name = args.profile or _default_profile_name(chosen)

    # Merge into the existing store, preserving other profiles, and activate this one.
    store = _load_store_or_empty()
    existed = profile_name in store["profiles"]
    store["profiles"][profile_name] = profile
    store["active"] = profile_name
    _atomic_write_json(STORE_PATH, store)

    verb = "Updated" if existed else "Added"
    print(f"\n{verb} profile '{profile_name}' in {STORE_PATH} and set it active.")
    print(f"Active tenant: {profile['tenant_name']} ({profile['tenant_id']}).")
    others = [p for p in store["profiles"] if p != profile_name]
    if others:
        print(f"Other saved profiles (preserved): {', '.join(others)}.")
        print("Switch anytime with:  python xero_profiles.py use <name>")
    print(f"Next: python demo.py --profile {profile_name}")


if __name__ == "__main__":
    main()
