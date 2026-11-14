"""The whole-market watch loop (Trading PR 2, radar since PR 4): Kraken prices -> radar -> agents -> guard -> desk."""

import ast
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_market import FakeKraken, epoch, now_text  # noqa: E402

from vicekrack.trading.errors import TradingError  # noqa: E402
from vicekrack.trading.providers.kraken_market import Catalog, tickers  # noqa: E402
from vicekrack.trading.practice.store import PracticeStore, load_config as load_practice_config  # noqa: E402
from vicekrack.trading.watch import scanner  # noqa: E402
from vicekrack.trading.watch.cycle import WatchFolder, load_watch_config, run_cycle  # noqa: E402
from vicekrack.trading.watch.runner import run_loop, seconds_until_next, single_loop, summary_line  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STREAK = 600                       # SOL starts a steady climb at minute 600 in the fake market


def seed_tape(root, upto, minutes=20):
    """Give the radar `minutes` of history before minute `upto`, as if the loop had been running."""
    folder = WatchFolder(root)
    tape = {}
    for index in range(upto - minutes, upto):
        catalog = Catalog(root, transport=FakeKraken(index), clock=lambda index=index: epoch(index))
        tape = scanner.update_tape(tape, tickers(catalog, FakeKraken(index)), epoch(index), 30)
    scanner.save_tape(folder.tape_path, tape)


class WatchLoopTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "config", self.root / "config")
        self.config = load_watch_config(root=self.root)
        self.practice_config = load_practice_config(root=self.root)
        self.practice = PracticeStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cycle(self, index, **kraken):
        return run_cycle(self.config, root=self.root, now=now_text(index), transport=FakeKraken(index, **kraken),
                         clock=lambda: epoch(index))

    def test_shipped_settings(self):
        self.assertEqual((self.config["interval"], self.config["cycle_seconds"]), ("1m", 60))
        self.assertEqual(self.config["provider"], "kraken_public")

    def test_radar_prices_the_whole_market_and_skips_junk(self):
        self.practice.start(self.practice_config, now_text(0))
        seed_tape(self.root, STREAK + 5)
        status = self.cycle(STREAK + 5)
        radar = status["radar"]
        self.assertTrue(radar["ok"])
        self.assertEqual(radar["stats"]["coins_priced"], 7)                      # euro and dark-pool pairs ignored
        self.assertEqual(radar["stats"]["liquid"], 4)                            # no USDT, TINY (thin) or DUST (too cheap)
        self.assertEqual([c["symbol"] for c in radar["candidates"]][:1], ["SOLUSD"])
        self.assertTrue(all(c["symbol"] not in ("TINYUSD", "USDTUSD", "DUSTUSD") for c in radar["candidates"]))
        sol = next(c for c in status["coins"] if c["symbol"] == "SOLUSD")
        self.assertTrue(sol["ok"])
        self.assertEqual([a["role"] for a in sol["research"]["agents"]],
                         ["market_scout", "trend_agent", "strategy_agent", "risk_review"])

    def test_warm_up_has_no_candidates(self):
        self.practice.start(self.practice_config, now_text(0))
        status = self.cycle(STREAK + 5)
        self.assertEqual(status["radar"]["candidates"], [])
        self.assertEqual(status["radar"]["stats"]["with_history"], 0)
        self.assertIn("streaks: none yet", summary_line(status))

    def test_every_holding_is_repriced_every_minute(self):
        self.practice.start(self.practice_config, now_text(0))
        self.practice.apply(self.practice_config, "buy", "DOGEUSD", "10.00", "0.2", now=now_text(100), by="you")
        status = self.cycle(110)
        state = self.practice.load(self.practice_config)
        self.assertEqual(state["marks"]["DOGEUSD"]["at"], now_text(110))
        self.assertEqual(status["practice"]["guard"], "ok")
        self.assertNotIn("DOGEUSD", [c["symbol"] for c in status["coins"]])     # your coins aren't run through the agents

    def test_a_crash_trips_the_80_dollar_guard(self):
        self.practice.start(self.practice_config, now_text(0))
        self.practice.apply(self.practice_config, "buy", "ETHUSD", "60.00", "3000", now=now_text(100), by="you")
        status = self.cycle(101, scale={"XETHZUSD": 0.4})                        # ETH falls 60%
        self.assertEqual(status["practice"]["guard"], "halted")
        state = self.practice.load(self.practice_config)
        self.assertEqual((state["status"], state["positions"]), ("halted", {}))

    def test_market_outage_guesses_nothing(self):
        self.practice.start(self.practice_config, now_text(0))
        self.practice.apply(self.practice_config, "buy", "DOGEUSD", "10.00", "0.2", now=now_text(100), by="you")
        status = self.cycle(120, fail={"AssetPairs", "Ticker"})
        self.assertFalse(status["radar"]["ok"])
        self.assertEqual(status["radar"]["error"], "provider_unreachable")
        self.assertEqual(status["practice"]["guard"], "unverified")             # 20 minutes old: no new buys
        self.assertEqual(self.practice.load(self.practice_config)["status"], "active")

    def test_without_a_practice_account_it_still_scans(self):
        status = self.cycle(STREAK)
        self.assertEqual(status["practice"]["error"], "practice_not_started")
        self.assertTrue(status["radar"]["ok"])

    def test_only_one_loop_at_a_time(self):
        folder = WatchFolder(self.root)
        with single_loop(folder):
            with self.assertRaises(TradingError) as caught:
                with single_loop(folder):
                    pass
        self.assertEqual(caught.exception.code, "watch_already_running")

    def test_loop_survives_a_bad_cycle_and_waits_for_the_next_minute(self):
        calls, waits, lines = [], [], []

        def flaky(config, root=None, now=None, sleep=None):
            calls.append(now)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"radar": {"ok": True, "stats": {"coins_priced": 3}, "candidates": []},
                    "practice": {"guard": "ok", "error": None, "state": {"equity": "100"}}, "desk": {"cases": []}}

        done = run_loop(self.config, root=self.root, max_cycles=3, sleep=waits.append, clock=lambda: epoch(10),
                        report=lines.append, cycle=flaky)
        self.assertEqual(done, 3)
        self.assertIn("unexpected_error", lines[1])
        self.assertIn("scanned 3 coins", lines[2])
        self.assertEqual(len(waits), 2)
        self.assertEqual(seconds_until_next(60, 5, epoch(10)), 60.0)

    def test_watch_code_never_trades_directly(self):
        """Only the trade desk's Executor may ask the practice account to buy or sell; never a broker."""
        for name in ("cycle.py", "runner.py", "cli.py", "scanner.py"):
            text = ast.dump(ast.parse((ROOT / "vicekrack/trading/watch" / name).read_text(encoding="utf-8")))
            for word in ("broker", "alpaca_crypto", "'buy'", "'sell'"):
                self.assertNotIn(word, text, f"{name} mentions {word}")


if __name__ == "__main__":
    unittest.main()
