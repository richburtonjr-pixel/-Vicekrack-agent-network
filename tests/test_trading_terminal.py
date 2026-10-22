"""Trading terminal page (read-only): it is wired in, serves its script, and shows no fake prices."""

import unittest
from pathlib import Path

from vicekrack.hq import api

STATIC = Path(api.STATIC)


class TradingTerminalTests(unittest.TestCase):
    def setUp(self):
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.js = (STATIC / "terminal.js").read_text(encoding="utf-8")
        self.app = (STATIC / "app.js").read_text(encoding="utf-8")

    def test_view_button_and_section_exist(self):
        self.assertIn('data-view="terminal"', self.html)
        self.assertIn('id="terminal-view"', self.html)
        self.assertIn('id="terminal-body"', self.html)

    def test_script_is_loaded_and_served(self):
        self.assertIn('/static/terminal.js', self.html)
        self.assertIn("/static/terminal.js", api.FILES)

    def test_view_switch_shows_only_the_terminal(self):
        self.assertIn('$("terminal-view").hidden = !terminal;', self.app)
        self.assertIn('window.HQTerminal.load()', self.app)

    def test_only_read_only_routes_are_called(self):
        self.assertIn('"/api/sessions"', self.js)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertNotIn(f'method: "{method}"', self.js)
        self.assertNotIn("/api/studio", self.js)
        self.assertNotIn("/api/broker-paper/intent", self.js)

    def test_no_price_or_random_data_is_invented(self):
        self.assertNotIn("Math.random", self.js)
        self.assertNotIn("https://", self.js)
        self.assertNotIn("WebSocket", self.js)


if __name__ == "__main__":
    unittest.main()
