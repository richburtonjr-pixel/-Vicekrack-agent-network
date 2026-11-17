"""Dashboard-editable trading limits (autonomous-trading step: configurable limits). Plain code.

You change a limit in the app; the change is saved in runtime/trading/desk/settings.json and laid over
config/trading-desk.json the next time the desk loads its settings (every watch cycle). Only the fields in
EDITABLE can be changed, each only within its HARD CAP below. The caps are written in this file, not in a
setting, so nothing in the app (and nothing the agents do) can raise them; the agents never write settings.
Practice capital ($100) and the $80 shutdown live in config/practice-account.json and are not editable here:
changing them starts a new practice account.
"""

import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..contracts import ROOT, utc_now

# field: (where in the desk config, kind, lowest, highest)
EDITABLE = {
    "auto_practice_trades": (("auto_practice_trades",), "bool", None, None),
    "order_dollars": (("order_dollars",), "money", "1.00", "50.00"),
    "max_agent_positions": (("risk", "max_agent_positions"), "int", 1, 5),
    "max_entries_per_day": (("risk", "max_entries_per_day"), "int", 1, 50),
    "max_daily_loss_dollars": (("risk", "max_daily_loss_dollars"), "money", "1.00", "20.00"),
    "stop_loss_pct": (("monitor", "stop_loss_pct"), "decimal", "0.5", "10"),
    "trail_start_pct": (("monitor", "profit_trail", "tiers", 0, "from_profit_pct"), "decimal", "1", "50"),
    "trail_giveback_points": (("monitor", "profit_trail", "tiers", 0, "giveback_points"), "decimal", "1", "30"),
}


def _path(root):
    return Path(root if root is not None else ROOT) / "runtime/trading/desk/settings.json"


def _get(config, where):
    for key in where:
        config = config[key]
    return config


def _set(config, where, value):
    for key in where[:-1]:
        config = config[key]
    config[where[-1]] = value


def _clean(field, value):
    where, kind, low, high = EDITABLE[field]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(field)
        return value
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ValueError(field)
        return value
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError(field) from None
    if not number.is_finite() or not Decimal(low) <= number <= Decimal(high):
        raise ValueError(field)
    if kind == "money":
        return format(number.quantize(Decimal("0.01")), "f")
    return format(number.normalize(), "f")


def load_overrides(root=None):
    try:
        document = json.loads(_path(root).read_text(encoding="utf-8"))
        return document.get("values", {}) if isinstance(document, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def overlay(config, overrides):
    """Lay valid overrides over a desk config (a copy). Anything invalid or out of bounds is ignored."""
    merged = json.loads(json.dumps(config))
    applied = {}
    for field, value in (overrides or {}).items():
        if field not in EDITABLE:
            continue
        try:
            clean = _clean(field, value)
            _get(merged, EDITABLE[field][0][:-1])            # the place must exist (e.g. a profit trail is configured)
            _set(merged, EDITABLE[field][0], clean)
            applied[field] = clean
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    merged["_dashboard_overrides"] = applied
    return merged


def current(config):
    """The editable fields' values in a (merged) desk config, plus their bounds, for the app."""
    out = {}
    for field, (where, kind, low, high) in EDITABLE.items():
        try:
            value = _get(config, where)
        except (KeyError, IndexError, TypeError):
            continue
        out[field] = {"value": value, "kind": kind, "min": low, "max": high}
    return out


def save(root, changes, by, now=None):
    """Validate and save changes from the app. Raises ValueError(field) on the first bad value; saves nothing then."""
    if not isinstance(changes, dict) or not changes or set(changes) - set(EDITABLE):
        raise ValueError("fields")
    values = load_overrides(root)
    for field, value in changes.items():
        values[field] = _clean(field, value)
    target = _path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {"values": values, "updated_at": now or utc_now(), "updated_by": str(by)[:40]}
    handle, temp = tempfile.mkstemp(dir=target.parent, prefix="settings", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
    return values
