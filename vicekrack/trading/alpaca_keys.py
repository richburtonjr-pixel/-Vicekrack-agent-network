"""Alpaca PAPER keys for the Trading Floor: environment first, then Windows Credential Manager.

"Save Alpaca Paper Keys.bat" stores the keys under ViceKrack/APCA_API_KEY_ID and
ViceKrack/APCA_API_SECRET_KEY. The older code reads environment variables only, so this module
builds a PRIVATE copy of the environment that also carries the saved keys under every name
ViceKrack uses (the market-data adapters' APCA_* and the paper broker's ALPACA_PAPER_*).

- os.environ is never changed; nothing is written, printed, logged or hashed.
- Only the source is ever reported ("environment", "windows_vault" or "missing"), never a value.
- Paper keys start with "PK"; anything else is refused here (live keys are never used).
"""

import os

VAULT_KEY_ID = "ViceKrack/APCA_API_KEY_ID"
VAULT_SECRET = "ViceKrack/APCA_API_SECRET_KEY"
KEY_NAMES = ("APCA_API_KEY_ID", "ALPACA_PAPER_API_KEY_ID")
SECRET_NAMES = ("APCA_API_SECRET_KEY", "ALPACA_PAPER_API_SECRET_KEY")


def _vault_loader():
    from ..mobile.winkey import load_named
    return load_named


def alpaca_environ(base=None, loader=None):
    """(environ copy, source). source is 'environment', 'windows_vault', 'missing' or 'not_paper'."""
    environ = dict(os.environ if base is None else base)
    key = next((str(environ[n]).strip() for n in KEY_NAMES if str(environ.get(n, "")).strip()), "")
    secret = next((str(environ[n]).strip() for n in SECRET_NAMES if str(environ.get(n, "")).strip()), "")
    source = "environment" if key and secret else None
    if source is None:
        try:
            load = loader or _vault_loader()
            key, secret = (load(VAULT_KEY_ID) or "").strip(), (load(VAULT_SECRET) or "").strip()
        except Exception:                                   # noqa: BLE001 - not Windows, or the vault refused
            key, secret = "", ""
        source = "windows_vault" if key and secret else "missing"
    if source == "missing":
        return environ, source
    if not key.startswith("PK"):
        return environ, "not_paper"
    for name in KEY_NAMES:
        environ[name] = key
    for name in SECRET_NAMES:
        environ[name] = secret
    return environ, source
