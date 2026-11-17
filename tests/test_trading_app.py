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
                                 "/m/api/trade/coin", "/m/api/trade/order", "/m/api/trade/restart",
                                 "/m/api/trade/desk", "/m/api/trade/results"})
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


class TradingDesk(unittest.TestCase):
    """GET /m/api/trade/desk: the HQ's eight-agent board and case files, read from disk. No network, no trading."""

    def setUp(self):
        import shutil
        import tempfile
        from desk_market import epoch, now_text
        from test_watch_loop import seed_tape
        from vicekrack.mobile.trade_api import TradeApi
        from vicekrack.trading.practice.store import PracticeStore, load_config
        from vicekrack.trading.watch.cycle import load_watch_config, run_cycle
        from desk_market import FakeKraken
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(ROOT / "config", self.root / "config")
        PracticeStore(self.root).start(load_config(root=self.root), now_text(0))
        seed_tape(self.root, 600)
        run_cycle(load_watch_config(root=self.root), root=self.root, now=now_text(600),
                  transport=FakeKraken(600), clock=lambda: epoch(600))

        def no_network(*_args, **_kwargs):
            raise AssertionError("the desk must not use the network")
        self.api = TradeApi(self.root, transport=no_network, clock=lambda: epoch(601))

    def test_eight_agents_decisions_and_researched_coins(self):
        desk = self.api.desk()
        self.assertEqual(desk["contract"], "trade_desk")
        self.assertTrue(desk["simulated"])
        self.assertEqual(len(desk["agents"]), 8)
        self.assertTrue(desk["decisions"])                   # the fake market's SOL streak opens a case
        self.assertTrue(all(step["agent"] for c in desk["decisions"] for step in c["steps"]))
        self.assertTrue(desk["researched"])
        self.assertNotIn("candles", desk["researched"][0])   # light: no chart data

    def test_reading_the_desk_never_trades(self):
        from vicekrack.trading.practice.store import PracticeStore, load_config
        before = PracticeStore(self.root).load(load_config(root=self.root))
        self.api.desk()
        self.assertEqual(PracticeStore(self.root).load(load_config(root=self.root)), before)


class TradingResults(TradingDesk):
    """GET /m/api/trade/results: worked out from the practice ledger only."""

    def test_curve_matches_the_ledger(self):
        res = self.api.results()
        self.assertEqual(res["contract"], "trade_results")
        sells = [p for p in res["curve"] if p["event"] == "sell"]
        self.assertEqual(len(sells), res["scores"]["all"]["closed_trades"])
        self.assertEqual(res["curve"][0]["event"], "start")
        from decimal import Decimal
        locked = Decimal(res["curve"][0]["balance"]) + sum(Decimal(p["pnl"]) for p in sells)
        self.assertEqual(locked - Decimal(res["account"]["starting_cash"]), Decimal(res["account"]["realized_pnl"]))

    def test_fees_and_drawdown_are_plain_numbers(self):
        from decimal import Decimal
        res = self.api.results()
        self.assertEqual(Decimal(res["fees"]["total"]), Decimal(res["fees"]["you"]) + Decimal(res["fees"]["agents"]))
        self.assertGreaterEqual(Decimal(res["drawdown"]["dollars"]), 0)

    def test_reading_results_never_trades(self):
        from vicekrack.trading.practice.store import PracticeStore, load_config
        before = PracticeStore(self.root).load(load_config(root=self.root))
        self.api.results()
        self.assertEqual(PracticeStore(self.root).load(load_config(root=self.root)), before)


class TradingDeskGateway(MobileBase):
    def test_needs_a_paired_phone(self):
        for path in ("/m/api/trade/desk", "/m/api/trade/results"):
            status, _, body = self.gw.respond("GET", path, self.remote())
            self.assertEqual(status, 401, path)


if __name__ == "__main__":
    unittest.main()
