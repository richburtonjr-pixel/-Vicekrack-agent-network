"""Saved state of the Mobile Command Center, under runtime/mobile/ (git-ignored, never published).

settings.json   remote address, owner's Tailscale login, daily cap, PIN hash (scrypt; never the PIN)
devices.json    paired phones: hashed token, hashed CSRF token, name, created/last seen/expiry
pairing.json    the one current pairing code (hashed), its expiry and attempts left
pin_guard.json  wrong-PIN counter and lockout time
spend.json      phone-approved paid actions by day (estimates, used for the daily cap)
audit.jsonl     one line per phone action: time, phone name, action, IDs, estimate, outcome.
                Never a PIN, token, cookie, key or free text.

Writes are atomic (temporary file + replace) and serialized by one lock per store.
"""

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_SETTINGS = {"contract": "mobile_settings", "version": "1.0", "remote_host": None, "owner_login": None,
                    "daily_cap_usd": 5.0, "timezone": "America/New_York", "pin": None}


class MobileStore:
    def __init__(self, root=None):
        self.folder = Path(root if root is not None else ROOT) / "runtime" / "mobile"
        self.lock = threading.RLock()

    def _path(self, name):
        return self.folder / name

    def read(self, name, default):
        with self.lock:
            try:
                with open(self._path(name), encoding="utf-8") as handle:
                    value = json.load(handle)
            except FileNotFoundError:
                return json.loads(json.dumps(default))
            if not isinstance(value, type(default)):
                raise ValueError(name)
            return value

    def write(self, name, value):
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self._path(name)
            temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)

    def update(self, name, default, change):
        """Read, change (a function returning the new value and a result), write; all under the lock."""
        with self.lock:
            value = self.read(name, default)
            new_value, result = change(value)
            self.write(name, new_value)
            return result

    def settings(self):
        merged = dict(DEFAULT_SETTINGS)
        merged.update(self.read("settings.json", {}))
        return merged

    def save_settings(self, **changes):
        def change(value):
            value = {**DEFAULT_SETTINGS, **value, **changes}
            return value, value
        return self.update("settings.json", {}, change)

    def append_audit(self, entry):
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self._path("audit.jsonl"), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n")

    def audit(self, limit=50):
        try:
            with open(self._path("audit.jsonl"), encoding="utf-8") as handle:
                lines = handle.readlines()[-limit:]
        except FileNotFoundError:
            return []
        rows = []
        for line in reversed(lines):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows
