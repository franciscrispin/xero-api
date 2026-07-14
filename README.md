# xero-api

A small, safe Xero API integration in Python. Authorize once in the browser, then
the scripts refresh their own tokens forever. Scopes are chosen at setup time, so
the same tool works for read-only reporting or full read-write automation.

Built for the standard OAuth 2.0 authorization-code flow with a **Web app**, which
is the flow that works for every region (including Singapore, where Custom
Connections are not available).

## Why not the official Xero MCP server?

Xero publishes an official MCP server, [`xeroapi/xero-mcp-server`](https://github.com/xeroapi/xero-mcp-server),
which would otherwise be the easy path. It authenticates using **Custom Connections**,
and Custom Connections are only available to Xero organisations in **Australia, New
Zealand, the United Kingdom, and the United States**. Organisations in any other
region (for example Singapore) cannot create a Custom Connection, so the official
MCP server does not work for them.

This project uses the standard **Web app** authorization-code flow instead, which is
available in every region. You authorize once in a browser and the tokens refresh
themselves after that.

---

## For the agent setting this up

This README is written so an AI agent can walk a first-time user through setup.
The user is assumed to have a Xero account but **no developer account yet**.

Split of work:

- **You (the agent) do**: create the Python environment, install dependencies,
  create the empty `.env` file, build the authorize command, run `demo.py`, and
  diagnose errors.
- **The user must do in a browser** (you cannot): create the developer account,
  create the app, generate the client secret, and click "Allow access" during
  consent. Give them the exact values to enter, then wait.
- **The client secret**: never ask the user to paste the client secret (or the
  client id) into the chat. A pasted secret is sent to the model provider and can
  be kept in conversation logs. The user types it straight into the local `.env`
  file, which you never read. See Step 3.
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

Ask the user to keep the **Client id** and **Client secret** for the next step.
**Do not ask them to paste either value into the chat.** They will type them
directly into the local `.env` file, which stays on their machine and is gitignored.

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

Now the **user** puts their credentials into `.env` themselves. Do not ask for the
values; hand them the instruction and let them edit the file. Any of these works:

- Open `.env` in a text editor and fill in the two lines:
  ```
  XERO_CLIENT_ID=...their client id...
  XERO_CLIENT_SECRET=...their client secret...
  ```
- Or, from a terminal, append the secret without it showing on screen (works in
  bash and zsh):
  ```bash
  printf 'Paste client secret (hidden): '; read -rs XERO_CLIENT_SECRET; echo
  echo "XERO_CLIENT_SECRET=$XERO_CLIENT_SECRET" >> .env; unset XERO_CLIENT_SECRET
  ```
  (The client id is not secret, so you can just type it into the file.)

You (the agent) do not need to see these values. If the user has already pasted a
secret into a chat somewhere, tell them to rotate it: Xero app **Configuration >
Generate a secret** issues a new one and invalidates the old.

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

**If the user wants everything**, don't hand-type every scope from the table above —
run:

```bash
python authorize.py --all-scopes
```

This requests every scope in `scopes.txt` except `app.connections`, which Xero
rejects outright for this app's flow (confirmed empirically; see `EXCLUDED_FROM_ALL`
in `authorize.py`). Nothing this project uses needs `app.connections`, so the
exclusion costs nothing. Everything else — including the full Payroll set — has been
verified to authorize together in one consent round once `app.connections` is out of
the request.

## Step 5 — Authorize in the browser [user, or agent if same machine]

Run the command from Step 4. It prints the scopes, opens the browser, and waits.
The user logs in, picks the organisation(s), and clicks **Allow access**. If the
user's login can see the **Demo Company**, the script selects it automatically for
safe first testing.

Add `--profile <name>` to store the authorization under a named profile (e.g.
`--profile demo` or `--profile real`); existing profiles are preserved and the new
one becomes active. With no `--profile`, the name is inferred (`demo` for the Demo
Company, else `real`). See [Profiles](#profiles-demo-vs-real) below.

Result: a local `tokens.json` (gitignored, permissions 600) holding one or more
named profiles.

## Step 6 — Verify [agent]

```bash
python demo.py                 # uses the active profile
python demo.py --profile real  # uses a specific profile
```

It refreshes the token (persisting the rotated refresh token first), then reads a
few records using whatever scopes were granted. If any section prints data, the
connection works and refreshes itself from here on.

To add a second organisation as its own profile, re-run the Step 4 command with a
different `--profile` name (e.g. `--profile real`). See [Profiles](#profiles-demo-vs-real).

---

## Profiles (demo vs real)

`tokens.json` holds one or more **named profiles**, each an independent
authorization with its **own rotating refresh token**. This lets you keep, say, a
`demo` profile and a `real` profile side by side and switch between them.

```json
{
  "version": 2,
  "active": "real",
  "profiles": {
    "demo": { "access_token": "…", "refresh_token": "…", "tenant_id": "…",
              "tenant_name": "Demo Company (Global)", "connections": [ … ] },
    "real": { "…": "…", "tenant_name": "playertwos" }
  }
}
```

Manage them with `xero_profiles.py`:

```bash
python xero_profiles.py                 # list profiles; * marks the active one
python xero_profiles.py use real        # set the active profile
python xero_profiles.py tenant demo     # switch the active org within a profile
python xero_profiles.py tenant "Demo Company (Global)" --profile demo
```

Add a new profile by authorizing into it (existing profiles are preserved):

```bash
python authorize.py --profile real --all-scopes            # opens the browser
python authorize.py --profile real --tenant playertwos …   # preselect the org, unattended
```

**Two things to understand about scope of access:**

- **Independent refresh tokens.** Refreshing one profile never rotates or
  invalidates another's token. This is the reason to use separate profiles rather
  than one shared token.
- **Profiles are *not* org isolation.** Xero grants connections at the **app + org**
  level, shared across every authorization of the same app. If your login can see
  several orgs, Xero's consent screen pre-selects them all and you cannot deselect
  an already-connected org. So every profile of one app reaches the *same* set of
  orgs (`connections`); the profile's `tenant_id` just picks the default active one,
  and `tenant` can repoint it to any connected org. **A profile name is a
  convention, not a wall.** For a token that *physically cannot* reach another org,
  create a separate Xero app (its own `client_id`/`client_secret`) connected only to
  that org.

Legacy single-tenant `tokens.json` files (from before profiles) are migrated
automatically on first load: the old token set becomes one profile (`demo` if its
tenant looks like the Demo Company, else `default`).

---

## Using it in code

```python
from xero_client import XeroClient

client = XeroClient()                     # active profile; loads .env + tokens.json
client = XeroClient(profile="real")       # or a specific profile

org = client.get("Organisation")          # any GET under api.xro/2.0
invoices = client.get("Invoices", params={"page": 1})
new_inv = client.post("Invoices", {       # POST/create (needs a write scope)
    "Invoices": [{
        "Type": "ACCREC",
        "Contact": {"ContactID": "…"},
        "LineItems": [{"Description": "Item", "Quantity": 1,
                        "UnitAmount": 100.0, "AccountCode": "200"}],
        "Status": "DRAFT",
    }],
})

client.switch_tenant("playertwos")        # repoint this profile at another
                                          # connected org (persisted, no re-auth)
```

`client.get(...)` / `client.post(...)` refresh only when needed and **persist the
rotated refresh token to disk before making the API call**. You never handle tokens
by hand. `client.prof` is the active profile's dict (e.g. `client.prof["tenant_name"]`).

## The one rule that keeps this alive

Xero **rotates the refresh token on every refresh**; the old one dies immediately.
If a new token is lost before it is saved, the connection is dead and you must
re-run `authorize.py`. `xero_client.py` prevents this by writing `tokens.json`
atomically (temp file → fsync → `os.replace`) the instant it receives the new token.

Do not:

- **Run two copies at once** against the same `tokens.json` — **even on different
  profiles**. Each save rewrites the whole file, so two processes that both load,
  refresh, and save will have the second clobber the first's rotated token (a lost
  rotation = a dead connection). Single writer per `tokens.json`, regardless of
  profile.
- **Restore an old `tokens.json` from backup** and use it (its refresh tokens are stale).

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
| `access_denied: Requested wrong apps scopes` | One (or more) requested scopes isn't valid for this app/org — not the OAuth config. `app.connections` is a known offender (see `EXCLUDED_FROM_ALL` in `authorize.py`); drop it first. If it persists with a custom `--scopes` list, bisect: run half the list, then the other half, and recurse into whichever half fails to find the bad scope. `authorize.py` prints this same advice when it hits the error. |
