"""ViceKrack Trading 2.0 (stage 1): the standalone /trading page. No network.

The page adds no new PC requests: it reads and orders through the same paired /m/api/trade/* requests as the
Trade tab, so the $80 guard, PC-side pricing, CSRF and audit rules all still apply."""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mobile import Base as MobileBase  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "vicekrack/mobile/static"


class TradingAppFiles(MobileBase):
    def test_files_served_with_the_same_security_headers(self):
        expected = {"/trading": "text/html", "/static/trading.js": "text/javascript",
                    "/static/trading.css": "text/css", "/trading.webmanifest": "application/manifest+json"}
        for path, kind in expected.items():
            status, headers, body = self.gw.respond("GET", path, self.remote())
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(kind), path)
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"], path)
            self.assertTrue(body, path)

    def test_installable_as_its_own_home_screen_app(self):
        manifest = json.loads((STATIC / "trading.webmanifest").read_text())
        self.assertEqual((manifest["display"], manifest["start_url"]), ("standalone", "/trading#home"))
        html = (STATIC / "trading.html").read_text()
        self.assertIn('href="/trading.webmanifest"', html)
        self.assertIn('name="apple-mobile-web-app-capable"', html)

    def test_practice_mode_always_shown(self):
        html = (STATIC / "trading.html").read_text()
        header = html.split('<header class="vt-top">')[1].split("</header>")[0]
        self.assertIn("PRACTICE MODE", header)               # in the fixed top bar, on every screen

    def test_four_sections_in_the_tab_bar(self):
        html = (STATIC / "trading.html").read_text()
        for tab in ("home", "market", "portfolio", "desk", "performance"):
            self.assertIn(f'data-tab="{tab}"', html)


class TradingAppScript(unittest.TestCase):
    script = (STATIC / "trading.js").read_text()

    def test_text_only_never_html(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, self.script)

    def test_uses_only_existing_pc_requests(self):
        paths = set(re.findall(r'"(/m/api/[a-z/]+)', self.script))
        self.assertEqual(paths, {"/m/api/session", "/m/api/trade/account", "/m/api/trade/markets",
                                 "/m/api/trade/coin", "/m/api/trade/order", "/m/api/trade/restart"})
        code = self.script.replace('"http://www.w3.org/2000/svg"', "")   # the SVG namespace is not a request
        for outside in ("kraken.com", "alpaca.markets", "http://", "https://"):
            self.assertNotIn(outside, code)            # never talks to a broker or exchange directly

    def test_orders_never_carry_a_price(self):
        body = self.script.split('post("/m/api/trade/order",')[1][:60]
        self.assertEqual(body.strip().split("}")[0], "{ symbol, side, amount ")

    def test_portfolio_is_built_from_the_account_read(self):
        self.assertIn('tab === "portfolio"', self.script)
        screen = self.script.split("async function portfolioScreen")[1].split("\n}\n")[0]
        self.assertIn('call("/m/api/trade/account")', screen)
        self.assertNotIn("post(", screen)                     # looking at the portfolio never trades

    def test_no_made_up_numbers(self):
        self.assertNotIn("Math.random", self.script)


if __name__ == "__main__":
    unittest.main()
