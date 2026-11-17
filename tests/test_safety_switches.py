"""Autonomous-trading step 1: Emergency Stop, daily loss stop, and the profit trail (no fixed take-profit). No network."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_trade_desk as desk_tests  # noqa: E402
ENTRY = desk_tests.ENTRY

from decimal import Decimal  # noqa: E402

from vicekrack.trading.desk import agents, profit_trail  # noqa: E402
from vicekrack.trading.desk.safety import EmergencySwitch, Peaks  # noqa: E402

NOW = "2026-11-17T15:00:00Z"
POSITION = {"quantity": "10", "cost_basis": "100", "opened_at": "2026-11-17T14:50:00Z", "opened_by": "agents"}
TRAIL = {"tiers": [{"from_profit_pct": "6", "giveback_points": "6"}], "min_lock_profit_pct": "0.5", "let_winners_run": True}
MONITOR = {"stop_loss_pct": "2", "profit_trail": TRAIL, "max_hold_minutes": 60, "ema_fast": 3, "ema_slow": 5}


def mark(price):
    return {"price": price, "at": NOW}


class ProfitTrail(unittest.TestCase):
    """Cost is $10.00 a coin. Profit % here is before selling costs (exit_cost_bps 0) unless stated."""

    def check(self, price, peak, settings=TRAIL, costs="0"):
        return profit_trail.check("10", price, peak, settings, costs)

    def test_riches_example_20_to_14_points(self):
        self.assertFalse(self.check("11.41", "12")["sell"])                 # best +20%, now +14.1%: hold
        state = self.check("11.40", "12")                                     # best +20%, now +14%: sell
        self.assertTrue(state["sell"])
        self.assertEqual(state["locked_pct"], Decimal("14"))

    def test_points_not_a_price_drop(self):
        # A 6% drop in PRICE from $12 is $11.28 (+12.8% profit); the rule waits for +14% = $11.40, sooner.
        self.assertTrue(self.check("11.40", "12")["sell"])
        self.assertNotEqual(Decimal("12") * Decimal("0.94"), Decimal("11.40"))

    def test_lock_rises_with_the_best_profit(self):
        self.assertEqual(self.check("13", "13")["locked_pct"], Decimal("24"))   # best +30% -> lock +24%
        self.assertEqual(self.check("14", "14")["locked_pct"], Decimal("34"))   # best +40% -> lock +34%
        self.assertFalse(self.check("14", "14")["sell"])                         # still climbing: no ceiling

    def test_no_fixed_take_profit(self):
        self.assertFalse(self.check("15", "15")["sell"])                         # +50% and rising: keep it

    def test_not_active_below_the_first_tier_and_never_locks_a_loss(self):
        self.assertFalse(self.check("10.50", "10.59")["active"])                 # best +5.9%: trail not on yet
        state = self.check("10.06", "10.60")                                       # best +6%: lock at +0.5%, not 0
        self.assertEqual(state["locked_pct"], Decimal("0.5"))
        self.assertFalse(state["sell"])
        self.assertTrue(self.check("10.05", "10.60")["sell"])

    def test_selling_costs_are_counted(self):
        # 35 bps of slippage + fee: $11.44 nets about +14.0%, so it sells a little earlier than $11.40.
        self.assertTrue(self.check("11.44", "12.05", costs="35")["sell"])

    def test_tiers(self):
        tiers = {"tiers": [{"from_profit_pct": "3", "giveback_points": "3"}, {"from_profit_pct": "10", "giveback_points": "6"}]}
        self.assertEqual(self.check("10.5", "10.5", tiers)["locked_pct"], Decimal("2"))    # best +5% -> 3 pts
        self.assertEqual(self.check("12", "12", tiers)["locked_pct"], Decimal("14"))       # best +20% -> 6 pts

    def test_bad_settings_are_refused(self):
        for bad in ({"tiers": []}, {"tiers": [{"from_profit_pct": "6", "giveback_points": "0"}]},
                    {"tiers": [{"from_profit_pct": "10", "giveback_points": "6"}, {"from_profit_pct": "5", "giveback_points": "3"}]},
                    {"tiers": [{"from_profit_pct": "6", "giveback_points": "6"}], "min_lock_profit_pct": "7"}):
            with self.assertRaises((ValueError, KeyError)):
                profit_trail.validate(bad)

    def test_replay_compares_settings(self):
        records = [{"cost_each": "10", "path": ["10", "11", "12", "11.3", "11.0"]},     # winner that fades
                   {"cost_each": "10", "path": ["10", "9.9", "9.7"]}]                      # loser: stop-loss
        results = {r["name"]: r for r in profit_trail.compare(records, TRAIL, "2")}
        self.assertEqual(results["current"]["positions"], 2)
        self.assertEqual(results["current"]["average_profit_pct"], "5.00")               # (+13% - 3%) / 2
        self.assertEqual(results["fixed_3pct_take_profit"]["average_profit_pct"], "3.50")  # (+10% - 3%) / 2


class PositionMonitorRules(unittest.TestCase):
    def reasons(self, price, peak=None, emergency=False, opened_at=POSITION["opened_at"]):
        return agents.position_monitor("SOLUSD", {**POSITION, "opened_at": opened_at}, [], mark(price), NOW, MONITOR,
                                       peak=peak, emergency=emergency)

    def test_profit_trail_sells(self):
        self.assertEqual(self.reasons("11.40", peak="12"), ["profit_trail"])

    def test_winners_are_not_sold_for_time(self):
        long_ago = "2026-11-17T10:00:00Z"
        self.assertEqual(self.reasons("12", peak="12", opened_at=long_ago), [])            # +20%, held 5 h: keep
        self.assertEqual(self.reasons("10.10", peak="10.20", opened_at=long_ago), ["held_too_long"])  # trail not on

    def test_stop_loss_for_trades_that_never_win(self):
        self.assertEqual(self.reasons("9.80"), ["stop_loss"])

    def test_emergency_closes_everything(self):
        self.assertEqual(self.reasons("10.00", emergency=True), ["emergency_stop"])


class RiskManagerRules(unittest.TestCase):
    practice = {"status": "active", "guard": "ok", "dollars_above_floor": "20.00", "positions": {}}
    config = {"max_agent_positions": 2, "max_entries_per_day": 10, "reentry_wait_minutes": 15, "pause_after_losses": 3,
              "pause_minutes": 120, "min_dollars_above_floor": "10.00", "max_daily_loss_dollars": "5.00"}

    def decide(self, **history):
        base = {"entries_today": 0, "last_exit_at": {}, "losing_streak": 0, "last_loss_at": None,
                "agents_pnl_today": "0", "emergency": False}
        return agents.risk_manager("SOLUSD", "sufficient_for_future_paper_evaluation", self.practice,
                                   {**base, **history}, NOW, self.config)

    def test_daily_loss_stop(self):
        self.assertEqual(self.decide(agents_pnl_today="-4.99")["decision"], "approve")
        vetoed = self.decide(agents_pnl_today="-5.00")
        self.assertEqual((vetoed["decision"], vetoed["reasons"]), ("veto", ["daily_loss_limit"]))

    def test_emergency_vetoes_every_buy(self):
        self.assertIn("emergency_stop", self.decide(emergency=True)["reasons"])


class Switches(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_off_until_engaged_then_stays_on(self):
        switch = EmergencySwitch(self.root)
        self.assertFalse(switch.state()["engaged"])
        self.assertTrue(switch.set(True, "phone", "test")["engaged"])
        self.assertTrue(EmergencySwitch(self.root).state()["engaged"])
        self.assertFalse(switch.set(False, "phone")["engaged"])

    def test_a_damaged_switch_counts_as_on(self):
        switch = EmergencySwitch(self.root)
        switch.path.parent.mkdir(parents=True)
        switch.path.write_text("{not json", encoding="utf-8")
        self.assertTrue(switch.state()["engaged"])

    def test_peaks_rise_reset_and_skip_your_own_coins(self):
        peaks = Peaks(self.root)
        mine = {**POSITION, "opened_by": "you"}
        positions = {"SOLUSD": POSITION, "ETHUSD": mine}
        self.assertEqual(peaks.update("a1", positions, {"SOLUSD": mark("10.2"), "ETHUSD": mark("5")}), {"SOLUSD": "10.2"})
        later = {"price": "10.1", "at": "2026-11-17T15:01:00Z"}
        self.assertEqual(peaks.update("a1", positions, {"SOLUSD": later}), {"SOLUSD": "10.2"})          # keeps the high
        reopened = {"SOLUSD": {**POSITION, "opened_at": "2026-11-17T16:00:00Z"}}
        self.assertEqual(peaks.update("a1", reopened, {"SOLUSD": mark("9.9")}), {"SOLUSD": "9.9"})     # new position
        self.assertEqual(peaks.update("a1", {}, {}), {})                                                # closed: forgotten
        logged = peaks.history()
        self.assertEqual([r["path"] for r in logged], [["10.2", "10.1"], ["9.9"]])                    # kept for testing


class EmergencyEndToEnd(desk_tests.TradeDeskTests):
    """The real watch cycle on the fake market, with the switch on."""

    def test_switch_on_sells_the_agents_coins_and_blocks_buys(self):
        self.cycle(ENTRY)                                     # the agents buy SOL
        self.assertIn("SOLUSD", self.store.load(self.practice_config)["positions"])
        EmergencySwitch(self.root).set(True, "test")
        status = self.cycle(ENTRY + 1)
        self.assertEqual(self.store.load(self.practice_config)["positions"], {})
        now = desk_tests.now_text(ENTRY + 1)
        this_cycle = [c for c in self.cases(ENTRY + 1) if c["opened_at"] == now]
        exits = [c for c in this_cycle if c["kind"] == "exit"]
        self.assertIn("emergency_stop", exits[-1]["signatures"][0]["reasons"])
        entries = [c for c in this_cycle if c["kind"] == "entry"]
        self.assertTrue(all(c["outcome"] == "vetoed" for c in entries))
        self.assertTrue(status["desk"]["emergency_stop"]["engaged"])

    def test_switch_on_blocks_every_buy(self):
        EmergencySwitch(self.root).set(True, "test")
        self.cycle(ENTRY)                                     # SOL's streak would normally be bought here
        self.assertEqual(self.store.load(self.practice_config)["positions"], {})
        entries = [c for c in self.cases(ENTRY) if c["kind"] == "entry"]
        self.assertTrue(entries and all(c["outcome"] == "vetoed" for c in entries))


# Run only the two tests above from this class, not the whole inherited desk suite a second time.
for _name in [n for n in dir(desk_tests.TradeDeskTests) if n.startswith("test_")]:
    setattr(EmergencyEndToEnd, _name, None)


if __name__ == "__main__":
    unittest.main()
