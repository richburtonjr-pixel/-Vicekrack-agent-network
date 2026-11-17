"""Autonomous-trading step 2: the top-gainers scanner (volume spikes, acceleration, already-pumped and spread gates)."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vicekrack.trading.watch import scanner  # noqa: E402

NOW = 1_800_000_000
CONFIG = {**json.loads((ROOT / "config/watch.json").read_text(encoding="utf-8"))["scan"]}


def coin(symbol, prices, *, today_pct="2.00", ask_bid=None, volumes=None, day_volume="2880"):
    """prices: one per minute, oldest first, ending now. volumes: base 'volume today' per minute (same length)."""
    start = NOW - (len(prices) - 1) * 60
    points = []
    for i, price in enumerate(prices):
        point = [start + i * 60, price]
        if volumes is not None:
            point.append(volumes[i])
        points.append(point)
    last = prices[-1]
    ask, bid = ask_bid or (last, last)
    row = {"symbol": symbol, "base": symbol[:-3], "last": last, "ask": ask, "bid": bid, "tradeable": True,
           "volume_24h_usd": "5000000", "change_today_pct": today_pct,
           "volume_today_base": volumes[-1] if volumes else None, "volume_24h_base": day_volume}
    return points, row


def run(*coins):
    tape, tickers = {}, {}
    for points, row in coins:
        tape[row["symbol"]] = points
        tickers[row["symbol"]] = row
    return scanner.scan(tape, tickers, NOW, CONFIG)


STEADY = ["100"] * 4 + [str(100 + i * 0.1) for i in range(1, 13)]       # +1.2% over 15 min, rising every minute
FLAT_THEN_POP = ["100"] * 11 + ["100", "100.1", "100.2", "100.3", "100.4"]  # +0.4% only in the last 5 minutes


class Scanner(unittest.TestCase):
    def test_steady_climb_is_a_streak(self):
        found, stats = run(coin("AAAUSD", STEADY))
        self.assertEqual(found[0]["kind"], "streak")
        self.assertIn("streak", found[0]["signals"])

    def test_volume_spike_catches_a_coin_before_the_streak_rules_would(self):
        # Normal: 14400/day = 50 per 5 minutes. Last 5 minutes: 300 = 6x.
        volumes = [str(1000 + 10 * i) for i in range(11)] + [str(1100 + 60 * i) for i in range(1, 6)]
        found, stats = run(coin("BBBUSD", FLAT_THEN_POP, volumes=volumes, day_volume="14400"))
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["kind"], found[0]["volume_spike_x"]), ("early", "6.0"))
        self.assertIn("volume_spike", found[0]["signals"])
        self.assertEqual(stats["volume_spikes"], 1)
        self.assertEqual(run(coin("BBBUSD", FLAT_THEN_POP))[0], [])            # same prices, no spike: not picked

    def test_already_pumped_coins_are_skipped(self):
        found, stats = run(coin("CCCUSD", STEADY, today_pct="35.00"))
        self.assertEqual((found, stats["skipped_already_pumped"]), ([], 1))

    def test_wide_spread_is_skipped(self):
        found, stats = run(coin("DDDUSD", STEADY, ask_bid=("101.5", "100.5")))     # ~1% gap
        self.assertEqual((found, stats["skipped_wide_spread"]), ([], 1))

    def test_acceleration(self):
        speeding = ["100"] * 6 + ["100.05", "100.1", "100.15", "100.2", "100.25", "100.5", "100.8", "101.1", "101.4", "101.7"]
        found, _ = run(coin("EEEUSD", speeding))
        self.assertTrue(found[0]["accelerating"])
        even = [str(round(100 + 0.1 * i, 2)) for i in range(16)]
        found, _ = run(coin("FFFUSD", even))
        self.assertFalse(found[0]["accelerating"])                               # even pace: not speeding up

    def test_midnight_volume_reset_is_not_a_spike(self):
        volumes = ["9000"] * 11 + ["5", "60", "120", "180", "240"]               # 'volume today' restarted at 00:00 UTC
        found, stats = run(coin("GGGUSD", FLAT_THEN_POP, volumes=volumes))
        self.assertEqual((found, stats["volume_spikes"]), ([], 0))

    def test_volume_spike_ranks_above_a_plain_streak(self):
        volumes = [str(1000 + 10 * i) for i in range(11)] + [str(1100 + 60 * i) for i in range(1, 6)]
        found, _ = run(coin("PLAINUSD", STEADY), coin("LOUDUSD", STEADY, volumes=volumes))
        self.assertEqual([c["symbol"] for c in found], ["LOUDUSD", "PLAINUSD"])
        self.assertGreater(float(found[0]["score"]), float(found[1]["score"]))

    def test_tape_keeps_volume_and_old_tapes_still_work(self):
        tape = scanner.update_tape({}, {"AAAUSD": {"last": "1", "volume_today_base": "5"}}, NOW, 30)
        self.assertEqual(tape["AAAUSD"][-1], [NOW, "1", "5"])
        old_style = {"AAAUSD": [[p[0], p[1]] for p in coin("AAAUSD", STEADY)[0]]}
        found, _ = scanner.scan(old_style, {"AAAUSD": coin("AAAUSD", STEADY)[1]}, NOW, CONFIG)
        self.assertEqual(found[0]["volume_spike_x"], None)                       # no volume history: still scans


if __name__ == "__main__":
    unittest.main()
