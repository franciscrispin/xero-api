# xero-api

A small, safe Xero API client in Python: a **`xero` command-line tool** and an
**importable library**. Authorize once in the browser, then it refreshes its own
tokens forever. Scopes are chosen at setup time, so the same tool works for
read-only reporting or full read-write automation.

Built for the standard OAuth 2.0 authorization-code flow with a **Web app**, which
is the flow that works for every region (including Singapore, where Custom
Connections are not available).

```bash
pip install -e .
xero auth --profile real --all-scopes
xero get Contacts --where 'Name=="Acme Ltd"' | jq '.Contacts[].ContactID'
```

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

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .          # installs the `xero` command and the `xero_api` package
```

Both work from any directory afterwards:

```bash
cd /anywhere
xero --help
python -c "from xero_api import XeroClient; print(XeroClient)"
```

Requires Python 3.9+. Dependencies (`requests`, `python-dotenv`) are declared in
`pyproject.toml`; there is no `requirements.txt`.

## Where credentials live

Nothing secret lives in this repo. Both files sit in a user config directory:

| File | What it is | Permissions |
|------|-----------|-------------|
| `.env` | `XERO_CLIENT_ID` / `XERO_CLIENT_SECRET` from your Xero app | `0600` |
| `tokens.json` | the rotating token store, one entry per profile | `0600` |

The directory is created `0700`. It is resolved in this order — **highest wins**:

1. an explicit `--store` / `--env` flag
2. `$XERO_HOME`
3. `$XDG_CONFIG_HOME/xero-api`, else `~/.config/xero-api`

There is deliberately **no fallback to the working directory**. A stale `tokens.json`
sitting in some checkout and silently shadowing the real credentials is a genuinely
confusing bug, so a missing store is a hard error that names `xero auth`:

```
Error: No Xero token store at /Users/you/.config/xero-api/tokens.json.
Authorize first:  xero auth --profile <name>
```

---

## CLI reference

### Reads are separable from writes

This is a deliberate design constraint, not an accident of layout. **Every read is
reachable without typing a verb that can also write**, so an agent's permission
system — which allowlists by literal command prefix — can auto-approve reads while
still prompting a human for anything else:

| Command | Touches Xero? | Can it change anything? |
|---------|---------------|-------------------------|
| `xero get …` | reads only | **no** — there is no write flag, and no read path falls through to a POST |
| `xero profiles list` | no, local file only | no |
| `xero profiles use` / `tenant` | no (validates locally) | rewrites the local store only |
| `xero post …` | yes | **yes** |
| `xero auth …` | yes (OAuth) | rewrites the local store |

So `Bash(xero get:*)` is safe to allowlist, while `xero post` and `xero auth` stay
behind a prompt.

`get` and `post` are **generic passthroughs over endpoint names**, not per-endpoint
subcommands. There is no `xero invoices list --unpaid`, on purpose: that path has to
grow to cover the whole Xero API and never finishes. A passthrough covers all of it
today and emits JSON for `jq`.

### `xero get` — read anything

```bash
xero get Organisation
xero get Contacts --where 'Name=="Acme Ltd"'
xero get Contacts --where 'Name.Contains("acme")'
xero get Invoices --params page=1 --params pageSize=5
xero get Invoices --where 'InvoiceNumber=="INV-0042"'
xero get Invoices --where 'Status=="AUTHORISED"' --params order=Date DESC
xero get Reports/ProfitAndLoss
xero get 'Invoices/9c8e…-uuid'
```

The endpoint is anything under `https://api.xero.com/api.xro/2.0/` (or a full URL).
Output is JSON on stdout, so pipe it:

```bash
xero get Invoices --where 'Status=="DRAFT"' | jq -r '.Invoices[] | "\(.InvoiceNumber)\t\(.Total)"'
```

Two convenience listings, and deliberately no more — item codes and branding theme
IDs are the two things you have to look up by hand before writing an invoice:

```bash
xero get --list-items             # code, unit price, name
xero get --list-branding-themes   # BrandingThemeID, name
```

### `xero post` — write

```bash
xero post Invoices --data @invoice.json
xero post Contacts --data '{"Contacts":[{"Name":"Acme Ltd"}]}'
```

`--data` takes inline JSON or `@path/to/file.json`. Needs a non-`.read` scope.

### `xero profiles` — switch organisations

```bash
xero profiles list                          # * marks the active profile
xero profiles use real                      # set the active profile
xero profiles tenant "Acme Ltd"             # switch org within the active profile
xero profiles tenant "Acme Ltd" --profile real
```

### `xero auth` — authorize

```bash
xero auth                                   # read-only accounting starter scopes
xero auth --profile real --all-scopes
xero auth --scopes accounting.invoices.read accounting.contacts.read
xero auth --profile real --tenant "Acme Ltd" --all-scopes   # unattended org choice
```

### Flags every subcommand accepts

| Flag | Meaning |
|------|---------|
| `--profile NAME` | which stored authorization to use (default: the active one) |
| `--store PATH` | token store location (overrides `$XERO_HOME` and the config dir) |
| `--env PATH` | credentials `.env` location (same precedence) |

---

## Using it as a library

```python
from xero_api import XeroClient

client = XeroClient()                          # active profile, credentials from
                                               # ~/.config/xero-api/
client = XeroClient(profile="real")            # a specific profile
client = XeroClient(profile="real",            # or point at another store entirely
                    env_path="/secure/.env",
                    store_path="/secure/tokens.json")

org = client.get("Organisation")               # any GET under api.xro/2.0
invoices = client.get("Invoices", params={"page": 1})
contacts = client.get("Contacts", params={"where": 'Name=="Acme Ltd"'})

new_inv = client.post("Invoices", {            # POST/create (needs a write scope)
    "Invoices": [{
        "Type": "ACCREC",
        "Contact": {"ContactID": "…"},
        "LineItems": [{"Description": "Item", "Quantity": 1,
                       "UnitAmount": 100.0, "AccountCode": "200"}],
        "Status": "DRAFT",
    }],
})

client.switch_tenant("Acme Ltd")               # repoint this profile at another
                                               # connected org (persisted, no re-auth)
```

The constructor signature is `XeroClient(profile=None, env_path=None, store_path=None)`.
All three are optional; the defaults resolve through the config directory described
above.

`client.get(...)` / `client.post(...)` refresh only when needed and **persist the
rotated refresh token to disk before making the API call**. You never handle tokens
by hand. `client.prof` is the active profile's dict (e.g. `client.prof["tenant_name"]`),
and failures raise `xero_api.XeroError`.

Also exported: `config_dir()`, `default_env_path()`, `default_store_path()`,
`load_store()`, `save_store()`, `list_profiles()`.

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
    "real": { "…": "…", "tenant_name": "Acme Ltd" }
  }
}
```

Add a new profile by authorizing into it (existing profiles are preserved and the
new one becomes active):

```bash
xero auth --profile real --all-scopes
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
  and `xero profiles tenant` can repoint it to any connected org. **A profile name
  is a convention, not a wall.** For a token that *physically cannot* reach another
  org, create a separate Xero app (its own `client_id`/`client_secret`) connected
  only to that org.

---

## First-time setup

This section is written so an AI agent can walk a first-time user through it. The
user is assumed to have a Xero account but **no developer account yet**.

Split of work:

- **You (the agent) do**: create the Python environment, install the package, create
  the config directory, build the `xero auth` command, and diagnose errors.
- **The user must do in a browser** (you cannot): create the developer account,
  create the app, generate the client secret, and click "Allow access" during
  consent. Give them the exact values to enter, then wait.
- **The client secret**: never ask the user to paste the client secret (or the
  client id) into the chat. A pasted secret is sent to the model provider and can
  end up in logs. Have them type it straight into `~/.config/xero-api/.env`.

### Step 1 — Create the Xero developer account [user]

1. Open https://developer.xero.com and click **Log in** (top right).
2. Log in with the existing Xero email and password.
3. Accept the developer terms if prompted.

That is the whole "developer account" step. It uses the existing Xero login; no
separate signup.

### Step 2 — Create a Web app [user]

Go to https://developer.xero.com/app/manage, click **New app**, and enter exactly:

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

> The redirect URI must match exactly. If you later change the port (see
> Troubleshooting), add the new `http://localhost:<port>/callback` here too. Xero
> allows several redirect URIs on one app.

### Step 3 — Set up the environment [agent]

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

mkdir -p ~/.config/xero-api && chmod 700 ~/.config/xero-api
cp .env.example ~/.config/xero-api/.env && chmod 600 ~/.config/xero-api/.env
```

Now the **user** puts their credentials into `~/.config/xero-api/.env` themselves.
Do not ask for the values; hand them the instruction and let them edit the file:

- Open the file in a text editor and fill in the two lines:
  ```
  XERO_CLIENT_ID=...their client id...
  XERO_CLIENT_SECRET=...their client secret...
  ```
- Or, from a terminal, append the secret without it showing on screen (bash/zsh):
  ```bash
  printf 'Paste client secret (hidden): '; read -rs S; echo
  echo "XERO_CLIENT_SECRET=$S" >> ~/.config/xero-api/.env; unset S
  ```
  (The client id is not secret, so it can just be typed into the file.)

You (the agent) do not need to see these values. If the user has already pasted a
secret into a chat somewhere, tell them to rotate it: Xero app **Configuration >
Generate a secret** issues a new one and invalidates the old.

### Step 4 — Choose scopes [agent]

Ask the user **what they want to do with the data**, then translate the answer into
scopes.

Rules:

- A `.read` scope is **read-only**. The same scope without `.read` allows **create
  and update**. Request both if they need to read and write the same data.
- Request the least you need. Scopes are additive: you can re-run later to add more.
- `offline_access` is added automatically. Do not include it.

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

The full catalog of valid scopes is [`xero_api/scopes.txt`](./xero_api/scopes.txt),
shipped as package data and resolved relative to the installed package — not the
working directory. `xero auth` validates against it and stops on a typo.

**Example.** The user says "I want to read and create invoices, and read contacts":

```bash
xero auth --scopes accounting.invoices.read accounting.invoices accounting.contacts.read
```

With no `--scopes`, a read-only accounting starter set is used.

**If the user wants everything**, don't hand-type every scope — run:

```bash
xero auth --all-scopes
```

This requests every scope in `scopes.txt` except `app.connections`, which Xero
rejects outright for this app's flow (confirmed empirically; see `EXCLUDED_FROM_ALL`
in `xero_api/auth.py`). Nothing this project uses needs `app.connections`, so the
exclusion costs nothing. Everything else — including the full Payroll set — has been
verified to authorize together in one consent round once `app.connections` is out of
the request.

### Step 5 — Authorize in the browser [user, or agent if same machine]

Run the command from Step 4. It prints the scopes, opens the browser, and waits.
The user logs in, picks the organisation(s), and clicks **Allow access**. If the
user's login can see the **Demo Company**, it is selected automatically for safe
first testing.

Add `--profile <name>` to store the authorization under a named profile (e.g.
`--profile demo` or `--profile real`). With no `--profile`, the name is inferred
(`demo` for the Demo Company, else `real`).

Result: `~/.config/xero-api/tokens.json`, mode `0600`, holding one or more named
profiles.

### Step 6 — Verify [agent]

```bash
xero profiles list            # local only, no network
xero get Organisation         # one read against the active profile
```

If `xero get Organisation` prints the org, the connection works and refreshes itself
from here on.

---

## The one rule that keeps this alive

Xero **rotates the refresh token on every refresh**; the old one dies immediately.
If a new token is lost before it is saved, the connection is dead and you must
re-run `xero auth`. `xero_api/client.py` prevents this by writing `tokens.json`
atomically (temp file → fsync → `os.replace`) the instant it receives the new token,
before any API call runs.

Do not:

- **Run two copies at once** against the same `tokens.json` — **even on different
  profiles**. Each save rewrites the whole file, so two processes that both load,
  refresh, and save will have the second clobber the first's rotated token (a lost
  rotation = a dead connection). Single writer per `tokens.json`, regardless of
  profile.
- **Restore an old `tokens.json` from backup** and use it (its refresh tokens are
  stale).

## Good to know

- **Refresh token lifetime**: ~60 days of inactivity. Run something at least every
  couple of weeks to keep it warm, or re-authorize when it lapses.
- **Access token lifetime**: 30 minutes. Handled automatically.
- **Rate limits**: 60 calls/min and 5,000/day per tenant. `get()` respects
  `Retry-After` on a 429.
- **Adding scopes later**: re-run `xero auth --scopes ...` with the new list.
  Consent is additive, so existing access is kept.
- **Some scope families need certification** (parts of Payroll, Finance API,
  Practice Manager). Standard accounting read/write scopes do not.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `No Xero token store at …` | You have not authorized yet, or the store is elsewhere. Run `xero auth --profile <name>`, or point at it with `$XERO_HOME` / `--store`. |
| `XERO_CLIENT_ID / XERO_CLIENT_SECRET not found` | Create `~/.config/xero-api/.env` from `.env.example` and fill in the two values, or export them in the environment. |
| `Address already in use` on the callback port | Another service holds the port. Set `XERO_REDIRECT_PORT` in `.env` to a free port, and register `http://localhost:<port>/callback` in the Xero app portal. |
| Browser shows `redirect_uri` error | The redirect URI in the portal does not match. Make it exactly `http://localhost:<port>/callback` for the port you use. |
| `invalid_grant` on refresh | The refresh token is dead (unused >60 days, revoked, or a rotation was lost). Re-run `xero auth`. |
| `Unknown scope(s)` | A scope is misspelled. Check it against `xero_api/scopes.txt`. |
| A read returns a permission error | That data's scope was not granted. Re-run `xero auth` with the scope added. |
| `access_denied: Requested wrong apps scopes` | One (or more) requested scopes isn't valid for this app/org — not the OAuth config. `app.connections` is a known offender (see `EXCLUDED_FROM_ALL` in `xero_api/auth.py`); drop it first. If it persists with a custom `--scopes` list, bisect: run half the list, then the other half, and recurse into whichever half fails to find the bad scope. `xero auth` prints this same advice when it hits the error. |
