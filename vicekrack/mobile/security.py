"""Pairing, phone sessions, the PIN and the daily spending cap.

- Pairing: the PC shows a 6-digit code (10 minutes, 5 tries). Entering it on the phone creates a device
  token (256 bits) kept in an HttpOnly cookie. Only its SHA-256 is saved. Phones expire after 90 days and
  can be unpaired from the phone or the PC.
- CSRF: every phone POST also carries a header token derived from the device token (HMAC), which other
  websites cannot read.
- PIN: 4 to 8 digits, stored as a salted scrypt hash. Five wrong PINs lock paid actions for 15 minutes.
- Daily cap: each paid phone approval reserves its estimate (the high end) against today's cap in the
  owner's time zone before it is forwarded. A repeated request ID never counts twice.
"""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..errors import NetworkError

PAIR_MINUTES, PAIR_TRIES = 10, 5
DEVICE_DAYS = 90
PIN_TRIES, PIN_LOCK_MINUTES = 5, 15
PIN = re.compile(r"^[0-9]{4,8}$")
CODE = re.compile(r"^[0-9]{6}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
KEEP_DAYS = 31


def utcnow():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- PIN
def hash_pin(pin):
    if not isinstance(pin, str) or not PIN.match(pin):
        raise NetworkError("invalid_pin", "The PIN must be 4 to 8 digits.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, dklen=32, **SCRYPT)
    return {"salt": salt.hex(), "hash": digest.hex(), **SCRYPT}


def _pin_matches(record, pin):
    digest = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(record["salt"]), dklen=32,
                            n=record["n"], r=record["r"], p=record["p"])
    return hmac.compare_digest(digest.hex(), record["hash"])


def check_pin(store, pin, now=None):
    """Raise unless `pin` is right and paid actions are not locked. Wrong guesses count toward the lock."""
    now = now or utcnow()
    record = store.settings().get("pin")
    if not record:
        raise NetworkError("pin_not_set", "Set a PIN on the PC first (Set up ViceKrack Mobile).")
    guard = store.read("pin_guard.json", {})
    locked_until = guard.get("locked_until")
    if locked_until and _parse(locked_until) > now:
        raise NetworkError("pin_locked", "Too many wrong PINs. Paid actions are locked for a few minutes.")
    if isinstance(pin, str) and PIN.match(pin) and _pin_matches(record, pin):
        store.write("pin_guard.json", {"wrong": 0, "locked_until": None})
        return True
    wrong = int(guard.get("wrong") or 0) + 1
    if wrong >= PIN_TRIES:
        store.write("pin_guard.json", {"wrong": 0, "locked_until": _iso(now + timedelta(minutes=PIN_LOCK_MINUTES))})
        raise NetworkError("pin_locked", "Too many wrong PINs. Paid actions are locked for 15 minutes.")
    store.write("pin_guard.json", {"wrong": wrong, "locked_until": None})
    raise NetworkError("pin_wrong", "That PIN is not right. Nothing was sent.")


# ---------------------------------------------------------------- pairing and devices
def new_pairing_code(store, now=None):
    now = now or utcnow()
    code = f"{secrets.randbelow(10 ** 6):06d}"
    store.write("pairing.json", {"code_sha256": _sha(code), "expires_at": _iso(now + timedelta(minutes=PAIR_MINUTES)),
                                 "tries_left": PAIR_TRIES})
    return code


def csrf_for(token):
    return hmac.new(token.encode(), b"vicekrack-mobile-csrf-v1", hashlib.sha256).hexdigest()


def redeem_pairing_code(store, code, name, now=None):
    """(token, device) for a right code; the code is then used up. Wrong codes use up tries."""
    now = now or utcnow()

    def change(pairing):
        if not pairing or _parse(pairing["expires_at"]) <= now or pairing.get("tries_left", 0) <= 0:
            return {}, ("expired", None)
        if not (isinstance(code, str) and CODE.match(code) and hmac.compare_digest(_sha(code), pairing["code_sha256"])):
            pairing["tries_left"] -= 1
            return (pairing if pairing["tries_left"] > 0 else {}), ("wrong", None)
        return {}, ("ok", None)

    outcome, _ = store.update("pairing.json", {}, change)
    if outcome == "expired":
        raise NetworkError("pairing_code_expired", "No active pairing code. Run “Pair my iPhone” on the PC.")
    if outcome == "wrong":
        raise NetworkError("pairing_code_wrong", "That code is not right.")
    token = secrets.token_urlsafe(32)
    device = {"device_id": "dev-" + secrets.token_hex(6), "name": name, "token_sha256": _sha(token),
              "created_at": _iso(now), "last_seen_at": _iso(now),
              "expires_at": _iso(now + timedelta(days=DEVICE_DAYS))}

    def add(devices):
        devices.setdefault("devices", []).append(device)
        return devices, None

    store.update("devices.json", {"devices": []}, add)
    return token, _public(device)


def _public(device):
    return {k: device[k] for k in ("device_id", "name", "created_at", "last_seen_at", "expires_at")}


def authenticate(store, token, now=None):
    """The paired device for this cookie token, or None (unknown, revoked or expired)."""
    if not isinstance(token, str) or not TOKEN.match(token):
        return None
    now = now or utcnow()
    digest = _sha(token)
    for device in store.read("devices.json", {"devices": []}).get("devices", []):
        if hmac.compare_digest(device["token_sha256"], digest):
            if _parse(device["expires_at"]) <= now:
                return None
            if now - _parse(device["last_seen_at"]) > timedelta(minutes=5):
                def touch(devices):
                    for row in devices.get("devices", []):
                        if row["device_id"] == device["device_id"]:
                            row["last_seen_at"] = _iso(now)
                    return devices, None
                store.update("devices.json", {"devices": []}, touch)
            return _public(device)
    return None


def list_devices(store):
    return [_public(d) for d in store.read("devices.json", {"devices": []}).get("devices", [])]


def revoke(store, device_id=None):
    """Unpair one phone, or every phone when device_id is None. Returns how many were removed."""
    def change(devices):
        rows = devices.get("devices", [])
        kept = [] if device_id is None else [d for d in rows if d["device_id"] != device_id]
        return {"devices": kept}, len(rows) - len(kept)
    return store.update("devices.json", {"devices": []}, change)


# ---------------------------------------------------------------- daily cap
def today_key(store, now=None):
    now = now or utcnow()
    return now.astimezone(ZoneInfo(store.settings()["timezone"])).strftime("%Y-%m-%d")


def spent_today(store, now=None):
    day = store.read("spend.json", {"days": {}}).get("days", {}).get(today_key(store, now), [])
    return round(sum(row["estimate_usd"] for row in day if row["state"] != "released"), 4)


def reserve(store, request_id, *, device, action, ref, estimate_usd, now=None):
    """Hold `estimate_usd` against today's cap for this request. A repeated request ID is not counted again."""
    now = now or utcnow()
    cap = float(store.settings()["daily_cap_usd"])
    day = today_key(store, now)

    def change(ledger):
        days = ledger.setdefault("days", {})
        for rows in days.values():
            for row in rows:
                if row["request_id"] == request_id:
                    return ledger, ("repeat", row)
        rows = days.setdefault(day, [])
        total = sum(r["estimate_usd"] for r in rows if r["state"] != "released")
        if total + estimate_usd > cap + 1e-9:
            return ledger, ("over", round(total, 4))
        row = {"request_id": request_id, "action": action, "ref": ref, "estimate_usd": round(estimate_usd, 4),
               "state": "reserved", "device_id": device["device_id"], "at": _iso(now)}
        rows.append(row)
        for old in sorted(days)[:-KEEP_DAYS]:
            days.pop(old)
        return ledger, ("ok", row)

    outcome, detail = store.update("spend.json", {"days": {}}, change)
    if outcome == "over":
        left = max(0.0, cap - detail)
        raise NetworkError("daily_cap_reached", f"This would pass your ${cap:.2f} daily phone limit "
                                                f"(${left:.2f} left today). Approve it on the PC instead.")
    return outcome, detail


def settle(store, request_id, state):
    """`kept` (it was sent or may have been billed) or `released` (refused before anything was sent)."""
    def change(ledger):
        for rows in ledger.get("days", {}).values():
            for row in rows:
                if row["request_id"] == request_id and row["state"] == "reserved":
                    row["state"] = state
        return ledger, None
    store.update("spend.json", {"days": {}}, change)
