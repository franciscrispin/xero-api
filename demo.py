"""Smoke test: refresh (persist the rotated token first), then read some data.

Works with whatever scopes you granted: each read is attempted independently and
a permission error on one does not stop the others. Run against the Demo Company
first (authorize.py selects it automatically if present); it consumes no
subscription.

    python demo.py                 # uses the active profile
    python demo.py --profile real  # uses a specific profile
"""

import argparse

from xero_client import XeroClient, XeroError


def try_read(label, fn):
    try:
        fn()
    except XeroError as e:
        msg = str(e).splitlines()[0]
        print(f"  ({label} skipped: {msg})")


def main():
    parser = argparse.ArgumentParser(description="Xero connection smoke test.")
    parser.add_argument(
        "--profile", default=None, help="Profile to use (default: the active one)."
    )
    args = parser.parse_args()

    client = XeroClient(profile=args.profile)  # loads .env + tokens.json
    print(f"Profile: {client.profile}")
    print(f"Tenant: {client.prof.get('tenant_name')} ({client.prof.get('tenant_id')})")
    print(f"Granted scope: {client.prof.get('scope')}\n")

    # Force the refresh-then-persist path so you can see it work end to end.
    # In normal use you would just call client.get(...) and let ensure_token()
    # decide. Either way, the rotated refresh token is written to disk before any
    # API call runs.
    print("Refreshing access token (rotated refresh token persisted immediately)...")
    client.refresh()
    print("  refreshed; tokens.json updated.\n")

    def org():
        o = client.get("Organisation")["Organisations"][0]
        print(f"Organisation: {o.get('Name')}  ({o.get('CountryCode')}, base currency {o.get('BaseCurrency')})")

    def invoices():
        rows = client.get("Invoices", params={"page": 1, "pageSize": 5}).get("Invoices", [])
        print(f"Invoices (up to 5): {len(rows)}")
        for i in rows:
            print(f"  {i.get('Type'):8} {i.get('InvoiceNumber','-'):12} "
                  f"{i.get('Total'):>12} {i.get('CurrencyCode','')}  {i.get('Status')}")

    def contacts():
        rows = client.get("Contacts", params={"page": 1}).get("Contacts", [])
        print(f"Contacts (page 1): {len(rows)}")

    def profit_and_loss():
        reports = client.get("Reports/ProfitAndLoss").get("Reports", [])
        if reports:
            print(f"Report: {reports[0].get('ReportName')}")

    try_read("Organisation", org)
    try_read("Invoices", invoices)
    try_read("Contacts", contacts)
    try_read("ProfitAndLoss", profit_and_loss)

    print("\nDone. If at least one read printed data, the connection works and self-refreshes.")


if __name__ == "__main__":
    try:
        main()
    except XeroError as e:
        raise SystemExit(f"\nXero error: {e}")
