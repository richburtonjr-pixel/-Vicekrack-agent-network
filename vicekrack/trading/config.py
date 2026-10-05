"""Paper-only trading configuration and kill switch.

The configuration has no broker, account or credential fields; `mode` is fixed to
"paper" and short selling is not supported. The kill switch can be engaged in the
configuration or with a local switch file (runtime/trading/kill-switch.json); an
unreadable switch file counts as engaged (fail safe).
"""

import json
import os
import tempfile
from pathlib import Path

from .contracts import ROOT, sha256, validate_schema
from .errors import TradingError
from .money import parse


def load_config(path="config/trading.paper.json", root=ROOT):
    """Return (config, sha256). Paths must stay inside the project."""
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_paper_config", "Trading configuration must remain inside the project.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_paper_config", "Cannot read a valid trading configuration.") from None
    validate_config(config)
    return config, sha256(config)


def validate_config(config):
    validate_schema("paper_config", config)
    limits = config["limits"]
    for key in ("max_order_notional", "max_position_notional", "max_daily_loss"):
        parse(limits[key], "money", "invalid_paper_config")
    fraction = parse(limits["max_position_fraction"], "decimal", "invalid_paper_config")
    if not 0 < fraction <= 1:
        raise TradingError("invalid_paper_config", "max_position_fraction must be greater than 0 and at most 1.")
    if parse(limits["max_quantity"], "decimal", "invalid_paper_config") <= 0:
        raise TradingError("invalid_paper_config", "max_quantity must be greater than zero.")
    if parse(limits["max_order_notional"], "money", "invalid_paper_config") > parse(limits["max_position_notional"], "money", "invalid_paper_config"):
        raise TradingError("invalid_paper_config", "max_order_notional must not exceed max_position_notional.")


def switch_path(root=None):
    return Path(root if root is not None else ROOT) / "runtime/trading/kill-switch.json"


def kill_switch_state(config, root=None):
    """(engaged, source). Config engagement always wins; a broken switch file is engaged."""
    if config is None:
        return True, "config_unavailable"
    if config["kill_switch"]["engaged"]:
        return True, "config"
    path = switch_path(root)
    if not path.exists():
        return False, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("engaged"), bool):
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        return True, "switch_file_unreadable"
    return (True, "switch_file") if data["engaged"] else (False, None)


def set_kill_switch(engaged, at, root=None):
    """Atomically write the local kill-switch file."""
    path = switch_path(root)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"engaged": bool(engaged), "changed_at": at}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise TradingError("kill_switch_write_failed", "Could not save the kill switch.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
