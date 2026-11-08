"""Trading Floor (desktop HQ): one screen that shows the market, the agents at work and the money.

Two read-only views, both served by `api.respond`:

  floor_snapshot()   GET /api/trading/floor   LOCAL FILES ONLY, no network. Reads what the Trading
                     Watch already saves on this PC: runtime/trading/watch/status.json (last cycle),
                     watch/bars/<COIN>.csv (the 5-minute candles the agents just researched),
                     desk/cases-YYYY-MM-DD.jsonl (who approved, who vetoed and why) and the practice
                     ledger (fills). Nothing is computed that the watch did not record.

  AlpacaView         GET /api/trading/alpaca  The Alpaca PAPER account: a connection light (account
                     status, practice equity, buying power, market open/closed) and 5-minute stock
                     candles for the symbols in config/trading-floor.json. GET requests only, to the
                     fixed paper and market-data hosts, cached for `alpaca_refresh_seconds` so the
                     screen can refresh freely. Keys come from `trading.alpaca_keys` and are never
                     returned, logged or stored.

Nothing here places, changes or cancels an order, starts the watch, or touches the $80 guard.
Agent activity shown on the screen is only what those files record; there is no decorative activity.
"""

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..errors import NetworkError
from ..trading.contracts import ROOT

SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
TAIL_BYTES = 512 * 1024

AGENTS = [
    {"id": "market_scout", "name": "Market Scout", "job": "Checks the latest prices are complete and fresh"},
    {"id": "trend_agent", "name": "Trend Agent", "job": "Reads which way the price is moving"},
    {"id": "strategy_agent", "name": "Strategy Agent", "job": "Looks for a buy signal"},
    {"id": "verifier", "name": "Verifier", "job": "Re-does the math from raw prices to double-check"},
    {"id": "risk_review", "name": "Risk Review", "job": "Protects the $80 floor and the trading limits"},
    {"id": "executor", "name": "Executor", "job": "Places the practice trade once everyone approves"},
    {"id": "position_monitor", "name": "Position Monitor", "job": "Watches open trades and asks to close them"},
    {"id": "performance_reviewer", "name": "Performance Reviewer", "job": "Scores wins and losses"},
]
RESEARCH = ("market_scout", "trend_agent", "strategy_agent", "risk_review")

PLAIN = {
    "data_available": "Prices are complete and fresh",
    "insufficient_data": "Not enough price data yet",
    "stale_data": "Prices are too old to trust",
    "trend_assessed": "Trend read",
    "insufficient_indicators": "Not enough data to read the trend",
    "active_research_signal": "Found a buy signal",
    "no_active_signal": "No buy signal right now",
    "conflicting_signals": "Signals disagree, so no buy",
    "sufficient_for_future_paper_evaluation": "Research complete: OK to consider a trade",
    "insufficient_for_future_paper_evaluation": "Research says wait",
    "workflow_failed": "The research run failed this cycle",
    "executed": "Practice trade placed",
    "vetoed": "Stopped by an agent",
    "refused": "The $80 guard refused it",
    "waiting": "Waiting for a fresh price",
    "approved_not_executed": "Everyone approved, but auto practice trades are off",
    "stop_loss": "price hit the stop-loss",
    "held_too_long": "held too long",
    "trend_turned_down": "the trend turned down",
    "research_incomplete": "research not complete",
    "account_stopped": "practice account is stopped",
    "too_close_to_floor": "too close to the $80 floor",
    "already_holding": "already holding this coin",
    "daily_entry_limit": "daily trade limit reached",
    "reentry_wait": "waiting after the last sale",
    "losing_streak_pause": "paused after a losing streak",
    "auto_trades_off": "auto practice trades are switched off",
    "no_fresh_price": "no fresh price this cycle",
}


def plain(code):
    if code is None:
        return None
    text = str(code)
    return PLAIN.get(text, text.replace("_", " "))


def load_floor_config(path="config/trading-floor.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    try:
        if not target.is_relative_to(root):
            raise ValueError
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        if config["config_version"] != "1.0":
            raise ValueError
        stocks = config["stocks"]
        if not isinstance(stocks, list) or len(stocks) > 6 or not all(isinstance(s, str) and SYMBOL.match(s) for s in stocks):
            raise ValueError
        if config["stock_bar_timeframe"] not in ("1Min", "5Min", "15Min", "1Hour"):
            raise ValueError
        for key, low, high in (("chart_bars", 24, 300), ("alpaca_refresh_seconds", 30, 600),
                               ("watch_stale_after_seconds", 300, 7200), ("feed_cases", 1, 50)):
            value = config[key]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        raise NetworkError("invalid_floor_config", "The Trading Floor settings are invalid.") from None
    return config


def _utc(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)


def _z(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tail_lines(path, max_bytes=TAIL_BYTES):
    """Parsed JSON lines from the end of a file (bounded). Unreadable lines are skipped, never guessed."""
    try:
        with Path(path).open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - max_bytes))
            data = stream.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if len(data) == max_bytes and lines:
        lines = lines[1:]                                   # first line may be cut in half
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _candles(path, limit):
    """The watch's own 5-minute bars for one coin: [{t, o, h, l, c}] oldest first."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = text.strip().splitlines()[1:]
    out = []
    for row in rows[-limit:]:
        parts = row.split(",")
        if len(parts) < 5:
            continue
        try:
            out.append({"t": parts[0], "o": float(parts[1]), "h": float(parts[2]), "l": float(parts[3]),
                        "c": float(parts[4])})
        except ValueError:
            continue
    return out


def _case_view(case):
    steps = []
    for s in case.get("signatures") or []:
        if not isinstance(s, dict):
            continue
        reasons = [plain(r) for r in (s.get("reasons") or [])]
        steps.append({"agent": s.get("agent"), "stage": s.get("stage"), "decision": s.get("decision"),
                      "summary": s.get("summary") or "", "reasons": reasons})
    return {"case_id": case.get("case_id"), "kind": case.get("kind"), "symbol": case.get("symbol"),
            "at": case.get("opened_at"), "price": case.get("price"), "outcome": case.get("outcome"),
            "outcome_text": plain(case.get("outcome")), "steps": steps}


def _research_now(coins):
    """Per research agent, what it concluded for each coin in the LAST cycle."""
    now = {role: [] for role in RESEARCH}
    for coin in coins:
        research = coin.get("research") or {}
        for agent in research.get("agents") or []:
            role = agent.get("role")
            if role in now:
                now[role].append({"symbol": coin.get("symbol"), "status": agent.get("status"),
                                  "conclusion": agent.get("conclusion"), "text": plain(agent.get("conclusion")),
                                  "detail": (research.get("summaries") or {}).get(role, "")})
    return now


def _agent_board(coins, cases, performance, cycle_at, positions=None):
    research_now = _research_now(coins)
    latest = {}
    for case in cases:                                      # newest first
        for step in case["steps"]:
            agent = step["agent"]
            key = "risk_account" if agent == "risk_review" and step.get("stage") == "account_check" else agent
            latest.setdefault(key, {"at": case["at"], "symbol": case["symbol"], "kind": case["kind"],
                                    "decision": step["decision"], "summary": step["summary"],
                                    "reasons": step["reasons"]})
    board = []
    for agent in AGENTS:
        card = {**agent, "now": research_now.get(agent["id"], []), "last_decision": latest.get(agent["id"]),
                "state": "idle", "headline": None}
        if agent["id"] == "risk_review" and "risk_account" in latest:
            card["last_decision"] = latest["risk_account"]
        if agent["id"] in RESEARCH and card["now"]:
            card["state"] = "worked"
            card["headline"] = "; ".join(f"{n['symbol']}: {n['text']}" for n in card["now"] if n["text"])
            card["at"] = cycle_at
        elif card["last_decision"]:
            d = card["last_decision"]
            card["state"] = "worked"
            card["headline"] = f"{d['symbol']}: {plain(d['decision'])}" + (f" - {d['summary']}" if d["summary"] else "")
            card["at"] = d["at"]
        if agent["id"] == "position_monitor" and positions and not (
                card["last_decision"] and card["last_decision"]["at"] == cycle_at):
            # It checks every open position every cycle; no case file means it found no reason to sell.
            card["state"] = "worked"
            card["headline"] = "Watching " + ", ".join(sorted(positions)) + ": no reason to sell this cycle."
            card["at"] = cycle_at
        if agent["id"] == "performance_reviewer" and isinstance(performance, dict):
            card["state"] = "worked"
            trades = performance.get("closed_trades") or 0
            card["headline"] = (f"{trades} closed practice trade{'s' if trades != 1 else ''}: "
                                f"{performance.get('wins', 0)} wins, {performance.get('losses', 0)} losses, "
                                f"P&L ${performance.get('realized_pnl', '0.00')}")
            card["at"] = cycle_at
        if card["headline"] is None:
            card["headline"] = ("Nothing to do yet. It acts only when a trade idea reaches it."
                                if agent["id"] in ("verifier", "executor") else
                                "No open trades to watch." if agent["id"] == "position_monitor" else
                                "Waiting for the first watch cycle.")
        board.append(card)
    return board


def floor_snapshot(root=None, now=None, config=None):
    root_path = Path(root if root is not None else ROOT)
    config = config or load_floor_config()               # settings always come from the project folder
    now = now or datetime.now(timezone.utc)
    watch_dir = root_path / "runtime/trading/watch"
    status = None
    try:
        status = json.loads((watch_dir / "status.json").read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            status = None
    except (OSError, ValueError, UnicodeError):
        status = None

    cycle_at = (status or {}).get("cycle_at")
    age = None
    if cycle_at:
        try:
            age = int((now - _utc(cycle_at)).total_seconds())
        except ValueError:
            age = None
    running = age is not None and age <= config["watch_stale_after_seconds"]

    coins = [c for c in (status or {}).get("coins") or [] if isinstance(c, dict)]
    markets = []
    for coin in coins:
        symbol = str(coin.get("symbol", ""))
        if not SYMBOL.match(symbol):
            continue
        research = coin.get("research") or {}
        markets.append({"symbol": symbol, "kind": "crypto", "source": "Kraken (free public prices)",
                        "price": coin.get("price"), "price_at": coin.get("price_at"), "error": coin.get("error"),
                        "verdict": research.get("verdict"), "verdict_text": plain(research.get("verdict")),
                        "candles": _candles(watch_dir / "bars" / f"{symbol}.csv", config["chart_bars"])})

    desk_dir = root_path / "runtime/trading/desk"
    raw_cases = []
    for days_back in (1, 0):
        day = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
        raw_cases.extend(_tail_lines(desk_dir / f"cases-{day}.jsonl"))
    cases = [_case_view(c) for c in reversed(raw_cases)][:config["feed_cases"]]

    practice = (status or {}).get("practice") or {}
    account = practice.get("state") or None
    fills = [l for l in _tail_lines(root_path / "runtime/trading/practice/ledger.jsonl")
             if l.get("kind") == "fill" and (account is None or l.get("account_id") == account.get("account_id"))]
    trades = [{"at": f.get("at"), "symbol": f.get("symbol"), "side": f.get("side"), "quantity": f.get("quantity"),
               "price": f.get("fill_price"), "dollars": f.get("notional"), "profit": f.get("realized_pnl"),
               "reason": plain(f.get("reason"))} for f in reversed(fills)][:15]
    desk = (status or {}).get("desk") or {}
    money = None
    if isinstance(account, dict):
        money = {k: account.get(k) for k in ("status", "starting_cash", "cash", "equity", "shutdown_floor",
                                             "dollars_above_floor", "guard", "realized_pnl", "positions", "halt")}
    return {"contract": "trading_floor", "version": "1.0", "simulated_money": True, "now": _z(now),
            "watch": {"running": running, "last_cycle_at": cycle_at, "age_seconds": age,
                      "interval": (status or {}).get("interval"), "found": status is not None,
                      "auto_practice_trades": desk.get("auto_practice_trades"),
                      "practice_error": practice.get("error"), "desk_error": desk.get("error")},
            "markets": markets,
            "agents": _agent_board(coins, cases, desk.get("performance"), cycle_at,
                                   (account or {}).get("positions") if isinstance(account, dict) else None),
            "decisions": cases, "money": money, "guard": practice.get("guard"), "trades": trades,
            "performance": desk.get("performance")}


class AlpacaView:
    """Read-only Alpaca PAPER view with a short cache. One instance per HQ server."""

    def __init__(self, root=None, *, transport=None, environ=None, loader=None, clock=time.time, config=None):
        self.root = root
        self.transport = transport
        self.environ = environ
        self.loader = loader
        self.clock = clock
        self.config = config
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = 0.0

    def snapshot(self, now=None):
        config = self.config or load_floor_config()
        with self._lock:
            if self._cached is not None and self.clock() - self._cached_at < config["alpaca_refresh_seconds"]:
                return self._cached
            result = self._fetch(config, now or datetime.now(timezone.utc))
            self._cached, self._cached_at = result, self.clock()
            return result

    def _fetch(self, config, now):
        from ..trading.alpaca_keys import alpaca_environ
        from ..trading.broker.client import DATA_BASE, AlpacaPaperClient, decimal_text
        from ..trading.broker.cli import load_broker_config
        from ..trading.errors import TradingError
        from ..trading.providers.alpaca import urllib_transport

        base = {"contract": "trading_floor_alpaca", "version": "1.0", "paper_only": True, "checked_at": _z(now),
                "connected": False, "keys": None, "message": None, "account": None, "market": None, "stocks": []}
        environ, source = alpaca_environ(self.environ, self.loader)
        base["keys"] = source
        if source == "missing":
            base["message"] = "No Alpaca paper keys found. Run \"Save Alpaca Paper Keys.bat\"."
            return base
        if source == "not_paper":
            base["message"] = "The saved key ID does not start with PK, so it is not a paper key. It was not used."
            return base
        try:
            broker_config = load_broker_config()
            client = AlpacaPaperClient(config=broker_config, allow_network=True,
                                       transport=self.transport or urllib_transport, environ=environ)
            status, account = client.get("/v2/account")
            if status != 200 or not isinstance(account, dict):
                raise TradingError("broker_malformed_response", "x")
            base["account"] = {"status": str(account.get("status", "")),
                               "equity": decimal_text(account.get("equity", "0"), "equity"),
                               "cash": decimal_text(account.get("cash", "0"), "cash"),
                               "buying_power": decimal_text(account.get("buying_power", "0"), "buying power"),
                               "trading_blocked": account.get("trading_blocked") is True}
            base["connected"] = True
            base["message"] = "Connected to your Alpaca PAPER account (practice money)."
            status, clock = client.get("/v2/clock")
            if status == 200 and isinstance(clock, dict):
                base["market"] = {"is_open": clock.get("is_open") is True, "next_open": clock.get("next_open"),
                                  "next_close": clock.get("next_close")}
        except TradingError as error:
            base["message"] = {"broker_auth_failed": "Alpaca did not accept the saved keys. Save them again "
                                                     "(a paste may have been cut off).",
                               "broker_forbidden": "Alpaca refused the request for these keys.",
                               "broker_rate_limited": "Alpaca asked us to slow down; trying again shortly.",
                               "endpoint_override_rejected": "An Alpaca setting on this PC points somewhere other "
                                                             "than the paper account, so nothing was sent."}.get(
                error.code, "Alpaca could not be reached right now. Check the internet connection.")
            base["error"] = error.code
            return base

        start = _z(now - timedelta(days=5))
        for symbol in config["stocks"]:
            stock = {"symbol": symbol, "kind": "stock", "source": "Alpaca (IEX feed)", "price": None,
                     "price_at": None, "error": None, "candles": []}
            try:
                url = (f"{DATA_BASE}/v2/stocks/{symbol}/bars?timeframe={config['stock_bar_timeframe']}"
                       f"&start={start}&limit={config['chart_bars']}&feed=iex&sort=desc&adjustment=raw")
                status, data = client.request("GET", url)
                if status != 200 or not isinstance(data, dict):
                    raise TradingError("stock_bars_unavailable", "x")
                bars = [b for b in (data.get("bars") or []) if isinstance(b, dict)]
                candles = []
                for b in reversed(bars):
                    candles.append({"t": str(b["t"]), "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
                                    "c": float(b["c"])})
                stock["candles"] = candles
                if candles:
                    stock["price"], stock["price_at"] = decimal_text(str(bars[0]["c"]), "price"), candles[-1]["t"]
            except (TradingError, KeyError, TypeError, ValueError):
                stock["error"] = "stock_bars_unavailable"
            base["stocks"].append(stock)
        return base
