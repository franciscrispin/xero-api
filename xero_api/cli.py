"""The `xero` command-line interface.

Design constraint that shapes this whole file: **reads and writes must be separable
by command prefix.** An agent's permission system allowlists by the literal start of
a shell command, so every read has to be reachable without typing a verb that could
also write. Hence:

    xero get ...            reads only, always -- never issues a POST, and has no
                            flag that would make it write
    xero profiles list      reads the local store only, never touches the Xero API
    xero post ...           the only command that writes to Xero
    xero auth ...           the only command that starts an OAuth flow

That makes `Bash(xero get:*)` safe to auto-approve while `xero post` and `xero auth`
still require a human. Do not add a --write-style flag to `get`, and do not let any
read path fall through to `post`.

`get` and `post` are deliberately GENERIC passthroughs over Xero endpoint names
rather than per-endpoint subcommands (`xero invoices list --unpaid` and friends).
Per-endpoint commands would have to grow to cover the whole Xero API and would never
be finished; a passthrough covers all of it today and emits JSON for `jq`.
"""

import argparse
import json
import sys

from . import __version__, auth, profiles
from .client import XeroClient, XeroError, config_dir


# ---------- shared flags ----------


def _common_flags(parser):
    """Flags every subcommand accepts. Precedence: flag > env var > config dir."""
    parser.add_argument(
        "--profile",
        default=None,
        help="Profile to use (default: the active one in the token store).",
    )
    parser.add_argument(
        "--store",
        default=None,
        metavar="PATH",
        help=f"Token store path (default: $XERO_HOME or {config_dir()}/tokens.json).",
    )
    parser.add_argument(
        "--env",
        default=None,
        metavar="PATH",
        help=f"Credentials .env path (default: $XERO_HOME or {config_dir()}/.env).",
    )
    return parser


def _parse_params(pairs):
    """Turn ['page=1', 'where=Name=="X"'] into {'page': '1', 'where': 'Name=="X"'}."""
    params = {}
    for pair in pairs or []:
        if "=" not in pair:
            sys.exit(f"--params expects key=value, got: {pair!r}")
        key, value = pair.split("=", 1)  # split once: values may contain '='
        params[key] = value
    return params


def _emit(data):
    json.dump(data, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


# ---------- read conveniences ----------
#
# Two hand-written listings, and deliberately no more. They exist because item codes
# and branding theme IDs are the two things you must look up by hand before writing
# an invoice, and reading them out of raw JSON is tedious. Everything else goes
# through the generic passthrough.


def _print_items(client):
    rows = client.get("Items").get("Items", [])
    if not rows:
        print("(no items)")
        return
    width = max(len(r.get("Code") or "") for r in rows)
    for r in rows:
        price = (r.get("SalesDetails") or {}).get("UnitPrice")
        price = "-" if price is None else f"{price:g}"
        print(f"{(r.get('Code') or '').ljust(width)}  {price:>10}  {r.get('Name') or ''}")


def _print_branding_themes(client):
    rows = client.get("BrandingThemes").get("BrandingThemes", [])
    if not rows:
        print("(no branding themes)")
        return
    for r in rows:
        print(f"{r.get('BrandingThemeID')}  {r.get('Name') or ''}")


# ---------- commands ----------


def cmd_get(args):
    """Read from Xero. This function must never perform a write of any kind."""
    # Validate the invocation before touching credentials, so a usage mistake does
    # not first fail on a missing token store.
    if not (args.endpoint or args.list_items or args.list_branding_themes):
        sys.exit(
            "No endpoint given. Examples:\n"
            "  xero get Organisation\n"
            "  xero get Contacts --where 'Name==\"Acme Ltd\"'\n"
            "  xero get Invoices --params page=1 --params pageSize=5\n"
            "Or a convenience listing: xero get --list-items"
        )
    params = _parse_params(args.params)

    client = XeroClient(profile=args.profile, env_path=args.env, store_path=args.store)
    if args.list_items:
        return _print_items(client)
    if args.list_branding_themes:
        return _print_branding_themes(client)

    if args.where:
        params["where"] = args.where
    _emit(client.get(args.endpoint, params=params or None))


def _load_data(raw):
    """--data accepts inline JSON or @path/to/file.json."""
    if raw.startswith("@"):
        path = raw[1:]
        try:
            with open(path) as f:
                text = f.read()
        except OSError as e:
            sys.exit(f"Could not read --data file {path}: {e}")
    else:
        text = raw
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"--data is not valid JSON: {e}")


def cmd_post(args):
    """Write to Xero. The only command in this CLI that does."""
    client = XeroClient(profile=args.profile, env_path=args.env, store_path=args.store)
    _emit(client.post(args.endpoint, _load_data(args.data)))


# ---------- parser ----------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="xero",
        description="Xero API client. Reads (get, profiles list) are separable from "
        "writes (post) and authorization (auth) by command prefix.",
    )
    parser.add_argument("--version", action="version", version=f"xero {__version__}")
    sub = parser.add_subparsers(dest="command")

    # --- auth ---
    p_auth = _common_flags(
        sub.add_parser("auth", help="Authorize in the browser and save a profile.")
    )
    scope_group = p_auth.add_mutually_exclusive_group()
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
    p_auth.add_argument(
        "--tenant",
        default=None,
        help="Preselect the active organisation by tenantName (case-insensitive) or "
        "tenantId, skipping the interactive prompt when the auth reaches several orgs.",
    )

    # --- profiles ---
    # A flat positional action rather than nested subparsers: it keeps one shared
    # set of flags, so `xero profiles tenant "Acme" --profile demo` parses the same
    # way no matter where the flags appear.
    p_prof = _common_flags(
        sub.add_parser(
            "profiles",
            help="List, switch, and re-point profiles. Reads the local store only.",
            description="Manage saved authorizations. 'list' never touches the Xero "
            "API; 'use' and 'tenant' only rewrite the local token store.",
            epilog="Examples:\n"
            "  xero profiles list\n"
            "  xero profiles use real\n"
            '  xero profiles tenant "Acme Ltd" --profile real',
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
    )
    p_prof.add_argument(
        "action",
        nargs="?",
        choices=["list", "use", "tenant"],
        default="list",
        help="list (default), use <name>, or tenant <tenantId|tenantName>.",
    )
    p_prof.add_argument(
        "target",
        nargs="?",
        help="Profile name for 'use'; tenantId or tenantName for 'tenant'.",
    )

    # --- get ---
    p_get = _common_flags(
        sub.add_parser(
            "get",
            help="Read any Xero endpoint; prints JSON on stdout. Never writes.",
            description="Read any endpoint under api.xro/2.0 (or a full URL) and print "
            "the JSON response. This command never writes to Xero.",
            epilog="Examples:\n"
            "  xero get Organisation\n"
            "  xero get Contacts --where 'Name==\"Acme Ltd\"'\n"
            "  xero get Invoices --params page=1 --params pageSize=5 | jq '.Invoices[].InvoiceNumber'\n"
            "  xero get --list-items",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
    )
    p_get.add_argument(
        "endpoint",
        nargs="?",
        help="Endpoint name, e.g. Contacts, Invoices, Reports/ProfitAndLoss.",
    )
    p_get.add_argument(
        "--where", default=None, help="Xero 'where' filter, e.g. 'Name==\"Acme Ltd\"'."
    )
    p_get.add_argument(
        "--params",
        action="append",
        default=[],
        metavar="K=V",
        help="Extra query parameter; repeatable.",
    )
    p_get.add_argument(
        "--list-items", action="store_true", help="Convenience: list item codes and prices."
    )
    p_get.add_argument(
        "--list-branding-themes",
        action="store_true",
        help="Convenience: list branding theme IDs and names.",
    )

    # --- post ---
    p_post = _common_flags(
        sub.add_parser("post", help="Write to a Xero endpoint. Requires a JSON body.")
    )
    p_post.add_argument("endpoint", help="Endpoint name, e.g. Invoices, Contacts.")
    p_post.add_argument(
        "--data",
        required=True,
        help="Request body: inline JSON, or @path/to/file.json.",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    try:
        if args.command == "auth":
            auth.run(args)
        elif args.command == "profiles":
            profiles.run(args)
        elif args.command == "get":
            cmd_get(args)
        elif args.command == "post":
            cmd_post(args)
    except XeroError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
    return 0


if __name__ == "__main__":
    sys.exit(main())
