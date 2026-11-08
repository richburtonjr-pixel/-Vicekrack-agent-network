"""Trading PR 1: $100 practice account and the $80 shutdown guard (simulated money only)."""

import ast
import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from vicekrack.trading.errors import TradingError
from vicekrack.trading.practice import account as book
from vicekrack.trading.practice import guard
from vicekrack.trading.practice.cli import main as cli_main
from vicekrack.trading.practice.store import RESTART_PHRASE, PracticeStore, load_config

ROOT = Path(__file__).resolve().parents[1]
T0, T1, T2 = "2026-11-07T04:00:00Z", "2026-11-07T04:05:00Z", "2026-11-07T04:10:00Z"


class PracticeAccountTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "config", self.root / "config")
        self.config = load_config(root=self.root)
        self.store = PracticeStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def started(self):
        return self.store.start(self.config, T0)

    # ------------------------------------------------------------------ basics
    def test_shipped_settings(self):
        self.assertEqual(self.config["account"]["starting_cash"], "100.00")
        self.assertEqual(self.config["shutdown"]["equity_floor"], "80.00")
        self.assertIs(self.config["leverage"], False)
        self.assertIs(self.config["allow_short"], False)

    def test_start_gives_100_and_guard_ok(self):
        state = self.started()
        summary = book.summary(state, self.config, T0)
        self.assertEqual((summary["cash"], summary["equity"], summary["guard"]), ("100.00", "100", "ok"))
        self.assertEqual(summary["dollars_above_floor"], "20")
        with self.assertRaises(TradingError) as caught:
            self.store.start(self.config, T1)
        self.assertEqual(caught.exception.code, "practice_exists")

    def test_buy_uses_simulator_costs_and_never_borrows(self):
        self.started()
        state, verdict, refusal = self.store.apply(self.config, "buy", "BTCUSD", "25.00", "90000.00", now=T0)
        self.assertIsNone(refusal)
        position = state["positions"]["BTCUSD"]
        self.assertEqual(position["quantity"], "0.0002775")          # sized down to stay inside $25 after slippage
        self.assertLessEqual(Decimal(position["cost_basis"]), Decimal("25.07"))
        self.assertGreaterEqual(Decimal(state["cash"]), 0)

    def test_order_and_position_limits(self):
        self.started()
        _, _, refusal = self.store.apply(self.config, "buy", "BTCUSD", "100.01", "90000.00", now=T0)
        self.assertEqual(refusal.code, "order_too_large")
        self.store.apply(self.config, "buy", "BTCUSD", "25.00", "90000.00", now=T0)
        _, _, refusal = self.store.apply(self.config, "buy", "BTCUSD", "10.00", "90000.00", now=T0)
        self.assertEqual(refusal.code, "position_already_open")                  # agents never add to a position
        _, _, refusal = self.store.apply(self.config, "buy", "BTCUSD", "10.00", "90000.00", now=T0, by="you")
        self.assertEqual(refusal.code, "position_already_open")                  # nor you to the agents' position
        for symbol in ("DOGEEUR", "btcusd", "BTC/USD"):
            with self.assertRaises(TradingError) as caught:
                self.store.apply(self.config, "buy", symbol, "10.00", "1.00", now=T0)
            self.assertEqual(caught.exception.code, "symbol_not_allowed")

    # ------------------------------------------------------------------ whole market, your own trades (PR 4)
    def test_any_kraken_usd_coin_and_buying_more_of_your_own(self):
        self.started()
        _, _, refusal = self.store.apply(self.config, "buy", "SOLUSD", "20.00", "150.00", now=T0, by="you")
        self.assertIsNone(refusal)
        state, _, refusal = self.store.apply(self.config, "buy", "SOLUSD", "10.00", "160.00", now=T0, by="you")
        self.assertIsNone(refusal)
        position = state["positions"]["SOLUSD"]
        self.assertEqual(position["opened_by"], "you")
        self.assertAlmostEqual(float(position["cost_basis"]), 30.07, delta=0.02)          # both buys plus fees
        self.assertEqual([l["by"] for l in self.store.ledger() if l["kind"] == "fill"], ["you", "you"])

    def test_buy_with_all_your_cash_never_borrows(self):
        self.started()
        state, _, refusal = self.store.apply(self.config, "buy", "ETHUSD", "all", "3000.00", now=T0, by="you")
        self.assertIsNone(refusal)
        self.assertGreaterEqual(Decimal(state["cash"]), 0)
        self.assertLess(Decimal(state["cash"]), Decimal("0.05"))
        _, _, refusal = book.buy(book.new_state(self.config, T0), self.config, "ETHUSD", "all", "3000.00", T0)
        self.assertEqual(refusal.code, "order_too_large")                        # "all" is for you, not the agents

    def test_partial_and_full_sells(self):
        self.started()
        self.store.apply(self.config, "buy", "SOLUSD", "40.00", "100.00", now=T0, by="you")
        state, _, _ = self.store.apply(self.config, "sell", "SOLUSD", "110.00", now=T1, by="you", dollars="11.00")
        left = state["positions"]["SOLUSD"]
        self.assertAlmostEqual(float(left["quantity"]), 0.29946, places=4)
        sell = [l for l in self.store.ledger() if l["kind"] == "fill"][-1]
        self.assertGreater(Decimal(sell["realized_pnl"]), 0)
        state, _, _ = self.store.apply(self.config, "sell", "SOLUSD", "110.00", now=T1, by="you", dollars="all")
        self.assertNotIn("SOLUSD", state["positions"])

    def test_the_guard_closes_your_coins_too(self):
        self.started()
        self.store.apply(self.config, "buy", "SOLUSD", "all", "100.00", now=T0, by="you")
        state, verdict, _ = self.store.apply(self.config, "mark", {"SOLUSD": {"price": "75.00", "at": T1}}, now=T1)
        self.assertEqual((verdict["verdict"], state["status"], state["positions"]), ("shutdown", "halted", {}))
        self.assertEqual([l["by"] for l in self.store.ledger() if l["kind"] == "fill"][-1], "guard")

    def test_no_leverage_when_cash_is_short(self):
        config = deepcopy(self.config)
        config["shutdown"]["equity_floor"] = "5.00"                    # keep the floor out of the way
        state = book.new_state(config, T0)
        state["cash"] = "10"
        _, _, refusal = book.buy(state, config, "ETHUSD", "25.00", "3000.00", T0)
        self.assertEqual(refusal.code, "insufficient_cash")
        self.assertEqual(state["positions"], {})

    # ------------------------------------------------------------------ the $80 shutdown
    def crash(self):
        self.started()
        self.store.apply(self.config, "buy", "BTCUSD", "25.00", "90000.00", now=T0)
        self.store.apply(self.config, "buy", "ETHUSD", "25.00", "3000.00", now=T0)
        # Both coins fall 50%: equity ~ 49.88 + 12.49 + 12.49 = ~74.86, below the $80 floor.
        return self.store.apply(self.config, "mark", {"BTCUSD": {"price": "45000.00", "at": T1},
                                                      "ETHUSD": {"price": "1500.00", "at": T1}}, now=T1)

    def test_floor_closes_everything_and_halts(self):
        state, verdict, _ = self.crash()
        self.assertEqual(verdict["verdict"], "shutdown")
        self.assertEqual(state["status"], "halted")
        self.assertEqual(state["positions"], {})
        self.assertLessEqual(Decimal(state["halt"]["equity_at_trigger"]), Decimal("80"))
        # A gap below the floor is reported honestly: the account ends below $80.
        self.assertLess(Decimal(state["cash"]), Decimal("80"))
        kinds = [line["kind"] for line in self.store.ledger()]
        self.assertEqual(kinds[-3:], ["fill", "fill", "halt"])
        self.assertTrue(all(line["reason"] == "equity_floor_reached" for line in self.store.ledger()[-3:-1]))

    def test_exactly_at_floor_triggers(self):
        state = book.new_state(self.config, T0)
        state["cash"] = "80"
        verdict = guard.check(state, {}, T0, self.config)
        self.assertEqual(verdict["verdict"], "shutdown")

    def test_halted_account_refuses_everything_until_a_person_restarts(self):
        self.crash()
        for operation, args in (("buy", ("BTCUSD", "10.00", "40000.00")), ("sell", ("BTCUSD", "40000.00"))):
            with self.assertRaises(TradingError) as caught:
                self.store.apply(self.config, operation, *args, now=T2)
            self.assertEqual(caught.exception.code, "account_halted")
        state, verdict, _ = self.store.apply(self.config, "mark", {"BTCUSD": {"price": "99000.00", "at": T2}}, now=T2)
        self.assertEqual((state["status"], verdict["verdict"]), ("halted", "halted"))   # a recovery doesn't restart it
        with self.assertRaises(TradingError) as caught:
            self.store.restart(self.config, "yes", T2)
        self.assertEqual(caught.exception.code, "restart_not_confirmed")
        fresh = self.store.restart(self.config, RESTART_PHRASE, T2)
        self.assertEqual((fresh["status"], fresh["cash"]), ("active", "100.00"))
        self.assertEqual(len(list((self.store.history).glob("practice-*.json"))), 1)

    def test_stale_or_missing_price_blocks_buys_but_closes_nothing(self):
        self.started()
        self.store.apply(self.config, "buy", "BTCUSD", "25.00", "90000.00", now=T0)
        later = "2026-11-07T05:00:00Z"                                  # BTC price is now 60 minutes old
        state, verdict, refusal = self.store.apply(self.config, "buy", "ETHUSD", "25.00", "3000.00", now=later)
        self.assertEqual(refusal.code, "equity_unverified")
        self.assertEqual(verdict["verdict"], "unverified")
        self.assertIn("BTCUSD", state["positions"])
        self.assertEqual(state["status"], "active")

    def test_buy_whose_costs_would_reach_the_floor_is_refused(self):
        config = deepcopy(self.config)
        config["shutdown"]["equity_floor"] = "99.95"
        state = book.new_state(config, T0)
        _, _, refusal = book.buy(state, config, "BTCUSD", "25.00", "90000.00", T0)
        self.assertEqual(refusal.code, "would_reach_floor")
        self.assertEqual(state["positions"], {})

    def test_future_prices_are_refused(self):
        self.started()
        with self.assertRaises(TradingError) as caught:
            self.store.apply(self.config, "mark", {"BTCUSD": {"price": "1.00", "at": "2026-11-07T06:00:00Z"}}, now=T0)
        self.assertEqual(caught.exception.code, "price_from_future")

    # ------------------------------------------------------------------ settings can't be loosened
    def test_unsafe_settings_are_refused(self):
        changes = [("leverage", True), ("allow_short", True)]
        for key, value in changes:
            bad = deepcopy(self.config)
            bad[key] = value
            with self.assertRaises(TradingError):
                book.validate_config(bad)
        for floor in ("100.00", "120.00", "0"):
            bad = deepcopy(self.config)
            bad["shutdown"]["equity_floor"] = floor
            with self.assertRaises(TradingError):
                book.validate_config(bad)
        bad = deepcopy(self.config)
        bad["shutdown"]["action"] = "keep_holding"
        with self.assertRaises(TradingError):
            book.validate_config(bad)

    def test_changed_settings_lock_the_running_account(self):
        self.started()
        changed = deepcopy(self.config)
        changed["shutdown"]["equity_floor"] = "50.00"
        with self.assertRaises(TradingError) as caught:
            self.store.load(changed)
        self.assertEqual(caught.exception.code, "practice_config_changed")

    def test_corrupted_state_blocks_trading(self):
        self.started()
        self.store.state_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(TradingError) as caught:
            self.store.apply(self.config, "buy", "BTCUSD", "10.00", "90000.00", now=T1)
        self.assertEqual(caught.exception.code, "practice_state_corrupt")

    # ------------------------------------------------------------------ independence from AI
    def test_guard_and_account_import_no_ai_agents_brokers_or_network(self):
        forbidden = ("agents", "providers", "broker", "openai", "anthropic", "xai", "urllib", "http", "socket", "requests")
        for name in ("guard.py", "account.py", "store.py"):
            tree = ast.parse((ROOT / "vicekrack/trading/practice" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, "module", None) or ""
                    names = [module] + [alias.name for alias in node.names]
                    for text in names:
                        for word in forbidden:
                            self.assertNotIn(word, text.lower(), f"{name} imports {text}")

    # ------------------------------------------------------------------ commands
    def test_commands_print_json(self):
        import io
        from contextlib import redirect_stdout
        clock = iter([T0, T0, T1]).__next__
        for argv in (["practice-start"], ["practice-buy", "ETHUSD", "20.00", "--price", "3000.00"], ["practice-status"]):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main(argv, root=self.root, clock=clock)
            self.assertEqual(code, 0, out.getvalue())
            self.assertTrue(json.loads(out.getvalue())["simulated"])


if __name__ == "__main__":
    unittest.main()
