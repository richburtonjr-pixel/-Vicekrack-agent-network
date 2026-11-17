"""Trade plans: risk sizing, plan-driven exits, partial profits, loss cooldown, no-chase check.
Pure functions on hand-made bars; every check only sees bars up to "now" (no future data)."""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vicekrack.trading.desk import agents, plans  # noqa: E402
from vicekrack.trading.desk.verifier import verify_momentum  # noqa: E402

SIZING = {"mode": "risk", "risk_per_trade_pct": "0.5", "max_position_pct": "35", "max_order_dollars": "25.00",
          "min_order_dollars": "10.00", "atr_bars": 30, "atr_multiple": "3", "min_stop_pct": "1.5", "max_stop_pct": "6"}
MONITOR = {"stop_loss_pct": "4", "max_hold_minutes": 240, "ema_fast": 9, "ema_slow": 21, "exit_cost_bps": "35",
           "min_hold_minutes": 30, "trend_bar_minutes": 5, "time_stop_min_r": "0.5",
           "breakeven_at_r": "1", "partial_at_r": "2", "partial_fraction": "0.5"}


def bars(closes, wiggle="0.002", start="2026-11-17T10:00:00Z"):
    from datetime import datetime, timedelta
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    out = []
    for i, c in enumerate(closes):
        c = Decimal(str(c))
        out.append({"start_utc": (t0 + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "open": format(c, "f"), "close": format(c, "f"),
                    "high": format(c * (1 + Decimal(wiggle)), "f"), "low": format(c * (1 - Decimal(wiggle)), "f")})
    return out


def position(cost_each="100", quantity="0.2", opened_at="2026-11-17T10:00:00Z"):
    return {"quantity": quantity, "cost_basis": format(Decimal(cost_each) * Decimal(quantity), "f"),
            "opened_at": opened_at, "opened_by": "agents"}


class SizingTests(unittest.TestCase):
    def test_calm_coin_gets_min_stop_and_size_is_capped_by_per_trade_limit(self):
        sized = plans.size_entry(bars([100] * 40, wiggle="0.0005"), "100", SIZING, "0.7", "25.00")
        self.assertEqual(sized["stop_pct"], Decimal("1.5"))
        self.assertEqual(sized["dollars"], "22.72")                 # $0.50 / (1.5% + 0.7%), under the $25 limit
        self.assertLessEqual(Decimal(sized["risk_dollars"]), Decimal("0.50"))

    def test_jumpy_coin_gets_wider_stop_and_smaller_trade(self):
        calm = plans.size_entry(bars([100] * 40, wiggle="0.0005"), "100", SIZING, "0.7", "25.00")
        jumpy = plans.size_entry(bars([100] * 40, wiggle="0.006"), "100", SIZING, "0.7", "25.00")
        self.assertGreater(jumpy["stop_pct"], calm["stop_pct"])
        self.assertLess(Decimal(jumpy["dollars"]), Decimal(calm["dollars"]))

    def test_never_above_person_per_trade_limit(self):
        sized = plans.size_entry(bars([100] * 40, wiggle="0.0005"), "1000", SIZING, "0.7", "25.00")
        self.assertEqual(sized["dollars"], "25.00")

    def test_too_small_trade_is_skipped(self):
        sized = plans.size_entry(bars([100] * 40, wiggle="0.01"), "40", SIZING, "0.7", "25.00")
        self.assertEqual(sized["skip"], "position_too_small")

    def test_resample_uses_only_complete_past_groups(self):
        closes = plans.resample(bars(list(range(1, 13))), 5)        # 12 bars -> last 10 used, 2 groups
        self.assertEqual(closes, [Decimal(7), Decimal(12)])


class PlanExitTests(unittest.TestCase):
    def setUp(self):
        self.position = position()
        sized = {"stop_pct": Decimal("2"), "risk_dollars": "0.54", "atr_pct": Decimal("0.5"), "dollars": "20"}
        self.plan = plans.new_plan(self.position, sized, MONITOR, "case-1", "momentum", "0.7")

    def mark(self, price):
        return {"price": str(price), "bid": str(price), "at": "2026-11-17T10:10:00Z"}

    def test_stop_uses_plan_price(self):
        reasons = agents.position_monitor("SOLUSD", self.position, bars([100] * 30), self.mark("97.9"),
                                          "2026-11-17T10:10:00Z", MONITOR, plan=self.plan)
        self.assertEqual(reasons, ["stop_loss"])

    def test_breakeven_stop_after_move(self):
        self.plan["breakeven_done"] = True
        reasons = agents.position_monitor("SOLUSD", self.position, bars([100] * 30), self.mark("100.2"),
                                          "2026-11-17T10:10:00Z", MONITOR, plan=self.plan)
        self.assertEqual(reasons, ["breakeven_stop"])

    def test_trend_exit_waits_for_min_hold(self):
        falling = bars([100] * 150 + [100 - i * 0.05 for i in range(30)])
        early = agents.position_monitor("SOLUSD", self.position, falling, self.mark("100"),
                                        "2026-11-17T10:10:00Z", MONITOR, plan=self.plan)
        self.assertEqual(early, [])
        later = agents.position_monitor("SOLUSD", self.position, falling, self.mark("100"),
                                        "2026-11-17T10:45:00Z", MONITOR, plan=self.plan)
        self.assertIn("trend_turned_down", later)

    def test_time_stop_spares_trades_that_are_working(self):
        working = agents.position_monitor("SOLUSD", self.position, bars([101.5] * 30), self.mark("101.5"),
                                          "2026-11-17T14:30:00Z", {**MONITOR, "ema_fast": 3, "ema_slow": 5},
                                          plan=self.plan)
        self.assertNotIn("time_stop", working)
        stuck = agents.position_monitor("SOLUSD", self.position, bars([100.1] * 30), self.mark("100.1"),
                                        "2026-11-17T14:30:00Z", MONITOR, plan=self.plan)
        self.assertIn("time_stop", stuck)

    def test_partial_profit_once(self):
        self.assertFalse(agents.partial_due(self.plan, self.mark("102")))
        self.assertTrue(agents.partial_due(self.plan, self.mark("104.1")))
        self.plan["partial_done"] = True
        self.assertFalse(agents.partial_due(self.plan, self.mark("110")))

    def test_emergency_always_wins(self):
        reasons = agents.position_monitor("SOLUSD", self.position, bars([100] * 30), self.mark("105"),
                                          "2026-11-17T10:01:00Z", MONITOR, plan=self.plan, emergency=True)
        self.assertEqual(reasons, ["emergency_stop"])


class EntryFilterTests(unittest.TestCase):
    def test_no_chase_when_far_above_trend(self):
        closes = [100] * 40 + [100 + i * 0.8 for i in range(1, 16)]       # +12% in 15 minutes
        b = bars(closes)
        now = b[-1]["start_utc"].replace(":00Z", ":59Z")
        config = {"max_bar_age_seconds": 180, "gap_check_bars": 50, "max_last_bar_move_pct": "3"}
        momentum = {"min_rise_15m_pct": "0.6", "ema_fast": 9, "ema_slow": 21}
        self.assertEqual(verify_momentum(b, 1, now, config, momentum)["decision"], "approve")
        chased = verify_momentum(b, 1, now, config, {**momentum, "max_stretch_pct": "3"})
        self.assertIn("too_stretched", chased["reasons"])

    def test_loss_cooldown_blocks_same_coin(self):
        verdict = "sufficient_for_future_paper_evaluation"
        summary = {"status": "active", "guard": "ok", "dollars_above_floor": "19", "positions": {}}
        history = {"entries_today": 1, "last_exit_at": {}, "losing_streak": 0, "last_loss_at": None,
                   "last_loss_at_by_symbol": {"SOLUSD": "2026-11-17T10:00:00Z"}, "agents_pnl_today": "-0.30"}
        config = {"max_agent_positions": 3, "max_entries_per_day": 20, "reentry_wait_minutes": 60,
                  "pause_after_losses": 3, "pause_minutes": 60, "max_daily_loss_dollars": "10",
                  "loss_cooldown_minutes": 180, "min_dollars_above_floor": "10"}
        blocked = agents.risk_manager("SOLUSD", verdict, summary, history, "2026-11-17T11:00:00Z", config)
        self.assertIn("lost_on_this_coin_recently", blocked["reasons"])
        other = agents.risk_manager("ETHUSD", verdict, summary, history, "2026-11-17T11:00:00Z", config)
        self.assertEqual(other["decision"], "approve")


if __name__ == "__main__":
    unittest.main()
