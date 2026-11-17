"""Autonomous-trading step 1: Emergency Stop, daily loss stop, take-profit and trailing stop. No network."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_trade_desk as desk_tests  # noqa: E402
ENTRY = desk_tests.ENTRY

from vicekrack.trading.desk import agents  # noqa: E402
from vicekrack.trading.desk.safety import EmergencySwitch, Peaks  # noqa: E402

NOW = "2026-11-17T15:00:00Z"
POSITION = {"quantity": "10", "cost_basis": "100", "opened_at": "2026-11-17T14:50:00Z", "opened_by": "agents"}
MONITOR = {"stop_loss_pct": "2", "take_profit_pct": "3", "trailing_stop_pct": "1.5", "max_hold_minutes": 60,
           "ema_fast": 3, "ema_slow": 5}


def mark(price):
    return {"price": price, "at": NOW}


class PositionMonitorRules(unittest.TestCase):
    def reasons(self, price, peak=None, emergency=False):
        return agents.position_monitor("SOLUSD", POSITION, [], mark(price), NOW, MONITOR, peak=peak, emergency=emergency)

    def test_take_profit_at_plus_3_percent(self):
        self.assertEqual(self.reasons("10.29"), [])
        self.assertEqual(self.reasons("10.30"), ["take_profit"])

    def test_trailing_stop_after_a_rise(self):
        self.assertEqual(self.reasons("10.10", peak="10.20"), [])              # 1.0% below the high: hold
        self.assertEqual(self.reasons("10.04", peak="10.20"), ["trailing_stop"])  # 1.57% below the high

    def test_trailing_stop_waits_until_the_coin_was_above_the_buy_price(self):
        self.assertEqual(self.reasons("9.85", peak="9.99"), [])                # never in profit: the 2% stop-loss rules

    def test_stop_loss_still_works(self):
        self.assertEqual(self.reasons("9.80"), ["stop_loss"])

    def test_emergency_closes_everything(self):
        self.assertEqual(self.reasons("10.00", emergency=True), ["emergency_stop"])

    def test_settings_can_be_left_out(self):
        plain = {k: v for k, v in MONITOR.items() if k not in ("take_profit_pct", "trailing_stop_pct")}
        self.assertEqual(agents.position_monitor("SOLUSD", POSITION, [], mark("11"), NOW, plain, peak="12"), [])


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
        self.assertEqual(peaks.update("a1", positions, {"SOLUSD": mark("10.1")}), {"SOLUSD": "10.2"})   # keeps the high
        reopened = {"SOLUSD": {**POSITION, "opened_at": "2026-11-17T16:00:00Z"}}
        self.assertEqual(peaks.update("a1", reopened, {"SOLUSD": mark("9.9")}), {"SOLUSD": "9.9"})     # new position
        self.assertEqual(peaks.update("a1", {}, {}), {})                                                # closed: forgotten


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
