# xero-api

A small, safe Xero API integration in Python. Authorize once in the browser, then
the scripts refresh their own tokens forever. Scopes are chosen at setup time, so
the same tool works for read-only reporting or full read-write automation.

Built for the standard OAuth 2.0 authorization-code flow with a **Web app**, which
is the flow that works for every region (including Singapore, where Custom
Connections are not available).

---

## For the agent setting this up

This README is written so an AI agent can walk a first-time user through setup.
The user is assumed to have a Xero account but **no developer account yet**.

Split of work:

- **You (the agent) do**: create the Python environment, install dependencies,
  write `.env`, build the authorize command, run `demo.py`, and diagnose errors.
- **The user must do in a browser** (you cannot): create the developer account,
  create the app, generate the client secret, and click "Allow access" during
  consent. Give them the exact values to enter, then wait.
- **Running `authorize.py`**: it opens a browser and listens on `localhost`. If you
  (the agent) run on the **same computer** as the user's browser, you can run it.
  If you run on a server or a different machine, give the user the command to run
  themselves.

Follow the steps in order. Do the steps marked **[agent]**; guide the steps marked
**[user]**.

---

## Step 1 — Create the Xero developer account [user]

Ask the user to:

1. Open https://developer.xero.com and click **Log in** (top right).
2. Log in with their existing Xero email and password.
3. Accept the developer terms if prompted.

That is the whole "developer account" step. It uses their existing Xero login; no
separate signup.

## Step 2 — Create a Web app [user]

Ask the user to go to https://developer.xero.com/app/manage, click **New app**, and
enter exactly:

| Field | Value |
|-------|-------|
| Integration type | **Web app** |
| App name | anything, e.g. `My company integration` |
| Company or application URL | any URL, e.g. `https://example.com` |
| Redirect URI | `http://localhost:8723/callback` |

Then:

1. Click **Create app**.
2. Open the app's **Configuration** tab.
3. Click **Generate a secret** and copy it now (it is shown once).
4. Copy the **Client id** from the same page.

Ask the user to paste the **Client id** and **Client secret** back to you. (The
secret is sensitive; it will be stored only in a local, gitignored `.env` file.)

> Redirect URI must match exactly. If you later change the port (see Troubleshooting),
> add the new `http://localhost:<port>/callback` here too. Xero allows several
> redirect URIs on one app.

## Step 3 — Set up the environment [agent]

From the repo directory, run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then write the user's credentials into `.env`:

```
XERO_CLIENT_ID=<the client id they gave you>
XERO_CLIENT_SECRET=<the client secret they gave you>
```

## Step 4 — Choose scopes and build the authorize command [agent]

Ask the user **what they want to do with the data**. Then translate their answer
into scopes using the guide below, and show them (or run) the command.

Rules:

- A `.read` scope is **read-only**. The same scope without `.read` allows **create
  and update**. Request both if they need to read and write the same data.
- Request the least you need. Scopes are additive: you can re-run later to add more.
- `offline_access` is added automatically. Do not include it.

**What the user wants → scopes:**

| The user wants to... | Scopes |
|----------------------|--------|
| Read invoices, credit notes, quotes | `accounting.invoices.read` |
| Read **and create/update** invoices | `accounting.invoices.read accounting.invoices` |
| Read payments | `accounting.payments.read` |
| Read bank transactions | `accounting.banktransactions.read` |
| Read manual journals | `accounting.manualjournals.read` |
| Read contacts (customers/suppliers) | `accounting.contacts.read` |
| Read + manage contacts | `accounting.contacts.read accounting.contacts` |
| Read chart of accounts, tax rates, org settings | `accounting.settings.read` |
| Read financial reports (P&L, balance sheet, etc.) | `accounting.reports.profitandloss.read accounting.reports.balancesheet.read accounting.reports.trialbalance.read` |
| Read GST / tax reports | `accounting.reports.taxreports.read` |
| Read budgets | `accounting.budgets.read` |
| Read file attachments | `accounting.attachments.read` |
| Payroll (employees, payruns, timesheets) | `payroll.employees.read payroll.payruns.read payroll.timesheets.read` |
| Files library | `files.read` |
| Fixed assets | `assets.read` |
| Projects | `projects.read` |

The full catalog of valid scopes is in [`scopes.txt`](./scopes.txt). `authorize.py`
validates against it and stops on a typo.

**Example.** The user says "I want to read and create invoices, and read contacts."
Build:

```bash
python authorize.py --scopes accounting.invoices.read accounting.invoices accounting.contacts.read
```

With no `--scopes`, a read-only accounting starter set is used.

## Step 5 — Authorize in the browser [user, or agent if same machine]

Run the command from Step 4. It prints the scopes, opens the browser, and waits.
The user logs in, picks the organisation, and clicks **Allow access**. If the user's
login can see the **Demo Company**, the script selects it automatically for safe
first testing.

Result: a local `tokens.json` (gitignored, permissions 600).

## Step 6 — Verify [agent]

```bash
python demo.py
```

It refreshes the token (persisting the rotated refresh token first), then reads a
few records using whatever scopes were granted. If any section prints data, the
connection works and refreshes itself from here on.

To switch from the Demo Company to the real organisation, re-run the Step 4 command
and pick that organisation when prompted (or delete `tokens.json` and re-authorize).

---

## Using it in code

```python
from xero_client import XeroClient

client = XeroClient()                     # loads .env + tokens.json
org = client.get("Organisation")          # any GET under api.xro/2.0
invoices = client.get("Invoices", params={"page": 1})
```

`client.get(...)` refreshes only when needed and **persists the rotated refresh
token to disk before making the API call**. You never handle tokens by hand.

## The one rule that keeps this alive

Xero **rotates the refresh token on every refresh**; the old one dies immediately.
If a new token is lost before it is saved, the connection is dead and you must
re-run `authorize.py`. `xero_client.py` prevents this by writing `tokens.json`
atomically (temp file → fsync → `os.replace`) the instant it receives the new token.

Do not:

- **Run two copies at once** against the same `tokens.json` (concurrent refreshes
  race; one wins and invalidates the other). Single writer only.
- **Restore an old `tokens.json` from backup** and use it (its refresh token is stale).

## Good to know

- **Refresh token lifetime**: ~60 days of inactivity. Run something at least every
  couple of weeks to keep it warm, or re-authorize when it lapses.
- **Access token lifetime**: 30 minutes. Handled automatically.
- **Rate limits**: 60 calls/min and 5,000/day per tenant. `get()` respects
  `Retry-After` on a 429.
- **Adding scopes later**: re-run `authorize.py --scopes ...` with the new list.
  Consent is additive, so existing access is kept.
- **Some scope families need certification** (parts of Payroll, Finance API,
  Practice Manager). Standard accounting read/write scopes do not.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Address already in use` on the callback port | Another service holds the port. Set `XERO_REDIRECT_PORT` in `.env` to a free port, and register `http://localhost:<port>/callback` in the Xero app portal. |
| Browser shows `redirect_uri` error | The redirect URI in the portal does not match. Make it exactly `http://localhost:<port>/callback` for the port you use. |
| `invalid_grant` on refresh | The refresh token is dead (unused >60 days, revoked, or a rotation was lost). Re-run `authorize.py`. |
| `Unknown scope(s)` | A scope is misspelled. Check it against `scopes.txt`. |
| A `demo.py` section is "skipped" | That data's scope was not granted. Re-run `authorize.py` with the scope added if you need it. |
