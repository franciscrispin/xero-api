"""Xero OAuth 2.0 API client: an importable library and the `xero` CLI.

Library use:

    from xero_api import XeroClient

    client = XeroClient()                    # active profile, credentials from
                                             # ~/.config/xero-api/
    client = XeroClient(profile="real")      # a specific profile
    contacts = client.get("Contacts", params={"where": 'Name=="Acme Ltd"'})
    client.post("Invoices", {"Invoices": [...]})

Credentials (.env) and the rotating token store (tokens.json) live in a user config
directory -- $XERO_HOME, else $XDG_CONFIG_HOME/xero-api, else ~/.config/xero-api --
never in the working directory. See `config_dir()`.
"""

from .client import (
    API_BASE,
    XeroClient,
    XeroError,
    config_dir,
    default_env_path,
    default_store_path,
    list_profiles,
    load_store,
    save_store,
)

__version__ = "2.0.0"

__all__ = [
    "API_BASE",
    "XeroClient",
    "XeroError",
    "config_dir",
    "default_env_path",
    "default_store_path",
    "list_profiles",
    "load_store",
    "save_store",
    "__version__",
]
