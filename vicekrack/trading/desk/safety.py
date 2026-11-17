"""Safety switches for the agents (autonomous-trading step 1). Plain code, no AI. Simulated money only.

Emergency Stop  One switch, saved in runtime/trading/desk/emergency.json. While it is ON the agents open
                nothing new, and on the next watch cycle (within about a minute) the Executor sells every coin
                THE AGENTS hold. Your own trades are yours: they are not sold and you can still trade. It stays
                on until someone turns it off; turning it off does not buy anything back.
Peak prices     The profit trail needs the highest price seen since the agents bought a coin. The desk keeps
                that per position in runtime/trading/desk/peaks.json, keyed by account and the position's
                opened_at, so a new position never inherits an old peak. Each position's price path is kept
                too, and moved to trail-log.jsonl when it closes, for testing other trail settings.
"""

import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..contracts import ROOT, utc_now

REASON_MAX = 120


def _base(root):
    return Path(root if root is not None else ROOT) / "runtime/trading/desk"


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def _read(path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return document if isinstance(document, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


class EmergencySwitch:
    def __init__(self, root=None):
        self.path = _base(root) / "emergency.json"

    def state(self):
        """{"engaged": bool, "at", "by", "reason"}. A damaged file counts as ENGAGED (fail safe)."""
        if not self.path.exists():
            return {"engaged": False, "at": None, "by": None, "reason": None}
        document = _read(self.path)
        if document is None or not isinstance(document.get("engaged"), bool):
            return {"engaged": True, "at": None, "by": None, "reason": "switch_file_unreadable"}
        return {"engaged": document["engaged"], "at": document.get("at"), "by": document.get("by"),
                "reason": document.get("reason")}

    def set(self, engaged, by, reason=None, now=None):
        if not isinstance(engaged, bool):
            raise ValueError("engaged must be true or false")
        reason = (str(reason)[:REASON_MAX] if reason else None)
        document = {"engaged": engaged, "at": now or utc_now(), "by": str(by)[:40], "reason": reason}
        _write(self.path, document)
        return self.state()


class Peaks:
    """Highest price per agent position (for the profit trail), plus the price path for later testing.

    When a position disappears (sold), its recorded path is appended to trail-log.jsonl, so different
    profit-trail settings can be replayed on real paper-trading history (`desk-trail-test`)."""

    PATH_LIMIT = 5000

    def __init__(self, root=None):
        self.path = _base(root) / "peaks.json"
        self.log_path = _base(root) / "trail-log.jsonl"

    def update(self, account_id, positions, marks, now=None):
        """Raise each agent position's peak to its latest mark; log and forget closed ones. Returns {symbol: peak}."""
        saved = _read(self.path) or {}
        mine = saved.get(account_id) if isinstance(saved.get(account_id), dict) else {}
        out = {}
        for symbol, position in positions.items():
            if position.get("opened_by", "agents") != "agents":
                continue
            entry = mine.get(symbol) if isinstance(mine.get(symbol), dict) else None
            same = bool(entry) and entry.get("opened_at") == position["opened_at"]
            try:
                peak = Decimal(entry["peak"]) if same else None
            except (InvalidOperation, KeyError, TypeError):
                peak = None
            path = list(entry.get("path") or []) if same else []
            mark = marks.get(symbol)
            if mark is not None:
                price = Decimal(mark["price"])
                peak = price if peak is None or price > peak else peak
                if not path or path[-1][0] != mark["at"]:
                    path.append([mark["at"], mark["price"]])
            if peak is not None:
                cost_each = Decimal(position["cost_basis"]) / Decimal(position["quantity"])
                out[symbol] = {"opened_at": position["opened_at"], "peak": format(peak, "f"),
                               "cost_each": format(cost_each, "f"), "path": path[-self.PATH_LIMIT:]}
        for symbol, entry in mine.items():                     # sold since last cycle: keep its history
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            if symbol in out and out[symbol]["opened_at"] == entry.get("opened_at"):
                continue
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"account_id": account_id, "symbol": symbol, "opened_at": entry.get("opened_at"),
                                         "closed_seen_at": now or utc_now(), "cost_each": entry.get("cost_each"),
                                         "path": [p[1] for p in entry["path"]]}) + "\n")
        _write(self.path, {account_id: out})
        return {symbol: entry["peak"] for symbol, entry in out.items()}

    def history(self, limit=500):
        """Closed agent positions with their recorded price paths (oldest first)."""
        if not self.log_path.is_file():
            return []
        out = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                record = json.loads(line)
                if record.get("cost_each") and record.get("path"):
                    out.append(record)
            except ValueError:
                continue
        return out
