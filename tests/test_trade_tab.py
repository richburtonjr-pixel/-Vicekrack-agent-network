"""Trading PR 4: whole Kraken USD market, your own practice trades, and the phone app's Trade tab."""

import json
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_market import FakeKraken, epoch  # noqa: E402
from test_mobile import Base as MobileBase  # noqa: E402

from vicekrack.mobile.gateway import Gateway  # noqa: E402
from vicekrack.mobile.trade_api import TradeApi  # noqa: E402
from vicekrack.trading.providers import kraken_market  # noqa: E402
from vicekrack.trading.errors import TradingError  # noqa: E402
from vicekrack.trading.practice import manual  # noqa: E402
from vicekrack.trading.practice.store import PracticeStore, load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MINUTE = 300


class Folder(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "config", self.root / "config")
        self.config = load_config(root=self.root)
        self.store = PracticeStore(self.root)
        self.kraken = FakeKraken(MINUTE)
        self.clock = lambda: epoch(MINUTE)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class KrakenMarketTests(Folder):
    def test_catalog_uses_app_names_and_skips_other_markets(self):
        coins = kraken_market.Catalog(self.root, transport=self.kraken, clock=self.clock).coins()
        self.assertEqual(sorted(coins), ["BTCUSD", "DOGEUSD", "DUSTUSD", "ETHUSD", "SOLUSD", "TINYUSD", "USDTUSD"])
        self.assertEqual((coins["BTCUSD"]["pair"], coins["BTCUSD"]["name"]), ("XBTUSD", "Bitcoin"))
        self.assertEqual(coins["DOGEUSD"]["pair"], "XDGUSD")

    def test_catalog_is_cached_and_survives_an_outage(self):
        kraken_market.Catalog(self.root, transport=self.kraken, clock=self.clock).coins()
        offline = FakeKraken(MINUTE, fail={"AssetPairs"})
        later = lambda: epoch(MINUTE) + 13 * 3600                                # older than 12 hours
        self.assertIn("SOLUSD", kraken_market.Catalog(self.root, transport=offline, clock=later).coins())
        with self.assertRaises(TradingError):
            kraken_market.Catalog(Path(tempfile.mkdtemp()), transport=offline, clock=later).coins()

    def test_one_request_prices_every_coin(self):
        catalog = kraken_market.Catalog(self.root, transport=self.kraken, clock=self.clock)
        rows = kraken_market.tickers(catalog, self.kraken)
        self.assertEqual(self.kraken.calls.count("Ticker"), 1)
        self.assertEqual(len(rows), 7)
        sol = rows["SOLUSD"]
        self.assertTrue(Decimal(sol["ask"]) > Decimal(sol["last"]) > Decimal(sol["bid"]))
        self.assertTrue(sol["tradeable"])
        self.assertFalse(rows["DUSTUSD"]["tradeable"])                          # too cheap for 8 decimal places
        self.assertEqual(set(kraken_market.tickers(catalog, self.kraken, symbols=["SOLUSD"])), {"SOLUSD"})

    def test_prices_are_rounded_for_the_account_and_tiny_ones_refused(self):
        self.assertEqual(kraken_market.to_price("0.000012345678"), "0.00001235")
        self.assertEqual(kraken_market.to_price("150.10000"), "150.1")
        self.assertIsNone(kraken_market.to_price("0.0000002"))
        self.assertIsNone(kraken_market.to_price("1e5"))

    def test_chart(self):
        catalog = kraken_market.Catalog(self.root, transport=self.kraken, clock=self.clock)
        points = kraken_market.chart(catalog, "SOLUSD", "1H", self.kraken)
        self.assertEqual(len(points), 60)
        with self.assertRaises(TradingError):
            kraken_market.chart(catalog, "SOLUSD", "5Y", self.kraken)


class ManualTradeTests(Folder):
    def setUp(self):
        super().setUp()
        self.store.start(self.config, "2026-11-12T00:00:00Z")

    def order(self, symbol, side, amount, kraken=None):
        return manual.place_order(symbol, side, amount, root=self.root, transport=kraken or self.kraken, clock=self.clock)

    def test_buy_uses_a_fresh_kraken_ask_and_is_yours(self):
        result = self.order("SOLUSD", "buy", "20")
        self.assertTrue(result["ok"], result)
        ask = Decimal(kraken_market.tickers(kraken_market.Catalog(self.root, transport=self.kraken, clock=self.clock),
                                            self.kraken, symbols=["SOLUSD"])["SOLUSD"]["ask"])
        self.assertEqual(Decimal(result["fill"]["price"]), ask.quantize(Decimal("0.00000001")))
        self.assertEqual(result["fill"]["by"], "you")
        self.assertEqual(result["account"]["positions"]["SOLUSD"]["opened_by"], "you")

    def test_sell_part_then_all_at_the_bid(self):
        self.order("DOGEUSD", "buy", "30")
        result = self.order("DOGEUSD", "sell", "10")
        self.assertTrue(result["ok"])
        self.assertIn("DOGEUSD", result["account"]["positions"])
        result = self.order("DOGEUSD", "sell", "all")
        self.assertNotIn("DOGEUSD", result["account"]["positions"])

    def test_every_holding_is_repriced_before_a_trade(self):
        self.order("ETHUSD", "buy", "30")
        self.order("SOLUSD", "buy", "30")
        state = self.store.load(self.config)
        self.assertEqual(set(state["marks"]), {"ETHUSD", "SOLUSD"})

    def test_bad_requests_trade_nothing(self):
        for args, code in ((("SOLUSD", "short", "10"), "invalid_side"), (("SOLUSD", "buy", "-5"), "invalid_amount"),
                           (("SOLUSD", "buy", "1e3"), "invalid_amount"), (("NOPEUSD", "buy", "10"), "unknown_coin"),
                           (("DUSTUSD", "buy", "10"), "not_tradeable")):
            with self.assertRaises(TradingError) as caught:
                self.order(*args)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.store.load(self.config)["positions"], {})
        result = self.order("SOLUSD", "buy", "500")
        self.assertEqual(result["refused"]["code"], "order_too_large")

    def test_a_crash_at_trade_time_trips_the_guard(self):
        self.order("ETHUSD", "buy", "all")
        crash = FakeKraken(MINUTE, scale={"XETHZUSD": 0.5})
        result = self.order("SOLUSD", "buy", "1", kraken=crash)
        self.assertFalse(result["ok"])
        self.assertEqual(result["refused"]["code"], "equity_floor_reached")
        self.assertEqual(self.store.load(self.config)["status"], "halted")


class TradeTabGatewayTests(MobileBase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "config", self.root / "config")
        self.trade = TradeApi(self.root, transport=FakeKraken(MINUTE), clock=lambda: epoch(MINUTE))
        self.gw = Gateway(self.store, self.hq, port=8770, clock=lambda: self.now, trade=self.trade)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def test_needs_a_paired_phone(self):
        for path in ("/m/api/trade/markets", "/m/api/trade/account", "/m/api/trade/coin?symbol=SOLUSD"):
            status, _, doc = self.call("GET", path)
            self.assertEqual((status, doc["error"]["code"]), (401, "not_paired"))

    def test_markets_coin_and_account(self):
        headers = self.paired()
        status, _, doc = self.call("GET", "/m/api/trade/markets", headers=headers)
        self.assertEqual(status, 200)
        volumes = [int(c["volume_24h_usd"]) for c in doc["coins"]]
        self.assertEqual(volumes, sorted(volumes, reverse=True))                # biggest markets first
        self.assertIn("SOLUSD", [c["symbol"] for c in doc["coins"]])
        status, _, doc = self.call("GET", "/m/api/trade/coin?symbol=SOLUSD&range=1D", headers=headers)
        self.assertEqual((status, doc["ticker"]["base"]), (200, "SOL"))
        self.assertTrue(doc["chart"])
        status, _, doc = self.call("GET", "/m/api/trade/account", headers=headers)
        self.assertEqual((status, doc["needs_start"]), (200, True))

    def test_start_trade_and_audit(self):
        headers = self.paired()
        status, _, doc = self.call("POST", "/m/api/trade/restart", {"confirm": "restart-practice"}, headers=headers)
        self.assertEqual(status, 200, doc)
        status, _, doc = self.call("POST", "/m/api/trade/order", {"symbol": "SOLUSD", "side": "buy", "amount": "15"},
                                   headers=headers)
        self.assertEqual((status, doc["ok"]), (200, True), doc)
        status, _, doc = self.call("GET", "/m/api/trade/account", headers=headers)
        self.assertEqual([h["symbol"] for h in doc["holdings"]], ["SOLUSD"])
        self.assertEqual(doc["trades"][0]["by"], "you")
        self.assertIn("trade/order", [line["action"] for line in self.store.audit()])

    def test_orders_need_the_csrf_header_and_carry_no_price(self):
        headers = self.paired()
        self.call("POST", "/m/api/trade/restart", {"confirm": "restart-practice"}, headers=headers)
        no_csrf = {k: v for k, v in headers.items() if k != "x-vk-mobile-csrf"}
        status, _, doc = self.call("POST", "/m/api/trade/order", {"symbol": "SOLUSD", "side": "buy", "amount": "5"},
                                   headers=no_csrf)
        self.assertEqual(doc["error"]["code"], "session_invalid")
        status, _, doc = self.call("POST", "/m/api/trade/order",
                                   {"symbol": "SOLUSD", "side": "buy", "amount": "5", "price": "0.01"}, headers=headers)
        self.assertEqual((status, doc["error"]["code"]), (400, "invalid_request"))

    def test_restart_needs_the_confirmation(self):
        headers = self.paired()
        status, _, doc = self.call("POST", "/m/api/trade/restart", {"confirm": "yes"}, headers=headers)
        self.assertEqual(doc["error"]["code"], "restart_not_confirmed")


class TradeScreenFiles(unittest.TestCase):
    def test_trade_tab_is_shipped_and_cached(self):
        static = ROOT / "vicekrack/mobile/static"
        self.assertIn('href="#trade"', (static / "index.html").read_text())
        self.assertIn("/static/trade.js", (static / "sw.js").read_text())
        script = (static / "trade.js").read_text()
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("price:", script.split("/m/api/trade/order")[1][:80])   # the phone never sends a price
        self.assertIn("Practice money", script)


if __name__ == "__main__":
    unittest.main()
