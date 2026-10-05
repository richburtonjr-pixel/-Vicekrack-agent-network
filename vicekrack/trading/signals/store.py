"""Research-signal storage (Step 27): runtime/trading/signals/ (ignored by Git).

records/<signal_id>.json  one file per research signal (same strategy + configuration +
                          dataset + bar -> same ID). Written once with an exclusive link.
                          A later run that finds the same signal keeps the existing
                          record (first detection wins) and reports it as already
                          recorded; any other difference is `signal_conflict`.
runs/<run_id>.json        the bounded evaluations of one run.

Records are published before the run, so a crash in between leaves only valid records;
re-running then reports them as already recorded. Everything is re-validated on read.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from ..contracts import ROOT, reject_trading_secrets, sha256, validate_schema
from ..errors import TradingError
from .engine import validate_run, validate_signal_record

RUN_ID = re.compile(r"^rsr-[0-9a-f]{24}$")
SIGNAL_ID = re.compile(r"^rsig-[0-9a-f]{24}$")


def load_signal_config(path="config/research-signals.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_signal_config", "Signal configuration must remain inside the project.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        validate_schema("research_signal_config", config)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        raise TradingError("invalid_signal_config", "config/research-signals.json is invalid.") from None
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_signal_config", "Cannot read a valid research-signal configuration.") from None
    return config, sha256(config)


def _identity(record):
    """Everything except when this particular replay noticed it (and the hash that covers that)."""
    return {k: v for k, v in record.items() if k not in ("detected_at_sim_utc", "expired_when_detected", "content_sha256")}


class SignalStore:
    def __init__(self, root=None):
        base = Path(root if root is not None else ROOT) / "runtime/trading/signals"
        self.runs, self.records = base / "runs", base / "records"

    def _publish(self, folder, name, document):
        temporary = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, indent=1, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, folder / name)
            return True
        except FileExistsError:
            return False
        except (OSError, ValueError, TypeError):
            raise TradingError("signal_write_failed", "Could not save research signals; nothing new was stored.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def save_run(self, run):
        validate_run(run)
        reject_trading_secrets(run)
        if (self.runs / f"{run['run_id']}.json").exists():
            raise TradingError("run_exists", "An identical signal run is already saved; nothing was changed.")
        new, existing = [], []
        for record in run["signals"]:
            if self._publish(self.records, f"{record['signal_id']}.json", record):
                new.append(record["signal_id"])
                continue
            stored = self.load_signal(record["signal_id"])
            if _identity(stored) != _identity(record):
                raise TradingError("signal_conflict", "A stored research signal with this ID has different content.")
            existing.append(record["signal_id"])
        if not self._publish(self.runs, f"{run['run_id']}.json", run):
            raise TradingError("run_exists", "An identical signal run is already saved; nothing was changed.")
        return {"run_id": run["run_id"], "new_signals": new, "already_recorded": existing}

    def load_run(self, run_id):
        if not RUN_ID.match(str(run_id)):
            raise TradingError("invalid_run_id", "A signal run ID looks like rsr- followed by 24 hex characters.")
        path = self.runs / f"{run_id}.json"
        if not path.is_file():
            raise TradingError("run_not_found", "No saved signal run with this ID.")
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            validate_run(run)
            if run["run_id"] != run_id:
                raise ValueError
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("signal_run_corrupt", "The saved signal run failed validation.") from None
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            raise TradingError("signal_run_corrupt", "The saved signal run is unreadable.") from None
        return run

    def load_signal(self, signal_id):
        if not SIGNAL_ID.match(str(signal_id)):
            raise TradingError("invalid_signal_id", "A research signal ID looks like rsig- followed by 24 hex characters.")
        path = self.records / f"{signal_id}.json"
        if not path.is_file():
            raise TradingError("signal_not_found", "No saved research signal with this ID.")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            validate_signal_record(record)
            if record["signal_id"] != signal_id:
                raise ValueError
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("signal_corrupt", "The saved research signal failed validation.") from None
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            raise TradingError("signal_corrupt", "The saved research signal is unreadable.") from None
        return record

    def list_runs(self):
        items = []
        for path in sorted(self.runs.glob("rsr-*.json")) if self.runs.is_dir() else []:
            try:
                run = self.load_run(path.stem)
                items.append({"run_id": run["run_id"], "dataset_id": run["dataset"]["dataset_id"],
                              "symbol": run["dataset"]["symbol"], "strategies": [s["name"] for s in run["strategies"]],
                              **run["summary"], "readable": True})
            except TradingError:
                items.append({"run_id": path.stem, "readable": False})
        return items

    def list_signals(self):
        items = []
        for path in sorted(self.records.glob("rsig-*.json")) if self.records.is_dir() else []:
            try:
                record = self.load_signal(path.stem)
                items.append({"signal_id": record["signal_id"], "strategy": record["strategy"]["name"],
                              "event": record["event"], "symbol": record["dataset"]["symbol"],
                              "bar_timestamp_utc": record["bar"]["timestamp_utc"],
                              "expires_at_utc": record["expires_at_utc"], "readable": True})
            except TradingError:
                items.append({"signal_id": path.stem, "readable": False})
        return items
