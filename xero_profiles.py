"""Manage saved Xero profiles (e.g. demo vs real company) in tokens.json.

    python xero_profiles.py                 # list profiles, mark the active one
    python xero_profiles.py list            # same as above
    python xero_profiles.py use real        # set the active profile
    python xero_profiles.py tenant Playertwos          # switch active tenant of the
                                                       # active profile (no re-auth)
    python xero_profiles.py tenant Playertwos --profile demo

Profiles are independent authorizations, each with its own refresh token. A single
profile can reach several organisations (its `connections`); `tenant` switches
between them without re-authorizing. Adding a new profile is done by authorize.py:

    python authorize.py --profile real --all-scopes
"""

import argparse
import sys

from xero_client import XeroClient, XeroError, load_store, save_store, list_profiles

STORE_PATH = "tokens.json"


def cmd_list():
    store = load_store(STORE_PATH)
    rows = list_profiles(store)
    if not rows:
        print("No profiles. Run: python authorize.py --profile <name>")
        return
    width = max(len(name) for name, _, _ in rows)
    for name, tenant_name, is_active in rows:
        marker = "*" if is_active else " "
        print(f" {marker} {name.ljust(width)}   {tenant_name or '(no active tenant)'}")
    print("\n* = active profile.  Switch with: python xero_profiles.py use <name>")


def cmd_use(name):
    store = load_store(STORE_PATH)
    if name not in store.get("profiles", {}):
        available = ", ".join(store.get("profiles", {})) or "(none)"
        sys.exit(f"Profile '{name}' not found. Available: {available}.")
    store["active"] = name
    save_store(store, STORE_PATH)
    tenant = store["profiles"][name].get("tenant_name")
    print(f"Active profile is now '{name}'  (tenant: {tenant}).")


def cmd_tenant(tenant, profile):
    # Uses the client so the switch is validated against the profile's connections
    # and persisted atomically.
    client = XeroClient(profile=profile)
    match = client.switch_tenant(tenant)
    print(
        f"Profile '{client.profile}' now points at "
        f"{match.get('tenantName')} ({match['tenantId']})."
    )


def main():
    parser = argparse.ArgumentParser(description="Manage Xero profiles in tokens.json.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List profiles and show the active one.")

    p_use = sub.add_parser("use", help="Set the active profile.")
    p_use.add_argument("name", help="Profile name to activate.")

    p_tenant = sub.add_parser(
        "tenant", help="Switch the active organisation within a profile (no re-auth)."
    )
    p_tenant.add_argument("tenant", help="tenantId or tenantName to switch to.")
    p_tenant.add_argument(
        "--profile", default=None, help="Profile to change (default: the active one)."
    )

    args = parser.parse_args()

    try:
        if args.command in (None, "list"):
            cmd_list()
        elif args.command == "use":
            cmd_use(args.name)
        elif args.command == "tenant":
            cmd_tenant(args.tenant, args.profile)
    except XeroError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
