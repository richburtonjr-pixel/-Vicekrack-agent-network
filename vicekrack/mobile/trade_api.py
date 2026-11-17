"""Trade tab requests for the Mobile Command Center (Trading PR 4). SIMULATED MONEY ONLY.

Same doorman as the rest of the app (gateway.py): only a paired phone (or this PC's browser after
pairing) may read; every POST also needs the app's Origin and CSRF header.

GET  /m/api/trade/markets                    every Kraken USD coin with prices (cached 15 seconds)
GET  /m/api/trade/coin?symbol=SOLUSD&range=1D one coin: prices, chart, your holding, latest agent view
GET  /m/api/trade/account                    balance, $80 fuse, holdings, trades, agent case files, scores
POST /m/api/trade/order    {"symbol", "side": "buy"|"sell", "amount": "20.00"|"all"}
POST /m/api/trade/restart  {"confirm": "restart-practice"}   start a fresh $100 account

Orders are priced on the PC from a fresh Kraken request; the phone never sends a price.
"""

import re
import threading
import time
from urllib.parse import parse_qs

from ..errors import NetworkError
from ..trading.providers import kraken_market
from ..trading.contracts import utc_now
from ..trading.desk.agents import performance
from ..trading.desk.desk import CaseFiles
from ..trading.errors import TradingError
from ..trading.practice import account as book
from ..trading.practice import manual
from ..trading.practice.store import RESTART_PHRASE, PracticeStore, load_config
from ..trading.providers.alpaca import urllib_transport
from ..trading.watch.cycle import WatchFolder

SYMBOL = re.compile(r"^[A-Z0-9]{1,12}USD$")
MARKET_CACHE_SECONDS = 15


class TradeApi:
    def __init__(self, root=None, transport=urllib_transport, clock=time.time):
        self.root, self.transport, self.clock = root, transport, clock
        self._lock = threading.Lock()
        self._market = (0.0, None)

    # ------------------------------------------------------------ reads
    def _catalog(self):
        return kraken_market.Catalog(self.root, transport=self.transport, clock=self.clock)

    def markets(self):
        with self._lock:
            saved_at, cached = self._market
            if cached is None or self.clock() - saved_at > MARKET_CACHE_SECONDS:
                rows = kraken_market.tickers(self._catalog(), self.transport)
                cached = {"contract": "trade_markets", "simulated": True, "updated_at": utc_now(),
                          "coins": sorted(rows.values(), key=lambda r: (-int(r["volume_24h_usd"]), r["symbol"]))}
                self._market = (self.clock(), cached)
        watch = WatchFolder(self.root).status() or {}
        radar = watch.get("radar") or {}
        return {**cached, "agents_watching": [c["symbol"] for c in radar.get("candidates", [])]}

    def coin(self, query):
        values = {k: v[0] for k, v in parse_qs(query, max_num_fields=4).items() if len(v) == 1}
        symbol, span = values.get("symbol", ""), values.get("range", "1D")
        if not SYMBOL.match(symbol):
            raise TradingError("invalid_symbol", "Unknown coin.")
        catalog = self._catalog()
        catalog.get(symbol)
        ticker = kraken_market.tickers(catalog, self.transport, symbols=[symbol]).get(symbol)
        points = kraken_market.chart(catalog, symbol, span, self.transport)
        account = self._account_or_none()
        holding = (account or {}).get("positions", {}).get(symbol)
        watch = WatchFolder(self.root).status() or {}
        agent_view = next((c for c in watch.get("coins", []) if c.get("symbol") == symbol), None)
        if agent_view:
            research = agent_view.get("research") or {}
            agent_view = {"checked_at": watch.get("cycle_at"), "verdict": research.get("verdict"),
                          "summaries": research.get("summaries"), "radar": agent_view.get("radar")}
        return {"contract": "trade_coin", "simulated": True, "symbol": symbol, "ticker": ticker, "range": span,
                "chart": points, "holding": holding, "agents": agent_view}

    def _account_or_none(self):
        try:
            config = load_config(**({"root": self.root} if self.root is not None else {}))
            state = PracticeStore(self.root).load(config)
            return book.summary(state, config, utc_now())
        except TradingError:
            return None

    def account(self):
        store = PracticeStore(self.root)
        watch = WatchFolder(self.root).status() or {}
        from ..trading.desk.safety import EmergencySwitch
        base = {"contract": "trade_account", "simulated": True, "notice": book.NOTICE,
                "emergency_stop": EmergencySwitch(self.root).state(),
                "watch": {"last_cycle_at": watch.get("cycle_at"), "cycle_seconds": watch.get("cycle_seconds"),
                          "radar": {k: (watch.get("radar") or {}).get(k) for k in ("ok", "error", "candidates", "stats")}}}
        try:
            config = load_config(**({"root": self.root} if self.root is not None else {}))
            state = store.load(config)
        except TradingError as error:
            return {**base, "account": None, "needs_start": error.code in ("practice_not_started", "practice_config_changed"),
                    "problem": error.as_dict()}
        now = utc_now()
        summary = book.summary(state, config, now)
        holdings = []
        for symbol, position in sorted(summary["positions"].items()):
            mark = summary["latest_prices"].get(symbol)
            value = None
            if mark:
                value = book.fmt(book.to_decimal(position["quantity"]) * book.parse(mark["price"]))
            holdings.append({"symbol": symbol, **position, "price": (mark or {}).get("price"),
                             "price_at": (mark or {}).get("at"), "value": value})
        ledger = store.ledger(300)
        mine = [line for line in ledger if line.get("account_id") == state["account_id"]]
        trades = [line for line in mine if line.get("kind") in ("fill", "halt")][-60:]
        scores = {"all": performance(mine, state["account_id"], summary)}
        for who in ("you", "agents"):
            scores[who] = performance([l for l in mine if l.get("by") == who], state["account_id"], summary)
        cases = [c for c in CaseFiles(self.root).recent(40) if c.get("account_id") == state["account_id"]]
        return {**base, "account": summary, "holdings": holdings, "trades": list(reversed(trades)),
                "cases": list(reversed(cases)), "scores": scores, "needs_start": False}

    def desk(self):
        """The AI Trading Desk, read-only: the same eight-agent board and case files the HQ Trading Floor shows,
        built from files the watch already wrote (no network, no trading). Charts are left out to keep it light."""
        from datetime import datetime, timezone
        from ..hq.floor import floor_snapshot
        snap = floor_snapshot(root=self.root, now=datetime.fromtimestamp(self.clock(), tz=timezone.utc))
        researched = [{k: m.get(k) for k in ("symbol", "price", "price_at", "error", "verdict", "verdict_text")}
                      for m in snap["markets"]]
        from ..trading.desk.desk import load_desk_config
        from ..trading.desk.safety import EmergencySwitch
        try:
            desk_config = load_desk_config(**({"root": self.root} if self.root is not None else {}))
            limits = {"order_dollars": desk_config["order_dollars"], **desk_config["risk"], **desk_config["monitor"],
                      "auto_practice_trades": desk_config["auto_practice_trades"]}
        except TradingError:
            limits = None
        return {"contract": "trade_desk", "simulated": True, "notice": book.NOTICE, "watch": snap["watch"],
                "emergency_stop": EmergencySwitch(self.root).state(), "limits": limits,
                "agents": snap["agents"], "decisions": snap["decisions"], "researched": researched,
                "performance": snap["performance"]}

    def results(self):
        """Performance of the current practice account, worked out from its own ledger (read-only, no network).

        The PC does not keep a minute-by-minute balance history, so the chart is the LOCKED-IN balance: the
        starting cash plus every closed trade's profit or loss (fees included), one point per sale, ending with
        today's balance including open coins. Drawdown is measured on those points."""
        base = {"contract": "trade_results", "simulated": True, "notice": book.NOTICE}
        store = PracticeStore(self.root)
        try:
            config = load_config(**({"root": self.root} if self.root is not None else {}))
            state = store.load(config)
        except TradingError as error:
            return {**base, "account": None, "needs_start": error.code in ("practice_not_started", "practice_config_changed"),
                    "problem": error.as_dict()}
        summary = book.summary(state, config, utc_now())
        lines = [l for l in store.ledger(100000) if l.get("account_id") == state["account_id"]]
        fills = [l for l in lines if l.get("kind") == "fill"]
        start = book.parse(state["starting_cash"])
        balance, peak, worst_drop, worst_at = start, start, book.to_decimal("0"), None
        curve = [{"at": state.get("created_at") or (lines[0]["at"] if lines else summary["updated_at"]),
                  "balance": book.fmt(start), "event": "start"}]
        for line in fills:
            if line.get("realized_pnl") is None:
                continue
            balance += book.parse(line["realized_pnl"], "signed_decimal")
            curve.append({"at": line["at"], "balance": book.fmt(balance), "event": "sell", "symbol": line["symbol"],
                          "by": line.get("by"), "pnl": line["realized_pnl"]})
        if summary.get("equity") is not None:
            curve.append({"at": summary["updated_at"], "balance": summary["equity"], "event": "now"})
        for point in curve:
            value = book.parse(point["balance"], "signed_decimal")
            peak = max(peak, value)
            drop = peak - value
            if drop > worst_drop:
                worst_drop, worst_at = drop, point["at"]
        fees, volume, coins = {"you": book.to_decimal("0"), "agents": book.to_decimal("0")}, book.to_decimal("0"), {}
        for line in fills:
            who = "you" if line.get("by") == "you" else "agents"
            fees[who] += book.parse(line["fee"])
            volume += book.parse(line["notional"])
            if line.get("realized_pnl") is not None:
                coin = coins.setdefault(line["symbol"], {"symbol": line["symbol"], "trades": 0, "pnl": book.to_decimal("0")})
                coin["trades"] += 1
                coin["pnl"] += book.parse(line["realized_pnl"], "signed_decimal")
        scores = {"all": performance(lines, state["account_id"], summary)}
        for who in ("you", "agents"):
            scores[who] = performance([l for l in lines if l.get("by") == who], state["account_id"], summary)
        halts = [l for l in lines if l.get("kind") == "halt"]
        return {**base, "needs_start": False, "account": {k: summary.get(k) for k in (
                    "status", "starting_cash", "cash", "equity", "shutdown_floor", "realized_pnl", "halt", "updated_at")},
                "curve": curve, "scores": scores,
                "drawdown": {"dollars": book.fmt(worst_drop), "pct": book.fmt(worst_drop * 100 / peak) if peak else "0",
                             "at": worst_at, "measured_on": "locked_in_balance"},
                "fees": {"you": book.fmt(fees["you"]), "agents": book.fmt(fees["agents"]), "total": book.fmt(fees["you"] + fees["agents"])},
                "volume": book.fmt(volume), "fills": len(fills), "halts": len(halts),
                "coins": sorted(({**c, "pnl": book.fmt(c["pnl"])} for c in coins.values()),
                                key=lambda c: book.parse(c["pnl"], "signed_decimal"), reverse=True)}

    # ------------------------------------------------------------ actions
    def order(self, document):
        if set(document) - {"symbol", "side", "amount", "request_id"}:
            raise NetworkError("invalid_request", "Send symbol, side and amount only.")
        symbol, side, amount = document.get("symbol"), document.get("side"), document.get("amount")
        if not isinstance(symbol, str) or not SYMBOL.match(symbol):
            raise TradingError("invalid_symbol", "Unknown coin.")
        result = manual.place_order(symbol, side, amount, root=self.root, transport=self.transport, clock=self.clock)
        with self._lock:
            self._market = (0.0, self._market[1])        # next market read is fresh
        return result

    def emergency(self, document):
        """Turn the agents' Emergency Stop on or off. On: the agents buy nothing and, on the next watch cycle,
        sell every coin they hold. Your own coins and trades are untouched."""
        from ..trading.desk.safety import EmergencySwitch
        engaged = document.get("engaged")
        if not isinstance(engaged, bool) or set(document) - {"engaged", "confirm"}:
            raise NetworkError("invalid_request", "Send engaged: true or false.")
        if not engaged and document.get("confirm") != "release-emergency-stop":
            raise TradingError("release_not_confirmed", "Confirm turning the Emergency Stop off.")
        state = EmergencySwitch(self.root).set(engaged, "phone", "Pressed in the app" if engaged else None)
        return {"contract": "trade_emergency", "simulated": True, "emergency_stop": state}

    def restart(self, document):
        if document.get("confirm") != RESTART_PHRASE or set(document) - {"confirm"}:
            raise TradingError("restart_not_confirmed", "Confirm starting a fresh $100 practice account.")
        config = load_config(**({"root": self.root} if self.root is not None else {}))
        state = PracticeStore(self.root).restart(config, RESTART_PHRASE)
        return {"contract": "trade_restart", "simulated": True, "account": book.summary(state, config, utc_now())}
