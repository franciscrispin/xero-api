"""Manage saved Xero profiles (e.g. demo vs real company) in the token store.

    xero profiles list                      # list profiles, mark the active one
    xero profiles use real                  # set the active profile
    xero profiles tenant "Acme Ltd"         # switch the active profile's tenant
    xero profiles tenant "Acme Ltd" --profile demo

Profiles are independent authorizations, each with its own refresh token. A single
profile can reach several organisations (its `connections`); `tenant` switches
between them without re-authorizing. Adding a new profile is done by `xero auth`:

    xero auth --profile real --all-scopes

`profiles list` is a pure read of the local store and never touches the Xero API.
"""

import sys

from .client import XeroClient, list_profiles, load_store, save_store


def cmd_list(store_path=None):
    store = load_store(store_path)
    rows = list_profiles(store)
    if not rows:
        print("No profiles. Run: xero auth --profile <name>")
        return
    width = max(len(name) for name, _, _ in rows)
    for name, tenant_name, is_active in rows:
        marker = "*" if is_active else " "
        print(f" {marker} {name.ljust(width)}   {tenant_name or '(no active tenant)'}")
    print("\n* = active profile.  Switch with: xero profiles use <name>")


def cmd_use(name, store_path=None):
    store = load_store(store_path)
    if name not in store.get("profiles", {}):
        available = ", ".join(store.get("profiles", {})) or "(none)"
        sys.exit(f"Profile '{name}' not found. Available: {available}.")
    store["active"] = name
    save_store(store, store_path)
    tenant = store["profiles"][name].get("tenant_name")
    print(f"Active profile is now '{name}'  (tenant: {tenant}).")


def cmd_tenant(tenant, profile=None, env_path=None, store_path=None):
    # Uses the client so the switch is validated against the profile's connections
    # and persisted atomically.
    client = XeroClient(profile=profile, env_path=env_path, store_path=store_path)
    match = client.switch_tenant(tenant)
    print(
        f"Profile '{client.profile}' now points at "
        f"{match.get('tenantName')} ({match['tenantId']})."
    )


def run(args):
    """Execute `xero profiles ...`. `args` is the namespace built by cli.py."""
    action = args.action or "list"
    if action == "list":
        cmd_list(args.store)
    elif action == "use":
        if not args.target:
            sys.exit("Usage: xero profiles use <name>")
        cmd_use(args.target, args.store)
    elif action == "tenant":
        if not args.target:
            sys.exit("Usage: xero profiles tenant <tenantId|tenantName> [--profile P]")
        cmd_tenant(args.target, args.profile, args.env, args.store)
