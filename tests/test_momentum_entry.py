"""Momentum entries: the agents may buy a scanner pick without a textbook signal on the last bar, after the
Verifier recomputes the climb from raw bars. Practice money only; every account limit still applies."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_trade_desk as desk_tests  # noqa: E402
from desk_market import FakeKraken, epoch, now_text  # noqa: E402
from test_watch_loop import seed_tape  # noqa: E402

from vicekrack.trading.desk.verifier import verify_momentum  # noqa: E402
from vicekrack.trading.watch.cycle import load_watch_config, run_cycle  # noqa: E402

MINUTE = 605            # fake market: SOL is still climbing but has no textbook signal on this bar


class MomentumEntry(desk_tests.TradeDeskTests):
    def at(self, index):
        seed_tape(self.root, index)
        return run_cycle(load_watch_config(root=self.root), root=self.root, now=now_text(index),
                         transport=FakeKraken(index), clock=lambda: epoch(index))

    def set_momentum(self, **changes):
        path = self.root / "config/trading-desk.json"
        config = json.loads(path.read_text())
        config["momentum_entry"].update(changes)
        path.write_text(json.dumps(config))

    def test_a_scanner_pick_is_bought_after_every_agent_signs(self):
        status = self.at(MINUTE)
        coin = next(c for c in status["coins"] if c["symbol"] == "SOLUSD")
        self.assertNotEqual(coin["research"]["verdict"], "sufficient_for_future_paper_evaluation")   # no textbook signal
        case = self.cases(MINUTE)[-1]
        self.assertEqual((case["idea"], case["outcome"]), ("momentum", "executed"))
        self.assertTrue(all(s["decision"] == "approve" for s in case["signatures"][:-1]))
        verifier = next(s for s in case["signatures"] if s["agent"] == "verifier")
        self.assertEqual(verifier["reasons"], ["momentum_recomputed"])
        self.assertIn("SOLUSD", self.store.load(self.practice_config)["positions"])

    def test_switched_off_means_signals_only(self):
        self.set_momentum(enabled=False)
        self.at(MINUTE)
        self.assertEqual(self.cases(MINUTE), [])

    def test_score_floor(self):
        self.set_momentum(min_score="99")
        self.at(MINUTE)
        self.assertEqual(self.cases(MINUTE), [])

    def test_limits_still_apply(self):
        from vicekrack.trading.desk.safety import EmergencySwitch
        EmergencySwitch(self.root).set(True, "test")
        self.at(MINUTE)
        self.assertEqual(self.cases(MINUTE)[-1]["outcome"], "vetoed")
        self.assertEqual(self.store.load(self.practice_config)["positions"], {})


for _name in [n for n in dir(desk_tests.TradeDeskTests) if n.startswith("test_")]:
    setattr(MomentumEntry, _name, None)


class VerifierMomentumCheck(unittest.TestCase):
    CONFIG = {"max_bar_age_seconds": 180, "gap_check_bars": 50, "max_last_bar_move_pct": "3"}
    MOMENTUM = {"ema_fast": 9, "ema_slow": 21, "min_rise_15m_pct": "0.6"}

    def bars(self, step):
        out = []
        for i in range(60):
            price = 100 + step * i
            out.append({"start_utc": f"2026-11-17T{10 + i // 60:02d}:{i % 60:02d}:00Z", "open": str(price - step),
                        "close": str(price), "high": str(price), "low": str(price - abs(step))})
        return out

    def test_a_real_climb_passes(self):
        result = verify_momentum(self.bars(0.1), 1, "2026-11-17T11:00:30Z", self.CONFIG, self.MOMENTUM)
        self.assertEqual(result["decision"], "approve")

    def test_a_fall_or_a_flat_line_is_vetoed(self):
        falling = verify_momentum(self.bars(-0.1), 1, "2026-11-17T11:00:30Z", self.CONFIG, self.MOMENTUM)
        self.assertIn("uptrend_not_confirmed", falling["reasons"])
        flat = verify_momentum(self.bars(0.001), 1, "2026-11-17T11:00:30Z", self.CONFIG, self.MOMENTUM)
        self.assertIn("rise_not_confirmed", flat["reasons"])


if __name__ == "__main__":
    unittest.main()
