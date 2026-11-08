"""Trading Floor: the watch's saved records become one screen; Alpaca is read-only, paper-only and cached."""

import json
import math
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vicekrack.hq import api
from vicekrack.hq.floor import AGENTS, AlpacaView, floor_snapshot, load_floor_config
from vicekrack.trading.alpaca_keys import alpaca_environ

PORT = 8765
HOST = {"host": f"127.0.0.1:{PORT}"}
STATIC = Path(api.__file__).resolve().parent / "static"
NOW = datetime(2026, 11, 7, 6, 0, 20, tzinfo=timezone.utc)


def z(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_sample_floor(root, now=NOW, with_trade=True):
    """A realistic set of Trading Watch files (the same shapes cycle.py, desk.py and the practice store write)."""
    root = Path(root)
    watch, desk, practice = root / "runtime/trading/watch", root / "runtime/trading/desk", root / "runtime/trading/practice"
    for folder in (watch / "bars", desk, practice):
        folder.mkdir(parents=True, exist_ok=True)
    start = now.replace(second=0) - timedelta(minutes=5 * 96)
    prices = {}
    for symbol, base, wobble in (("BTCUSD", 75800.0, 160.0), ("ETHUSD", 2240.0, 9.0)):
        lines, last = ["timestamp,open,high,low,close,volume"], base
        for i in range(96):
            o = last
            c = base + wobble * math.sin(i / 9.0) + wobble * 0.4 * math.sin(i / 2.3) + i * wobble * 0.01
            lines.append(f"{z(start + timedelta(minutes=5 * i))},{o:.2f},{max(o, c) + wobble * .15:.2f},"
                         f"{min(o, c) - wobble * .15:.2f},{c:.2f},{3.1 + i % 5}")
            last = c
        (watch / "bars" / f"{symbol}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        prices[symbol] = f"{last:.2f}"

    def research(verdict, strategy):
        return {"run_id": "wfr-" + "a" * 32, "status": "completed", "as_of": z(now), "verdict": verdict,
                "agents": [{"role": "market_scout", "status": "completed", "conclusion": "data_available"},
                           {"role": "trend_agent", "status": "completed", "conclusion": "trend_assessed"},
                           {"role": "strategy_agent", "status": "completed", "conclusion": strategy},
                           {"role": "risk_review", "status": "completed", "conclusion": verdict}],
                "summaries": {"market_scout": "120 closed 5-minute bars, no gaps.",
                              "trend_agent": "Price above the 20 EMA; RSI 58.",
                              "strategy_agent": "EMA crossover active." if strategy == "active_research_signal" else "No rule active.",
                              "risk_review": "Research complete."},
                "strategy_findings": None}

    account_id = "pa-" + "1" * 16
    positions = {"BTCUSD": {"quantity": "0.00032912", "cost_basis": "25.00", "opened_at": z(now - timedelta(minutes=25))}} if with_trade else {}
    practice_state = {"notice": "x", "simulated": True, "account_id": account_id, "status": "active",
                      "starting_cash": "100.00", "cash": "75.00" if with_trade else "100.00",
                      "equity": "100.04" if with_trade else "100.00", "shutdown_floor": "80.00",
                      "dollars_above_floor": "20.04" if with_trade else "20.00", "guard": "ok", "guard_problems": [],
                      "positions": positions, "latest_prices": {}, "realized_pnl": "0.00", "fills": 1 if with_trade else 0,
                      "halt": None, "updated_at": z(now)}
    performance = {"agent": "performance_reviewer", "closed_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": None,
                   "realized_pnl": "0.00", "best": None, "worst": None, "equity_change": "0.04", "note": "x"}
    status = {"contract": "watch_status", "version": "1.0", "notice": "x", "simulated": True, "trades_placed": False,
              "cycle_at": z(now), "provider": "kraken_public", "interval": "5m",
              "coins": [{"symbol": "BTCUSD", "pair": "XBTUSD", "ok": True, "price": prices["BTCUSD"], "price_at": z(now - timedelta(seconds=20)),
                         "bars": 120, "research": research("sufficient_for_future_paper_evaluation", "active_research_signal"), "error": None},
                        {"symbol": "ETHUSD", "pair": "ETHUSD", "ok": True, "price": prices["ETHUSD"], "price_at": z(now - timedelta(seconds=20)),
                         "bars": 120, "research": research("insufficient_for_future_paper_evaluation", "no_active_signal"), "error": None}],
              "practice": {"state": practice_state, "guard": "ok", "error": None},
              "desk": {"auto_practice_trades": True, "cases": [], "performance": performance, "error": None}}
    (watch / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    cases = [{"contract": "trade_desk_case", "case_id": "case-" + "b" * 16, "kind": "entry", "symbol": "BTCUSD",
              "opened_at": z(now - timedelta(minutes=25)), "price": "75960.10", "account_id": account_id, "outcome": "executed",
              "signatures": [{"agent": "market_scout", "decision": "approve", "reasons": ["data_available"], "summary": "Prices complete."},
                             {"agent": "trend_agent", "decision": "approve", "reasons": ["trend_assessed"], "summary": "Uptrend."},
                             {"agent": "strategy_agent", "decision": "approve", "reasons": ["active_research_signal"], "summary": "EMA crossover."},
                             {"agent": "risk_review", "decision": "approve", "reasons": ["sufficient_for_future_paper_evaluation"], "summary": "Research complete."},
                             {"agent": "verifier", "decision": "approve", "reasons": ["recomputed_ema_crossover"], "summary": "Math checks out."},
                             {"agent": "risk_review", "stage": "account_check", "decision": "approve", "reasons": ["account_checks_passed"],
                              "summary": "Guard ok with $20.00 above the floor."},
                             {"agent": "executor", "decision": "executed", "reasons": ["filled"], "summary": "Bought $25.00 of BTC."}]},
             {"contract": "trade_desk_case", "case_id": "case-" + "c" * 16, "kind": "entry", "symbol": "BTCUSD",
              "opened_at": z(now), "price": prices["BTCUSD"], "account_id": account_id, "outcome": "vetoed",
              "signatures": [{"agent": "verifier", "decision": "approve", "reasons": ["recomputed_ema_crossover"], "summary": "Math checks out."},
                             {"agent": "risk_review", "stage": "account_check", "decision": "veto", "reasons": ["already_holding"],
                              "summary": "Account risk check failed: already_holding."}]}] if with_trade else []
    (desk / f"cases-{now.strftime('%Y-%m-%d')}.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    fills = [{"sequence": 1, "account_id": account_id, "simulated": True, "kind": "fill", "at": z(now - timedelta(minutes=25)),
              "symbol": "BTCUSD", "side": "buy", "quantity": "0.00032912", "price": "75960.10", "fill_price": "76036.06",
              "notional": "25.00", "fee": "0.06", "cash_after": "75.00", "realized_pnl": None, "reason": "desk_entry"}] if with_trade else []
    (practice / "ledger.jsonl").write_text("".join(json.dumps(f) + "\n" for f in fills), encoding="utf-8")
    return root


class FakeAlpaca:
    """Records every request; answers like Alpaca's paper and data APIs."""

    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, url, headers, *, timeout, max_bytes, method="GET", body=None):
        self.calls.append((method, url, dict(headers)))
        if self.status != 200:
            return self.status, {}, b"{}"
        if url.endswith("/v2/account"):
            data = {"id": "acct-1", "status": "ACTIVE", "equity": "100000", "cash": "100000", "buying_power": "200000"}
        elif url.endswith("/v2/clock"):
            data = {"is_open": False, "timestamp": "2026-11-07T01:00:00-05:00", "next_open": "2026-11-09T09:30:00-05:00",
                    "next_close": "2026-11-09T16:00:00-05:00"}
        else:
            data = {"bars": [{"t": f"2026-11-06T20:{55 - 5 * i:02d}:00Z", "o": 600 + i, "h": 601 + i, "l": 599 + i, "c": 600.5 + i}
                             for i in range(10)], "symbol": "SPY", "next_page_token": None}
        return 200, {}, json.dumps(data).encode()


KEYS = {"APCA_API_KEY_ID": "PKTESTKEY123", "APCA_API_SECRET_KEY": "secretvalue456"}


class Keys(unittest.TestCase):
    def test_environment_keys_are_copied_to_every_name_without_touching_os_environ(self):
        import os
        environ, source = alpaca_environ(KEYS, loader=lambda t: self.fail("vault read"))
        self.assertEqual(source, "environment")
        self.assertEqual(environ["ALPACA_PAPER_API_KEY_ID"], "PKTESTKEY123")
        self.assertNotIn("ALPACA_PAPER_API_KEY_ID", os.environ)

    def test_vault_keys_are_used_when_the_environment_has_none(self):
        vault = {"ViceKrack/APCA_API_KEY_ID": "PKVAULT1", "ViceKrack/APCA_API_SECRET_KEY": "s3cret"}
        environ, source = alpaca_environ({}, loader=vault.get)
        self.assertEqual(source, "windows_vault")
        self.assertEqual(environ["APCA_API_SECRET_KEY"], "s3cret")

    def test_missing_and_live_keys(self):
        self.assertEqual(alpaca_environ({}, loader=lambda t: None)[1], "missing")
        environ, source = alpaca_environ({"APCA_API_KEY_ID": "AKLIVE", "APCA_API_SECRET_KEY": "x"})
        self.assertEqual(source, "not_paper")
        self.assertNotIn("ALPACA_PAPER_API_KEY_ID", environ)

    def test_a_broken_vault_counts_as_missing(self):
        def boom(target):
            raise OSError("no vault")
        self.assertEqual(alpaca_environ({}, loader=boom)[1], "missing")


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_empty_folder_is_honest(self):
        snap = floor_snapshot(self.root, now=NOW)
        self.assertFalse(snap["watch"]["found"])
        self.assertFalse(snap["watch"]["running"])
        self.assertEqual(snap["markets"], [])
        self.assertIsNone(snap["money"])
        self.assertEqual([a["id"] for a in snap["agents"]], [a["id"] for a in AGENTS])
        self.assertTrue(all(a["state"] == "idle" for a in snap["agents"]))

    def test_watch_records_become_markets_agents_and_money(self):
        write_sample_floor(self.root)
        snap = floor_snapshot(self.root, now=NOW + timedelta(seconds=40))
        self.assertTrue(snap["watch"]["running"])
        btc = snap["markets"][0]
        self.assertEqual(btc["symbol"], "BTCUSD")
        self.assertEqual(len(btc["candles"]), load_floor_config()["chart_bars"])
        self.assertEqual(btc["verdict_text"], "Research complete: OK to consider a trade")
        agents = {a["id"]: a for a in snap["agents"]}
        self.assertIn("BTCUSD: Found a buy signal", agents["strategy_agent"]["headline"])
        self.assertIn("ETHUSD: No buy signal right now", agents["strategy_agent"]["headline"])
        self.assertEqual(agents["risk_review"]["last_decision"]["decision"], "veto")       # the newest account check
        self.assertEqual(agents["executor"]["last_decision"]["decision"], "executed")
        self.assertIn("Watching BTCUSD", agents["position_monitor"]["headline"])
        self.assertEqual(snap["decisions"][0]["outcome_text"], "Stopped by an agent")
        self.assertEqual(snap["money"]["equity"], "100.04")
        self.assertEqual(snap["trades"][0]["side"], "buy")

    def test_an_old_cycle_means_the_watch_is_stopped(self):
        write_sample_floor(self.root)
        self.assertFalse(floor_snapshot(self.root, now=NOW + timedelta(hours=2))["watch"]["running"])

    def test_corrupt_files_do_not_invent_data(self):
        write_sample_floor(self.root)
        (self.root / "runtime/trading/watch/status.json").write_text("{not json", encoding="utf-8")
        snap = floor_snapshot(self.root, now=NOW)
        self.assertFalse(snap["watch"]["found"])
        self.assertEqual(snap["markets"], [])


class Alpaca(unittest.TestCase):
    def view(self, transport, environ=KEYS, clock=None):
        ticks = clock or [1000.0]
        return AlpacaView(transport=transport, environ=environ, loader=lambda t: None, clock=lambda: ticks[0])

    def test_connected_view_reads_account_clock_and_stock_candles_with_get_only(self):
        fake = FakeAlpaca()
        snap = self.view(fake).snapshot(now=NOW)
        self.assertTrue(snap["connected"])
        self.assertEqual(snap["account"]["buying_power"], "200000")
        self.assertFalse(snap["market"]["is_open"])
        self.assertEqual([s["symbol"] for s in snap["stocks"]], ["SPY", "AAPL"])
        self.assertEqual(snap["stocks"][0]["candles"][0]["t"], "2026-11-06T20:10:00Z")   # oldest first
        self.assertEqual(snap["stocks"][0]["price"], "600.5")
        self.assertTrue(all(method == "GET" for method, _, _ in fake.calls))
        hosts = {url.split("/")[2] for _, url, _ in fake.calls}
        self.assertEqual(hosts, {"paper-api.alpaca.markets", "data.alpaca.markets"})
        self.assertNotIn("PKTESTKEY123", json.dumps(snap))
        self.assertNotIn("secretvalue456", json.dumps(snap))

    def test_results_are_cached(self):
        fake, ticks = FakeAlpaca(), [1000.0]
        view = self.view(fake, clock=ticks)
        view.snapshot(now=NOW)
        count = len(fake.calls)
        ticks[0] += 30
        view.snapshot(now=NOW)
        self.assertEqual(len(fake.calls), count)
        ticks[0] += 60
        view.snapshot(now=NOW)
        self.assertGreater(len(fake.calls), count)

    def test_rejected_keys_say_so_plainly(self):
        snap = self.view(FakeAlpaca(status=401)).snapshot(now=NOW)
        self.assertFalse(snap["connected"])
        self.assertIn("did not accept", snap["message"])

    def test_no_keys_means_no_network(self):
        fake = FakeAlpaca()
        snap = self.view(fake, environ={}).snapshot(now=NOW)
        self.assertEqual(snap["keys"], "missing")
        self.assertEqual(fake.calls, [])

    def test_live_keys_are_never_sent(self):
        fake = FakeAlpaca()
        snap = self.view(fake, environ={"APCA_API_KEY_ID": "AKLIVE", "APCA_API_SECRET_KEY": "x"}).snapshot(now=NOW)
        self.assertEqual(snap["keys"], "not_paper")
        self.assertEqual(fake.calls, [])


class Wiring(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def get(self, target, method="GET"):
        status, _, body = api.respond(method, target, dict(HOST), port=PORT, root=self.root)
        return status, json.loads(body)

    def test_floor_route(self):
        write_sample_floor(self.root)
        status, body = self.get("/api/trading/floor")
        self.assertEqual(status, 200)
        self.assertEqual(body["contract"], "trading_floor")
        self.assertEqual(self.get("/api/trading/floor?x=1")[0], 400)
        self.assertEqual(self.get("/api/trading/floor", method="POST")[0], 405)

    def test_alpaca_route_uses_the_cached_view(self):
        fake = FakeAlpaca()
        api._ALPACA_VIEWS[str(self.root)] = AlpacaView(transport=fake, environ=KEYS, loader=lambda t: None)
        self.addCleanup(api._ALPACA_VIEWS.pop, str(self.root), None)
        status, body = self.get("/api/trading/alpaca")
        self.assertEqual(status, 200)
        self.assertTrue(body["connected"])

    def test_page_wiring(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="floor"', html)
        self.assertIn('id="floor-view"', html)
        self.assertIn('/static/floor.js', html)
        self.assertNotIn('data-view="terminal"', html)
        status, headers, _ = api.respond("GET", "/static/floor.js", dict(HOST), port=PORT)
        self.assertEqual(status, 200)
        script = (STATIC / "floor.js").read_text(encoding="utf-8")
        self.assertNotIn("POST", script)                          # the screen only reads
        self.assertNotIn("Math.random", script)                   # nothing decorative pretends to be work


if __name__ == "__main__":
    unittest.main()
