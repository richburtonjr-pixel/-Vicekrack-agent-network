"""The Alpaca PAPER key helper: confirmation, input checks, and no secret printed. No Windows needed."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "save_alpaca_paper_keys.py"
BAT = ROOT / "scripts" / "windows" / "Save Alpaca Paper Keys.bat"


def load_script():
    spec = importlib.util.spec_from_file_location("save_alpaca_paper_keys", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AlpacaPaperKeyHelperTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_script()

    def test_accepts_paper_confirmation_and_two_different_keys(self):
        self.assertIsNone(self.helper.check_inputs("PAPER", "PKTESTKEYID123", "testsecretvalue456"))

    def test_refuses_without_the_paper_confirmation(self):
        for confirm in ("", "LIVE", "paper ", "yes"):
            self.assertIsNotNone(self.helper.check_inputs(confirm, "PKTESTKEYID123", "testsecretvalue456"))

    def test_refuses_empty_spaced_or_identical_keys(self):
        self.assertIsNotNone(self.helper.check_inputs("PAPER", "", "testsecretvalue456"))
        self.assertIsNotNone(self.helper.check_inputs("PAPER", "has space", "testsecretvalue456"))
        self.assertIsNotNone(self.helper.check_inputs("PAPER", "x" * 129, "testsecretvalue456"))
        self.assertIsNotNone(self.helper.check_inputs("PAPER", "samevalue", "samevalue"))

    def test_only_the_names_are_printed_never_the_values(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for printed in [line for line in source.splitlines() if "print(" in line]:
            self.assertNotIn("key_id", printed)
            self.assertNotIn("secret", printed.replace("secret key", "").replace("secret_key", ""))

    def test_targets_use_the_vicekrack_namespace(self):
        self.assertTrue(self.helper.KEY_ID_TARGET.startswith("ViceKrack/"))
        self.assertTrue(self.helper.SECRET_TARGET.startswith("ViceKrack/"))

    def test_double_click_file_runs_the_helper(self):
        text = BAT.read_text(encoding="utf-8")
        self.assertIn("save_alpaca_paper_keys.py", text)
        self.assertIn("_find_python.bat", text)


if __name__ == "__main__":
    unittest.main()
