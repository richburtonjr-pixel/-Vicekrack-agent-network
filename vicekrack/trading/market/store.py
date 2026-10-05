"""Local market-data storage (Step 25): runtime/trading/market/ (ignored by Git).

datasets/<dataset_id>.json and replays/<replay_id>.json are written to a temporary file,
fsynced and published with an exclusive hard link: never partial, never overwritten. A
dataset ID is derived from the adapter, source-file SHA-256, symbol and interval, so
importing the same source for the same symbol and interval again is always rejected with
`dataset_exists`. Every stored file is re-validated when read.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from ..contracts import ROOT, reject_trading_secrets, sha256, utc_now, validate_schema
from ..errors import TradingError
from .adapters import LocalCsvAdapter, SyntheticFixtureAdapter
from .bars import build_dataset, validate_dataset, zone

DATASET_ID = re.compile(r"^mds-[0-9a-f]{24}$")
REPLAY_ID = re.compile(r"^rpl-[0-9a-f]{24}$")


def load_market_config(path="config/market-data.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_market_config", "Market-data configuration must remain inside the project.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        validate_schema("market_data_config", config)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        raise TradingError("invalid_market_config", "config/market-data.json is invalid.") from None
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_market_config", "Cannot read a valid market-data configuration.") from None
    return config, sha256(config)


class MarketStore:
    def __init__(self, root=None, clock=utc_now):
        self.base = Path(root if root is not None else ROOT) / "runtime/trading/market"
        self.datasets = self.base / "datasets"
        self.replays = self.base / "replays"
        self.clock = clock

    # ------------------------------------------------------------------ writing
    def _publish(self, folder, name, document, exists_code, exists_message):
        reject_trading_secrets(document)
        temporary = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, indent=1, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, folder / name)
        except FileExistsError:
            raise TradingError(exists_code, exists_message) from None
        except (OSError, ValueError, TypeError):
            raise TradingError("market_write_failed", "Could not save market data locally; nothing was stored.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def import_dataset(self, adapter, *, config, config_sha256, symbol=None, interval=None, tz=None,
                       naive_timezone=None, currency=None, label=None):
        rows, source, defaults = adapter.read()
        symbol = symbol or getattr(adapter, "symbol", None)
        settings = {"symbol": symbol or defaults.get("symbol"), "interval": interval or defaults.get("interval"),
                    "tz": tz or defaults.get("timezone"), "currency": currency or defaults.get("currency") or "USD",
                    "label": label or ("synthetic" if adapter.adapter == "synthetic_fixture" else "unknown")}
        requested = {"symbol": symbol, "interval": interval, "timezone": tz, "currency": currency}
        for key, value in defaults.items():
            if requested[key] is not None and requested[key] != value:
                raise TradingError("fixture_settings_mismatch", "Synthetic fixtures define their own symbol, interval, timezone and currency.")
        if None in settings.values():
            raise TradingError("import_settings_missing", "Symbol, interval and timezone are required for this import.")
        if settings["label"] not in adapter.allowed_labels:
            raise TradingError("label_not_allowed", "This data label is not allowed for this adapter.")
        if naive_timezone is not None:
            zone(naive_timezone)
        dataset = build_dataset(rows, symbol=settings["symbol"], interval=settings["interval"], tz=settings["tz"],
                                naive_timezone=naive_timezone, currency=settings["currency"], label=settings["label"],
                                source=source, config=config, config_sha256=config_sha256, imported_at=self.clock())
        self._publish(self.datasets, f"{dataset['dataset_id']}.json", dataset, "dataset_exists",
                      "This source was already imported for this symbol and interval; nothing was changed.")
        return dataset

    def save_replay(self, report):
        validate_schema("market_replay_report", report)
        self._publish(self.replays, f"{report['replay_id']}.json", report, "replay_exists",
                      "An identical replay report already exists; nothing was changed.")
        return report

    # ------------------------------------------------------------------ reading
    def load(self, dataset_id):
        if not DATASET_ID.match(str(dataset_id)):
            raise TradingError("invalid_dataset_id", "A dataset ID looks like mds- followed by 24 hex characters.")
        path = self.datasets / f"{dataset_id}.json"
        if not path.is_file():
            raise TradingError("dataset_not_found", "No stored dataset with this ID.")
        try:
            dataset = json.loads(path.read_text(encoding="utf-8"))
            validate_dataset(dataset)
            if dataset["dataset_id"] != dataset_id:
                raise ValueError
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("dataset_corrupt", "The stored dataset failed validation.") from None
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            raise TradingError("dataset_corrupt", "The stored dataset is unreadable.") from None
        return dataset

    def load_replay(self, replay_id):
        if not REPLAY_ID.match(str(replay_id)):
            raise TradingError("invalid_replay_id", "A replay ID looks like rpl- followed by 24 hex characters.")
        path = self.replays / f"{replay_id}.json"
        if not path.is_file():
            raise TradingError("replay_not_found", "No stored replay report with this ID.")
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            validate_schema("market_replay_report", report)
        except (TradingError, OSError, ValueError, UnicodeError):
            raise TradingError("replay_corrupt", "The stored replay report is invalid.") from None
        return report

    def list_datasets(self):
        items = []
        for path in sorted(self.datasets.glob("mds-*.json")) if self.datasets.is_dir() else []:
            try:
                d = self.load(path.stem)
                items.append({"dataset_id": d["dataset_id"], "symbol": d["symbol"], "interval": d["interval"],
                              "data_label": d["data_label"], "adapter": d["source"]["adapter"], "bars": d["bar_count"],
                              "first_start_utc": d["first_start_utc"], "last_start_utc": d["last_start_utc"],
                              "gaps": d["gaps"]["gap_count"], "imported_at": d["imported_at"], "readable": True})
            except TradingError:
                items.append({"dataset_id": path.stem, "readable": False})
        return items

    def list_replays(self):
        items = []
        for path in sorted(self.replays.glob("rpl-*.json")) if self.replays.is_dir() else []:
            try:
                r = self.load_replay(path.stem)
                items.append({"replay_id": r["replay_id"], "dataset_id": r["dataset_id"], "steps": r["simulation"]["steps"],
                              "start_utc": r["simulation"]["start_utc"], "end_utc": r["simulation"]["end_utc"],
                              "readable": True})
            except TradingError:
                items.append({"replay_id": path.stem, "readable": False})
        return items


def make_adapter(kind, *, config, fixture=None, file=None, symbol=None, source_name=None, root=ROOT):
    if kind == "synthetic":
        if fixture is None or file is not None:
            raise TradingError("import_settings_missing", "Synthetic imports need --fixture and no --file.")
        return SyntheticFixtureAdapter(fixture, root=root)
    if kind == "csv":
        if file is None or fixture is not None or symbol is None:
            raise TradingError("import_settings_missing", "CSV imports need --file and --symbol, and no --fixture.")
        return LocalCsvAdapter(file, limits=config["limits"], symbol=symbol, source_name=source_name or "local-csv")
    raise TradingError("unknown_adapter", "Only the synthetic and csv adapters exist; there are no live feeds.")
