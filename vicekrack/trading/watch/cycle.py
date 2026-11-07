"""One watch cycle (Trading PR 2). SIMULATED / RESEARCH ONLY: this step places no trades.

Every cycle, for each watched coin:
  1. Fetch recent CLOSED 5-minute bars from Kraken's free public endpoint (existing
     `providers.kraken` adapter; no keys, no account).
  2. Save them as a local CSV under runtime/trading/watch/bars/ and load that file through the
     existing local-CSV importer, labelled "delayed" with source name "kraken-<PAIR>-5m". The
     dataset is checked by the same validator as every other dataset (order, gaps, hashes).
  3. Run the existing four research agents (Market Scout -> Trend -> Strategy -> Risk Review)
     on it, at the last bar close.
Then, once per cycle:
  4. Hand the latest closing prices to the practice account (`practice.store.apply("mark")`).
     That runs the $80 guard: at or below the floor it closes everything and stops.
  5. Save a status snapshot (runtime/trading/watch/status.json) and append a short line to the
     cycle log, for the HQ and the phone.

A coin that fails to fetch is reported and skipped; its price simply gets older, and the guard
blocks new buys once it is stale. One coin's failure never stops the other or the guard.
"""

import csv
import io
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..agents.controller import run_workflow
from ..agents.store import load_agent_config
from ..contracts import ROOT, utc_now
from ..errors import TradingError
from ..indicators.store import load_indicator_config
from ..market.adapters import LocalCsvAdapter
from ..market.bars import build_dataset
from ..market.store import load_market_config
from ..practice import account as practice_book
from ..practice.store import PracticeStore, load_config as load_practice_config
from ..providers.alpaca import urllib_transport
from ..providers.kraken import KrakenPublicOHLCAdapter, interval_minutes
from ..signals.store import load_signal_config

NOTICE = ("WATCH LOOP: free Kraken public prices and rule-based research only. This step places no trades. "
          "Prices feed the practice account so its $80 shutdown is checked every cycle.")


def load_watch_config(path="config/watch.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_watch_config", "The watch settings must stay inside the project.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        if config["config_version"] != "1.0" or config["provider"] != "kraken_public":
            raise ValueError
        symbols = config["symbols"]
        if not symbols or not all(isinstance(k, str) and isinstance(v, str) for k, v in symbols.items()):
            raise ValueError
        interval_minutes(config["interval"])
        for key, low, high in (("bars_for_research", 20, 500), ("cycle_seconds", 60, 3600),
                               ("wait_after_bar_close_seconds", 0, 120), ("max_log_bytes", 10000, 50000000)):
            value = config[key]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError
        if not isinstance(config["keep_pc_awake"], bool):
            raise ValueError
    except TradingError:
        raise TradingError("invalid_watch_config", "The watch settings are invalid.") from None
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError):
        raise TradingError("invalid_watch_config", "The watch settings are invalid.") from None
    return config


def _utc_z(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_epoch(now_text):
    return datetime.fromisoformat(now_text.replace("Z", "+00:00")).timestamp()


class WatchFolder:
    def __init__(self, root=None):
        self.base = Path(root if root is not None else ROOT) / "runtime/trading/watch"
        self.bars = self.base / "bars"
        self.status_path = self.base / "status.json"
        self.log_path = self.base / "cycles.jsonl"
        self.lock_path = self.base / "watch.lock"

    def write_atomic(self, target, text):
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, suffix=".tmp", delete=False,
                                             newline="") as stream:
                temporary = Path(stream.name)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def append_log(self, line, max_bytes):
        self.base.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists() and self.log_path.stat().st_size > max_bytes:
            os.replace(self.log_path, self.base / "cycles.previous.jsonl")      # keep one older file, drop the rest
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n")

    def status(self):
        if not self.status_path.is_file():
            return None
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {"error": "status_unreadable"}


def _rows_to_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
    for row in rows:
        writer.writerow([row["timestamp"], row["open"], row["high"], row["low"], row["close"], row["volume"]])
    return out.getvalue()


def _research(symbol, pair, config, folder, configs, now, transport, clock):
    """Fetch, validate and research one coin. Returns a compact report (never raises TradingError)."""
    report = {"symbol": symbol, "pair": pair, "ok": False, "price": None, "price_at": None, "bars": 0,
              "research": None, "error": None}
    try:
        adapter = KrakenPublicOHLCAdapter(pair=pair, interval=config["interval"], allow_network=True,
                                          transport=transport, clock=clock)
        rows, _, _ = adapter.read()
        rows = rows[-config["bars_for_research"]:]
        for number, row in enumerate(rows, start=1):
            row["row"] = number
        path = folder.bars / f"{symbol}.csv"
        folder.write_atomic(path, _rows_to_csv(rows))
        market_config, market_sha = configs["market"]
        loaded, source, _ = LocalCsvAdapter(path, limits=market_config["limits"], symbol=symbol,
                                            source_name=f"kraken-{pair}-{config['interval']}").read()
        dataset = build_dataset(loaded, symbol=symbol, interval=config["interval"], tz="UTC", naive_timezone=None,
                                currency="USD", label="delayed", source=source, config=market_config,
                                config_sha256=market_sha, imported_at=now)
        last = dataset["bars"][-1]
        closed_at = datetime.fromisoformat(last["start_utc"].replace("Z", "+00:00")) + \
            timedelta(minutes=interval_minutes(config["interval"]))
        report.update(price=last["close"], price_at=_utc_z(closed_at), bars=dataset["bar_count"])
        run = run_workflow(dataset, workflow_config=configs["agents"], market_config=market_config,
                           indicator_config=configs["indicators"], signal_config=configs["signals"], created_at=now)
        final = run["final"]
        report["research"] = {
            "run_id": run["run_id"], "status": run["status"], "as_of": run["sim_time_utc"],
            "verdict": final["verdict"] if final else None,
            "agents": [{"role": s["role"], "status": s["status"], "conclusion": s["conclusion"]} for s in run["stages"]],
            "summaries": {e["role"]: e["summary"] for e in (final or {}).get("explanation", [])},
        }
        report["ok"] = True
    except TradingError as error:
        report["error"] = error.code
    return report


def run_cycle(config, *, root=None, now=None, transport=urllib_transport, clock=None):
    """One full watch cycle. Returns the status snapshot it saved."""
    now = now or utc_now()
    clock = clock or (lambda: _now_epoch(now))
    folder = WatchFolder(root)
    configs = {"market": load_market_config(), "agents": load_agent_config()[0],
               "indicators": load_indicator_config()[0], "signals": load_signal_config()[0]}
    coins = [_research(symbol, pair, config, folder, configs, now, transport, clock)
             for symbol, pair in sorted(config["symbols"].items())]

    practice = {"state": None, "guard": None, "error": None}
    prices = {c["symbol"]: {"price": c["price"], "at": c["price_at"]} for c in coins if c["ok"]}
    try:
        practice_config = load_practice_config(**({"root": root} if root is not None else {}))
        store = PracticeStore(root)
        if prices:
            state, verdict, _ = store.apply(practice_config, "mark", prices, now=now)
        else:
            state = store.load(practice_config)
        summary = practice_book.summary(state, practice_config, now)
        practice.update(state=summary, guard=summary["guard"])
    except TradingError as error:
        practice["error"] = error.code

    status = {"contract": "watch_status", "version": "1.0", "notice": NOTICE, "simulated": True,
              "trades_placed": False, "cycle_at": now, "provider": config["provider"], "interval": config["interval"],
              "coins": coins, "practice": practice}
    folder.write_atomic(folder.status_path, json.dumps(status, indent=2, ensure_ascii=False, allow_nan=False))
    folder.append_log({"at": now, "prices": {c["symbol"]: c["price"] for c in coins},
                       "verdicts": {c["symbol"]: (c["research"] or {}).get("verdict") for c in coins},
                       "errors": {c["symbol"]: c["error"] for c in coins if c["error"]},
                       "guard": practice["guard"], "practice_error": practice["error"],
                       "equity": (practice["state"] or {}).get("equity")}, config["max_log_bytes"])
    return status
