"""One watch cycle (whole market since Trading PR 4). SIMULATED MONEY ONLY.

Every minute:
  1. RADAR: one Kraken request prices every USD coin (`kraken_market.tickers`). The prices are added
     to the tape and the Market Scout ranks coins on a steady climb (`watch/scanner.py`).
  2. FUSE: every coin the practice account holds is re-priced from that same request, which runs
     the $80 guard: at or below the floor it closes everything and stops.
  3. WORKUP: the top streak candidates, plus any coin the agents hold (so the Position Monitor has
     fresh bars), get recent CLOSED 1-minute bars from Kraken. Those are saved as local CSV, loaded
     through the normal importer and validator, and run through the four research agents.
  4. DESK: the Position Monitor checks the agents' positions, and every "sufficient" research verdict
     becomes a case file the Verifier and Risk Review must also approve before the Executor places a
     $25 practice buy at Kraken's ask price. See `trading/desk/desk.py`.
  5. Save a status snapshot (runtime/trading/watch/status.json) and a line in the cycle log.

If the whole-market request fails, nothing is guessed: holdings simply aren't re-priced, and the
guard blocks new buys once prices are over 10 minutes old. One coin failing never stops the others.
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
from ..desk.desk import load_desk_config, run_desk
from ..errors import TradingError
from ..indicators.store import load_indicator_config
from ..providers.kraken_market import Catalog, tickers as fetch_tickers, to_price
from ..market.adapters import LocalCsvAdapter
from ..market.bars import build_dataset
from ..market.store import load_market_config
from ..practice import account as practice_book
from ..practice.store import PracticeStore, load_config as load_practice_config
from ..providers.alpaca import urllib_transport
from ..providers.kraken import KrakenPublicOHLCAdapter, interval_minutes
from ..signals.store import load_signal_config
from . import scanner

NOTICE = ("WHOLE-MARKET WATCH: free Kraken public prices and rule-based agents. Practice trades use simulated money "
          "only, after every agent approves. The $80 shutdown is checked every minute.")


def load_watch_config(path="config/watch.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_watch_config", "The watch settings must stay inside the project.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        if config["config_version"] != "2.0" or config["provider"] != "kraken_public":
            raise ValueError
        interval_minutes(config["interval"])
        for key, low, high in (("bars_for_research", 20, 500), ("cycle_seconds", 60, 3600),
                               ("wait_after_bar_close_seconds", 0, 120), ("max_log_bytes", 10000, 50000000)):
            value = config[key]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError
        if not 0 <= float(config["request_spacing_seconds"]) <= 10 or not isinstance(config["keep_pc_awake"], bool):
            raise ValueError
        scan = config["scan"]
        for key, low, high in (("history_minutes", 16, 240), ("min_up_minutes_of_last_5", 0, 5), ("max_candidates", 0, 30)):
            value = scan[key]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError
        for key in ("min_volume_24h_usd", "min_rise_5m_pct", "min_rise_15m_pct"):
            if float(scan[key]) < 0:
                raise ValueError
        if not isinstance(scan["skip_bases"], list) or not all(isinstance(b, str) for b in scan["skip_bases"]):
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
        self.tape_path = self.base / "tape.json"

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
    """Fetch, validate and research one coin. Returns (compact report, dataset or None); never raises TradingError."""
    report = {"symbol": symbol, "pair": pair, "ok": False, "price": None, "price_at": None, "bars": 0,
              "research": None, "error": None}
    dataset = None
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
            "strategy_findings": next((s["findings"] for s in run["stages"] if s["role"] == "strategy_agent"), None),
        }
        report["ok"] = True
    except TradingError as error:
        report["error"] = error.code
        dataset = None
    return report, (dataset if report["ok"] else None)


def run_cycle(config, *, root=None, now=None, transport=urllib_transport, clock=None, sleep=None):
    """One full watch cycle. Returns the status snapshot it saved."""
    now = now or utc_now()
    clock = clock or (lambda: _now_epoch(now))
    now_epoch = int(_now_epoch(now))
    folder = WatchFolder(root)
    rooted = {"root": root} if root is not None else {}
    configs = {"market": load_market_config(), "agents": load_agent_config()[0],
               "indicators": load_indicator_config()[0], "signals": load_signal_config()[0]}

    # 1. radar: the whole market in one request
    radar = {"ok": False, "error": None, "candidates": [], "stats": None}
    market = {}
    catalog = Catalog(root, transport=transport, clock=clock)
    try:
        market = fetch_tickers(catalog, transport)
        tape = scanner.update_tape(scanner.load_tape(folder.tape_path), market, now_epoch, config["scan"]["history_minutes"])
        scanner.save_tape(folder.tape_path, tape)
        candidates, stats = scanner.scan(tape, market, now_epoch, config["scan"])
        radar.update(ok=True, candidates=candidates, stats=stats)
    except TradingError as error:
        radar["error"] = error.code

    practice = {"state": None, "guard": None, "error": None}
    desk = {"cases": [], "performance": None, "error": None}
    coins, datasets = [], {}
    store, practice_config, state = PracticeStore(root), None, None
    try:
        practice_config = load_practice_config(**rooted)
        state = store.load(practice_config)
        # 2. fuse: re-price every holding from the radar's prices
        marks = {}
        for symbol in state["positions"]:
            row = market.get(symbol)
            price = to_price(row["last"]) if row else None
            if price is not None:
                marks[symbol] = {"price": price, "at": now}
        if marks:
            state, _, _ = store.apply(practice_config, "mark", marks, now=now)
    except TradingError as error:
        practice["error"] = error.code

    # 3. workup: streak candidates plus the agents' own holdings
    deep = [c["symbol"] for c in radar["candidates"]]
    if state is not None:
        deep += [s for s, p in sorted(state["positions"].items()) if p.get("opened_by", "agents") == "agents" and s not in deep]
    spacing = float(config["request_spacing_seconds"])
    for number, symbol in enumerate(deep):
        if number and sleep is not None and spacing:
            sleep(spacing)
        try:
            pair = catalog.get(symbol)["pair"]
        except TradingError as error:
            coins.append({"symbol": symbol, "ok": False, "error": error.code, "research": None, "price": None})
            continue
        report, dataset = _research(symbol, pair, config, folder, configs, now, transport, clock)
        row = market.get(symbol) or {}
        report.update(ask=to_price(row.get("ask")), bid=to_price(row.get("bid")),
                      radar=next((c for c in radar["candidates"] if c["symbol"] == symbol), None))
        coins.append(report)
        if dataset is not None:
            datasets[symbol] = dataset

    # 4. desk, then the account as it stands
    if practice_config is not None and state is not None:
        try:
            desk.update(run_desk(coins, datasets, store=store, practice_config=practice_config,
                                 desk_config=load_desk_config(**rooted), signal_config=configs["signals"],
                                 now=now, root=root))
        except TradingError as error:
            desk["error"] = error.code
        try:
            state = store.load(practice_config)
            summary = practice_book.summary(state, practice_config, now)
            practice.update(state=summary, guard=summary["guard"])
        except TradingError as error:
            practice["error"] = error.code

    status = {"contract": "watch_status", "version": "2.0", "notice": NOTICE, "simulated": True,
              "trades_placed": any(c["outcome"] == "executed" for c in desk["cases"]), "cycle_at": now,
              "provider": config["provider"], "interval": config["interval"], "cycle_seconds": config["cycle_seconds"],
              "radar": radar, "coins": coins, "practice": practice, "desk": desk}
    folder.write_atomic(folder.status_path, json.dumps(status, indent=2, ensure_ascii=False, allow_nan=False))
    folder.append_log({"at": now, "radar_ok": radar["ok"], "radar_error": radar["error"],
                       "candidates": [c["symbol"] for c in radar["candidates"]],
                       "verdicts": {c["symbol"]: (c["research"] or {}).get("verdict") for c in coins},
                       "errors": {c["symbol"]: c["error"] for c in coins if c["error"]},
                       "guard": practice["guard"], "practice_error": practice["error"],
                       "cases": [(c["symbol"], c["kind"], c["outcome"]) for c in desk["cases"]],
                       "equity": (practice["state"] or {}).get("equity")}, config["max_log_bytes"])
    return status
