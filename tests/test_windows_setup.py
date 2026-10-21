"""The double-click Windows setup: it exists where people look, points at real files and never holds a secret."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "Setup ViceKrack Mobile.bat"
WINDOWS = ROOT / "scripts" / "windows"


class WindowsSetupTests(unittest.TestCase):
    def setUp(self):
        self.text = SETUP.read_text(encoding="utf-8")

    def test_setup_is_in_the_main_folder(self):
        self.assertTrue(SETUP.is_file())

    def test_every_helper_it_uses_exists(self):
        self.assertTrue((WINDOWS / "_find_python.bat").is_file())
        for name in ("Start ViceKrack (PC + Phone).bat", "2 Pair my iPhone.bat"):
            self.assertIn(name, self.text)
            self.assertTrue((WINDOWS / name).is_file(), name)

    def test_runs_the_secure_mobile_steps(self):
        for step in ("mobile-save-key", "mobile-setup", "winkey import save_key", "tailscale.exe"):
            self.assertIn(step, self.text)
        commands = "\n".join(line for line in self.text.lower().splitlines() if not line.strip().startswith("rem"))
        self.assertNotIn("funnel", commands)

    def test_never_contains_or_prints_a_key(self):
        self.assertNotRegex(self.text, r"xai-[A-Za-z0-9]{10,}")
        self.assertNotRegex(self.text, r"(?i)set\s+\"?XAI_API_KEY=")
        self.assertNotRegex(self.text, r"(?i)echo[^\n]*%XAI_API_KEY%")

    def test_every_goto_has_a_label(self):
        labels = set(re.findall(r"(?m)^:(\w+)", self.text))
        for target in re.findall(r"(?i)goto :?(\w+)", self.text):
            if target.lower() != "eof":
                self.assertIn(target, labels)

    def test_no_parenthesis_blocks(self):
        # Paths such as "(PC + Phone)" break cmd.exe ( ... ) blocks, so the file uses goto only.
        self.assertNotRegex(self.text, r"(?m)^\s*(if|for)\b[^\n]*\($")


if __name__ == "__main__":
    unittest.main()
