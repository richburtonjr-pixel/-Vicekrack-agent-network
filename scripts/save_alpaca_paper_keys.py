"""Save the Alpaca PAPER trading keys in Windows Credential Manager. Run by "Save Alpaca Paper Keys.bat".

- Nothing is typed into a file, printed, or sent anywhere. Both keys are entered with hidden input.
- You must type PAPER to confirm the keys are paper keys. Live keys are refused.
- Only the names are printed, never the values.
"""

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KEY_ID_TARGET = "ViceKrack/APCA_API_KEY_ID"
SECRET_TARGET = "ViceKrack/APCA_API_SECRET_KEY"
MAX_LENGTH = 128


def check_inputs(confirm, key_id, secret):
    """Return None when the inputs are acceptable, otherwise a fixed error message."""
    if (confirm or "").strip() != "PAPER":
        return "Type PAPER exactly to confirm these are paper keys. Nothing was saved."
    for value in (key_id, secret):
        if not value or len(value) > MAX_LENGTH or any(ch.isspace() for ch in value):
            return "One of the keys is empty or has spaces in it. Nothing was saved."
    if key_id == secret:
        return "The key ID and secret must be different. Nothing was saved."
    return None


def main():
    from vicekrack.mobile.winkey import save_named
    print("Save Alpaca PAPER keys (paper only, no real money).")
    print("Keys stay hidden while you paste. Right-click pastes.\n")
    confirm = input("Type PAPER to confirm these are paper keys: ")
    key_id = getpass.getpass("Paste the Alpaca PAPER key ID and press Enter: ")
    secret = getpass.getpass("Paste the Alpaca PAPER secret key and press Enter: ")
    problem = check_inputs(confirm, key_id, secret)
    if problem:
        print(problem)
        return 1
    try:
        save_named(KEY_ID_TARGET, key_id.strip(), label="Alpaca key ID")
        save_named(SECRET_TARGET, secret.strip(), label="Alpaca secret key")
    except Exception as error:  # fixed messages come from winkey; nothing else is shown
        print(getattr(error, "message", "The keys could not be saved. Nothing was shown."))
        return 1
    print("Saved the Alpaca PAPER keys in Windows Credential Manager.")
    print("Names saved: ViceKrack/APCA_API_KEY_ID and ViceKrack/APCA_API_SECRET_KEY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
