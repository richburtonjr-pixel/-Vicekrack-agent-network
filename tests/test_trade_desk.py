"""Trading PR 3 (whole market since PR 4): the trade desk, where every agent trade idea is a case file signed in turn."""

import ast
import json
import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_market import FakeKraken, epoch, now_text  # noqa: E402
from test_watch_loop import seed_tape  # noqa: E402

from vicekrack.trading.desk import agents  # noqa: E402
from vicekrack.trading.desk.desk import CaseFiles, load_desk_config  # noqa: E402
from vicekrack.trading.desk.verifier import verify  # noqa: E402
from vicekrack.trading.practice.store import PracticeStore, load_config as load_practice_config  # noqa: E402
from vicekrack.trading.signals.store import load_signal_config  # noqa: E402
from vicekrack.trading.watch.cycle import load_watch_config, run_cycle  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENTRY, EXIT = 600, 651          # fake market: SOL's streak is approved at minute 600; its trend turns down by 651


class TradeDeskTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "config", self.root / "config")
        self.practice_config = load_practice_config(root=self.root)
        self.store = PracticeStore(self.root)
        self.store.start(self.practice_config, now_text(0))
        self.desk_config = load_desk_config(root=self.root)
        self.signals = load_signal_config()[0]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cycle(self, index, **kraken):
        if index == ENTRY:
            seed_tape(self.root, ENTRY)
        return run_cycle(load_watch_config(root=self.root), root=self.root, now=now_text(index),
                         transport=FakeKraken(index, **kraken), clock=lambda: epoch(index))

    def cases(self, index):
        return CaseFiles(self.root).read_day(now_text(index))

    # ------------------------------------------------------------------ end to end
    def test_an_idea_is_signed_by_every_agent_before_the_executor_buys(self):
        status = self.cycle(ENTRY)
        self.assertTrue(status["trades_placed"])
        case = self.cases(ENTRY)[-1]
        self.assertEqual((case["kind"], case["symbol"], case["outcome"]), ("entry", "SOLUSD", "executed"))
        order = [(s["agent"], s.get("stage")) for s in case["signatures"]]
        self.assertEqual(order, [("market_scout", None), ("trend_agent", None), ("strategy_agent", None),
                                 ("risk_review", None), ("verifier", None), ("risk_review", "account_check"),
                                 ("executor", None)])
        self.assertTrue(all(s["decision"] == "approve" for s in case["signatures"][:-1]))
        position = self.store.load(self.practice_config)["positions"]["SOLUSD"]
        self.assertEqual(position["opened_by"], "agents")
        fill = [l for l in self.store.ledger() if l["kind"] == "fill"][-1]
        self.assertEqual((fill["by"], fill["notional"] <= "25"), ("agents", True))
        self.assertGreater(float(fill["price"]), float(next(c for c in status["coins"] if c["symbol"] == "SOLUSD")["bid"]))

    def test_position_monitor_closes_and_performance_reviewer_reports(self):
        self.cycle(ENTRY)
        status = self.cycle(EXIT)
        case = self.cases(EXIT)[-1]
        self.assertEqual((case["kind"], case["outcome"]), ("exit", "executed"))
        self.assertEqual(case["signatures"][0]["agent"], "position_monitor")
        self.assertIn("trend_turned_down", case["signatures"][0]["reasons"])
        self.assertEqual(self.store.load(self.practice_config)["positions"], {})
        performance = status["desk"]["performance"]
        self.assertEqual(performance["closed_trades"], 1)
        self.assertEqual(performance["wins"] + performance["losses"], 1)

    def test_position_monitor_never_sells_your_own_coins(self):
        self.cycle(ENTRY)
        state = self.store.load(self.practice_config)
        state["positions"]["SOLUSD"]["opened_by"] = "you"
        self.store.state_path.write_text(json.dumps(state))
        self.cycle(EXIT)
        self.assertIn("SOLUSD", self.store.load(self.practice_config)["positions"])
        self.assertFalse([c for c in self.cases(EXIT) if c["kind"] == "exit"])

    def test_switching_auto_trades_off_reviews_without_trading(self):
        path = self.root / "config/trading-desk.json"
        config = json.loads(path.read_text())
        config["auto_practice_trades"] = False
        path.write_text(json.dumps(config))
        self.cycle(ENTRY)
        case = self.cases(ENTRY)[-1]
        self.assertEqual(case["outcome"], "approved_not_executed")
        self.assertEqual(self.store.load(self.practice_config)["positions"], {})

    def test_the_80_dollar_guard_still_wins_over_the_desk(self):
        state = json.loads(self.store.state_path.read_text())
        state["cash"] = "85"                                            # only $5 above the floor
        self.store.state_path.write_text(json.dumps(state))
        self.cycle(ENTRY)
        case = self.cases(ENTRY)[-1]
        self.assertEqual(case["outcome"], "vetoed")
        account = next(s for s in case["signatures"] if s.get("stage") == "account_check")
        self.assertIn("too_close_to_floor", account["reasons"])
        self.assertEqual(self.store.load(self.practice_config)["positions"], {})

    # ------------------------------------------------------------------ verifier
    def bars(self, count=60, step=1.0, start="2026-11-12T00:00:00Z"):
        from datetime import datetime, timedelta
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        bars, price = [], 100.0
        for i in range(count):
            close = price + step
            bars.append({"start_utc": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "open": f"{price:.2f}", "high": f"{max(price, close):.2f}", "low": f"{min(price, close):.2f}",
                         "close": f"{close:.2f}", "volume": "1"})
            price = close
        return bars

    def breakout_bars(self):
        bars = self.bars(step=0.0)                                       # flat, then one bar breaks out
        bars[-1].update(close="101.00", high="101.00")
        return bars

    def test_verifier_confirms_a_real_breakout(self):
        bars = self.breakout_bars()
        signature = verify(bars, 5, {"supporting_values": [f"breakout-3@{len(bars)}: close=101"]}, self.signals,
                           "2026-11-12T05:00:20Z", self.desk_config["verifier"])
        self.assertEqual(signature["decision"], "approve", signature)

    def test_verifier_vetoes_a_claim_its_own_math_does_not_confirm(self):
        bars = self.bars(step=0.0)                                       # flat: nothing broke out
        signature = verify(bars, 5, {"supporting_values": [f"breakout-3@{len(bars)}: close=100"]}, self.signals,
                           "2026-11-12T05:00:20Z", self.desk_config["verifier"])
        self.assertEqual(signature["decision"], "veto")
        self.assertIn("signal_not_independently_confirmed", signature["reasons"])

    def test_verifier_vetoes_stale_gappy_old_or_spiky_data(self):
        cfg = self.desk_config["verifier"]
        bars = self.breakout_bars()
        claim = {"supporting_values": [f"breakout-3@{len(bars)}: close=101"]}
        self.assertIn("data_too_old", verify(bars, 5, claim, self.signals, "2026-11-12T06:00:00Z", cfg)["reasons"])
        gappy = deepcopy(bars)
        del gappy[-10]
        self.assertIn("missing_bars", verify(gappy, 5, {"supporting_values": [f"breakout-3@{len(gappy)}: c"]},
                                             self.signals, "2026-11-12T05:00:20Z", cfg)["reasons"])
        old = {"supporting_values": [f"breakout-3@{len(bars) - 1}: close=101"]}
        self.assertIn("signal_not_on_last_bar", verify(bars, 5, old, self.signals, "2026-11-12T05:00:20Z", cfg)["reasons"])
        spiky = deepcopy(bars)
        spiky[-1].update(close="110.00", high="110.00")
        self.assertIn("last_bar_spike", verify(spiky, 5, claim, self.signals, "2026-11-12T05:00:20Z", cfg)["reasons"])

    def test_verifier_does_not_count_rules_it_cannot_recompute(self):
        bars = self.breakout_bars()
        claim = {"supporting_values": [f"vwap-reclaim@{len(bars)}: close=101"]}
        signature = verify(bars, 5, claim, self.signals, "2026-11-12T05:00:20Z", self.desk_config["verifier"])
        self.assertEqual(signature["decision"], "veto")

    # ------------------------------------------------------------------ risk review account check
    def practice(self, **changes):
        base = {"status": "active", "guard": "ok", "dollars_above_floor": "20", "positions": {}}
        base.update(changes)
        return base

    def history(self, **changes):
        base = {"entries_today": 0, "last_exit_at": {}, "losing_streak": 0, "last_loss_at": None}
        base.update(changes)
        return base

    def risk(self, practice=None, history=None, verdict="sufficient_for_future_paper_evaluation"):
        cfg = {**self.desk_config["risk"], "min_dollars_above_floor": self.desk_config["min_dollars_above_floor"]}
        return agents.risk_manager("BTCUSD", verdict, practice or self.practice(), history or self.history(),
                                   "2026-11-12T05:00:00Z", cfg)

    def test_risk_review_account_checks(self):
        self.assertEqual(self.risk()["decision"], "approve")
        cases = [
            (dict(practice=self.practice(guard="unverified")), "guard_unverified"),
            (dict(practice=self.practice(status="halted", guard="halted")), "account_stopped"),
            (dict(practice=self.practice(dollars_above_floor="9.99")), "too_close_to_floor"),
            (dict(practice=self.practice(positions={"BTCUSD": {}})), "already_holding"),
            (dict(practice=self.practice(positions={"ETHUSD": {}, "SOLUSD": {"opened_by": "agents"}})), "agent_position_limit"),
            (dict(history=self.history(entries_today=10)), "daily_entry_limit"),
            (dict(history=self.history(last_exit_at={"BTCUSD": "2026-11-12T04:50:00Z"})), "reentry_wait"),
            (dict(history=self.history(losing_streak=3, last_loss_at="2026-11-12T04:00:00Z")), "losing_streak_pause"),
            (dict(verdict="insufficient_for_future_paper_evaluation"), "research_incomplete"),
        ]
        for kwargs, reason in cases:
            signature = self.risk(**kwargs)
            self.assertEqual(signature["decision"], "veto", reason)
            self.assertIn(reason, signature["reasons"])

    # ------------------------------------------------------------------ position monitor
    def test_position_monitor_rules(self):
        cfg = self.desk_config["monitor"]
        position = {"quantity": "1", "cost_basis": "100", "opened_at": "2026-11-12T04:00:00Z"}
        rising = self.bars(step=1.0)
        hold = agents.position_monitor("BTCUSD", position, rising, {"price": "99.00", "at": "x"},
                                       "2026-11-12T04:30:00Z", cfg)
        self.assertEqual(hold, [])
        stop = agents.position_monitor("BTCUSD", position, rising, {"price": "98.00", "at": "x"},
                                       "2026-11-12T04:30:00Z", cfg)
        self.assertIn("stop_loss", stop)
        late = agents.position_monitor("BTCUSD", position, rising, {"price": "101.00", "at": "x"},
                                       "2026-11-12T05:00:00Z", cfg)
        self.assertIn("held_too_long", late)
        falling = agents.position_monitor("BTCUSD", position, self.bars(step=-1.0), {"price": "101.00", "at": "x"},
                                          "2026-11-12T04:30:00Z", cfg)
        self.assertIn("trend_turned_down", falling)

    # ------------------------------------------------------------------ structure
    def test_only_the_executor_reaches_the_practice_account(self):
        tree = ast.parse((ROOT / "vicekrack/trading/desk/desk.py").read_text(encoding="utf-8"))
        callers = set()
        for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "apply":
                    callers.add(function.name)
        self.assertEqual(callers, {"_execute"})
        for name in ("desk.py", "agents.py", "verifier.py"):
            text = (ROOT / "vicekrack/trading/desk" / name).read_text(encoding="utf-8")
            for word in ("broker", "openai", "anthropic", "xai", "urllib"):
                self.assertNotIn(word, text.lower(), f"{name} mentions {word}")

    def test_unsafe_desk_settings_are_refused(self):
        from vicekrack.trading.errors import TradingError
        path = self.root / "config/trading-desk.json"
        good = json.loads(path.read_text())
        for change in (lambda c: c["monitor"].update(stop_loss_pct="0"), lambda c: c["monitor"].update(ema_fast=9),
                       lambda c: c.update(auto_practice_trades="yes"), lambda c: c["risk"].update(max_entries_per_day=0)):
            bad = deepcopy(good)
            change(bad)
            path.write_text(json.dumps(bad))
            with self.assertRaises(TradingError):
                load_desk_config(root=self.root)


if __name__ == "__main__":
    unittest.main()
