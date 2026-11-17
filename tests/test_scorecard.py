"""Autonomous-trading step 3: the early-or-late scorecard for the scanner's picks. No network, no trades."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_market import epoch, now_text  # noqa: E402
from test_watch_loop import seed_tape  # noqa: E402

from vicekrack.trading.watch.scorecard import Scorecard, gainer_ranks  # noqa: E402

T0 = 1_800_000_000


def market(prices, today=None):
    """prices: {base: last}. Every coin trades $5M a day; BTC is the benchmark."""
    today = today or {}
    return {b + "USD": {"symbol": b + "USD", "base": b, "last": p, "volume_24h_usd": "5000000",
                        "change_today_pct": today.get(b, "1.00")} for b, p in prices.items()}


def iso(epoch_seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.card = Scorecard(self.root)

    def step(self, seconds, prices, picks=(), today=None):
        return self.card.update([{"symbol": s, "base": s[:-3], "signals": ["volume_spike"], "score": "5"} for s in picks],
                                market(prices, today), iso(T0 + seconds), T0 + seconds)

    def test_an_early_pick(self):
        self.step(0, {"AAA": "10", "BTC": "100"}, picks=["AAAUSD"])
        self.step(3600, {"AAA": "10.5", "BTC": "101"}, today={"AAA": "20"})       # 1 h: +5%, top gainer
        self.step(14400, {"AAA": "11", "BTC": "100"})                              # 4 h: +10%
        result = self.step(86400, {"AAA": "10.2", "BTC": "99"})                    # 24 h: closed
        self.assertEqual(result["closed"], 1)
        record = self.card.closed()[0]
        self.assertEqual(record["verdict"], "early")
        self.assertEqual((record["max_gain_pct"], record["change_pct"]), ("10.00", {"1h": "5.00", "4h": "10.00", "24h": "2.00"}))
        self.assertEqual(record["benchmark_change_pct"], {"1h": "1.00", "4h": "0.00", "24h": "-1.00"})
        self.assertTrue(record["became_top_gainer"])

    def test_a_late_pick(self):
        self.step(0, {"BBB": "10", "BTC": "100"}, picks=["BBBUSD"])
        self.step(3600, {"BBB": "9.7", "BTC": "100"})
        self.step(86400, {"BBB": "9.5", "BTC": "100"})
        self.assertEqual(self.card.closed()[0]["verdict"], "late")

    def test_mixed(self):
        self.step(0, {"CCC": "10", "BTC": "100"}, picks=["CCCUSD"])
        self.step(3600, {"CCC": "10.2", "BTC": "100"})
        self.step(86400, {"CCC": "10.1", "BTC": "100"})
        self.assertEqual(self.card.closed()[0]["verdict"], "mixed")

    def test_a_coin_is_not_recorded_again_for_4_hours(self):
        self.step(0, {"AAA": "10", "BTC": "100"}, picks=["AAAUSD"])
        self.assertEqual(self.step(60, {"AAA": "10", "BTC": "100"}, picks=["AAAUSD"])["recorded"], 0)
        self.assertEqual(self.step(4 * 3600, {"AAA": "10", "BTC": "100"}, picks=["AAAUSD"])["recorded"], 1)

    def test_summary_compares_with_bitcoin(self):
        self.test_an_early_pick()
        summary = self.card.summary()
        self.assertEqual((summary["closed_picks"], summary["verdicts"]["early"], summary["became_top_gainer"]), (1, 1, 1))
        self.assertEqual(summary["average_change_pct"]["4h"], "10.00")
        self.assertEqual(summary["benchmark_average_change_pct"]["4h"], "0.00")
        self.assertEqual(summary["by_signal"]["volume_spike"], {"picks": 1, "early": 1, "average_max_gain_pct": "10.00"})

    def test_gainer_ranks_skip_stablecoins_and_thin_coins(self):
        prices = market({"AAA": "1", "USDT": "1", "BBB": "1"}, today={"AAA": "5", "USDT": "9", "BBB": "7"})
        prices["BBBUSD"]["volume_24h_usd"] = "1000"
        self.assertEqual(gainer_ranks(prices), {"AAAUSD": 1})


class InTheWatchCycle(unittest.TestCase):
    """The real watch cycle on the fake market records the radar's picks and never trades because of them."""

    def test_cycle_records_picks(self):
        from vicekrack.trading.watch.cycle import load_watch_config, run_cycle
        from desk_market import FakeKraken
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copytree(Path(__file__).resolve().parents[1] / "config", root / "config")
        seed_tape(root, 600)
        status = run_cycle(load_watch_config(root=root), root=root, now=now_text(600), transport=FakeKraken(600),
                           clock=lambda: epoch(600))
        self.assertEqual(status["scorecard"]["recorded"], len(status["radar"]["candidates"]))
        self.assertGreaterEqual(status["scorecard"]["open"], 1)
        self.assertEqual(Scorecard(root).summary()["open_picks"], status["scorecard"]["open"])


if __name__ == "__main__":
    unittest.main()
