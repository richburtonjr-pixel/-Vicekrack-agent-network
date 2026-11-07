"""Trading PR 2: the 24/7 watch loop (Kraken prices -> research agents -> practice guard). No trades."""

import ast
import json
import math
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from vicekrack.trading.errors import TradingError
from vicekrack.trading.practice.store import PracticeStore, load_config as load_practice_config
from vicekrack.trading.watch.cycle import WatchFolder, load_watch_config, run_cycle
from vicekrack.trading.watch.runner import run_loop, seconds_until_next, single_loop

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-07T04:00:20Z"
NOW_EPOCH = int(datetime(2026, 11, 7, 4, 0, 20, tzinfo=timezone.utc).timestamp())


def kraken_payload(pair, base_price, scale=1.0, bars=150, now_epoch=NOW_EPOCH):
    """Kraken-shaped OHLC: the last entry is the still-open bar, which must be dropped."""
    last_open = now_epoch // 300 * 300
    rows = []
    for i in range(bars):
        start = last_open - 300 * (bars - 1 - i)
        close = base_price * scale * (1 + 0.004 * math.sin(i / 6) + 0.00005 * i)
        open_ = close * 0.999
        rows.append([start, f"{open_:.2f}", f"{close * 1.001:.2f}", f"{open_ * 0.999:.2f}", f"{close:.2f}",
                     f"{close:.2f}", f"{1.5 + 0.1 * (i % 7):.8f}", 10])
    return {"error": [], "result": {pair: rows, "last": last_open}}


class FakeKraken:
    def __init__(self, scale=1.0, fail=()):
        self.scale, self.fail, self.calls = scale, set(fail), []

    def __call__(self, url, headers, *, timeout, max_bytes, method="GET", body=None):
        self.calls.append(url)
        assert url.startswith("https://api.kraken.com/0/public/OHLC?") and method == "GET" and body is None
        pair = "XBTUSD" if "XBTUSD" in url else "ETHUSD"
        if pair in self.fail:
            raise OSError("down")
        base = 90000.0 if pair == "XBTUSD" else 3000.0
        return 200, {}, json.dumps(kraken_payload(pair, base, self.scale)).encode()


class WatchLoopTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "config", self.root / "config")
        self.config = load_watch_config(root=self.root)
        self.practice_config = load_practice_config(root=self.root)
        self.practice = PracticeStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cycle(self, transport, now=NOW):
        epoch = int(datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp())
        return run_cycle(self.config, root=self.root, now=now, transport=transport, clock=lambda: epoch)

    def test_shipped_settings(self):
        self.assertEqual(self.config["symbols"], {"BTCUSD": "XBTUSD", "ETHUSD": "ETHUSD"})
        self.assertEqual((self.config["interval"], self.config["cycle_seconds"]), ("5m", 300))

    def test_cycle_fetches_researches_and_feeds_the_guard(self):
        self.practice.start(self.practice_config, "2026-11-07T03:00:00Z")
        status = self.cycle(FakeKraken())
        self.assertFalse(status["trades_placed"])                              # no idea passed this cycle
        for coin in status["coins"]:
            self.assertTrue(coin["ok"], coin)
            self.assertEqual(coin["bars"], 120)                                  # trimmed to bars_for_research
            self.assertEqual(coin["price_at"], "2026-11-07T04:00:00Z")           # close of the last CLOSED bar
            self.assertEqual([a["role"] for a in coin["research"]["agents"]],
                             ["market_scout", "trend_agent", "strategy_agent", "risk_review"])
            self.assertEqual(coin["research"]["status"], "completed")
        state = self.practice.load(self.practice_config)
        self.assertEqual(set(state["marks"]), {"BTCUSD", "ETHUSD"})
        self.assertEqual(status["practice"]["guard"], "ok")
        self.assertEqual(WatchFolder(self.root).status()["cycle_at"], NOW)
        log = (self.root / "runtime/trading/watch/cycles.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 1)

    def test_crash_seen_by_the_loop_trips_the_80_dollar_guard(self):
        self.practice.start(self.practice_config, "2026-11-07T03:00:00Z")
        self.cycle(FakeKraken())
        self.practice.apply(self.practice_config, "buy", "BTCUSD", "25.00",
                            self.practice.load(self.practice_config)["marks"]["BTCUSD"]["price"], now=NOW)
        self.practice.apply(self.practice_config, "buy", "ETHUSD", "25.00",
                            self.practice.load(self.practice_config)["marks"]["ETHUSD"]["price"], now=NOW)
        later = "2026-11-07T04:05:20Z"
        status = self.cycle(FakeKraken(scale=0.4), now=later)                   # both coins fall 60%
        self.assertEqual(status["practice"]["guard"], "halted")
        state = self.practice.load(self.practice_config)
        self.assertEqual((state["status"], state["positions"]), ("halted", {}))
        self.assertEqual(state["halt"]["reason"], "equity_floor_reached")

    def test_one_coin_failing_does_not_stop_the_other_or_the_guard(self):
        self.practice.start(self.practice_config, "2026-11-07T03:00:00Z")
        status = self.cycle(FakeKraken(fail={"ETHUSD"}))
        coins = {c["symbol"]: c for c in status["coins"]}
        self.assertTrue(coins["BTCUSD"]["ok"])
        self.assertEqual(coins["ETHUSD"]["error"], "provider_unreachable")
        self.assertEqual(status["practice"]["guard"], "ok")

    def test_without_a_practice_account_it_still_watches(self):
        status = self.cycle(FakeKraken())
        self.assertEqual(status["practice"]["error"], "practice_not_started")
        self.assertTrue(all(c["ok"] for c in status["coins"]))

    def test_only_one_loop_at_a_time(self):
        folder = WatchFolder(self.root)
        with single_loop(folder):
            with self.assertRaises(TradingError) as caught:
                with single_loop(folder):
                    pass
        self.assertEqual(caught.exception.code, "watch_already_running")

    def test_loop_survives_a_bad_cycle_and_waits_for_the_next_bar(self):
        calls, waits, lines = [], [], []

        def flaky(config, root=None, now=None):
            calls.append(now)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"coins": [{"symbol": "BTCUSD", "price": "1", "error": None}], "practice": {"guard": "ok", "error": None}}

        done = run_loop(self.config, root=self.root, max_cycles=3, sleep=waits.append, clock=lambda: NOW_EPOCH,
                        report=lines.append, cycle=flaky)
        self.assertEqual(done, 3)
        self.assertIn("unexpected_error", lines[1])
        self.assertEqual(len(waits), 2)
        self.assertEqual(seconds_until_next(300, 20, NOW_EPOCH), 300.0)          # 04:00:20 -> 04:05:20

    def test_watch_code_never_trades_directly(self):
        """Only the trade desk's Executor may ask the practice account to buy or sell; never a broker."""
        for name in ("cycle.py", "runner.py", "cli.py"):
            text = ast.dump(ast.parse((ROOT / "vicekrack/trading/watch" / name).read_text(encoding="utf-8")))
            for word in ("broker", "alpaca_crypto", "'buy'", "'sell'"):
                self.assertNotIn(word, text, f"{name} mentions {word}")

if __name__ == "__main__":
    unittest.main()
