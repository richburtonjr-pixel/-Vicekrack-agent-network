"""Limits changed from the dashboard: saved, laid over the desk settings, always inside hard caps."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vicekrack.trading.desk import settings
from vicekrack.trading.desk.desk import load_desk_config

ROOT = Path(__file__).resolve().parents[1]


class DashboardLimits(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(ROOT / "config", self.root / "config")

    def test_changes_reach_the_desk(self):
        settings.save(self.root, {"order_dollars": "20", "max_agent_positions": 3, "trail_giveback_points": "4",
                                  "auto_practice_trades": False}, "test")
        config = load_desk_config(root=self.root)
        self.assertEqual((config["order_dollars"], config["risk"]["max_agent_positions"], config["auto_practice_trades"]),
                         ("20.00", 3, False))
        self.assertEqual(config["monitor"]["profit_trail"]["tiers"][0]["giveback_points"], "4")
        self.assertEqual(sorted(config["_dashboard_overrides"]),
                         ["auto_practice_trades", "max_agent_positions", "order_dollars", "trail_giveback_points"])

    def test_hard_caps_cannot_be_passed(self):
        for change in ({"order_dollars": "500"}, {"max_agent_positions": 9}, {"max_daily_loss_dollars": "100"},
                       {"stop_loss_pct": "0"}, {"trail_giveback_points": "abc"}, {"max_agent_positions": True},
                       {"starting_cash": "1000"}, {}):
            with self.assertRaises(ValueError, msg=str(change)):
                settings.save(self.root, change, "test")
        self.assertFalse((self.root / "runtime/trading/desk/settings.json").exists())    # nothing saved

    def test_a_tampered_file_is_ignored_value_by_value(self):
        path = self.root / "runtime/trading/desk/settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"values": {"order_dollars": "9999", "max_entries_per_day": 5, "shutdown": "0"}}))
        config = load_desk_config(root=self.root)
        self.assertEqual((config["order_dollars"], config["risk"]["max_entries_per_day"]), ("25.00", 5))

    def test_values_that_dont_fit_together_fall_back_to_the_file(self):
        desk = self.root / "config/trading-desk.json"
        config = json.loads(desk.read_text())
        config["monitor"]["profit_trail"]["tiers"].append({"from_profit_pct": "10", "giveback_points": "8"})
        desk.write_text(json.dumps(config))
        settings.save(self.root, {"trail_start_pct": "20"}, "test")      # first tier above the second: unusable
        loaded = load_desk_config(root=self.root)
        self.assertTrue(loaded["_dashboard_overrides_ignored"])
        self.assertEqual(loaded["monitor"]["profit_trail"]["tiers"][0]["from_profit_pct"], "6")

    def test_practice_capital_is_not_editable_here(self):
        self.assertNotIn("starting_cash", settings.EDITABLE)
        self.assertNotIn("equity_floor", settings.EDITABLE)


if __name__ == "__main__":
    unittest.main()
