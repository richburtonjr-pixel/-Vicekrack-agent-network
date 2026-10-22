"""HQ Phase 3b: fractional (crypto) quantities in the offline simulator, analytics and the HQ results desk.
All market data here is MOCKED (a fake Alpaca crypto response); nothing touches the network.
"""

import io
import json
import math
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from test_market_crypto import ENV, NOW, SATURDAY_NIGHT, FakeAlpaca, ok, stamp
from vicekrack.hq import api
from vicekrack.trading.analytics import cli as analytics_cli
from vicekrack.trading.analytics.store import AnalyticsStore
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.market.store import MarketStore, load_market_config, make_adapter
from vicekrack.trading.providers.alpaca import load_provider_config
from vicekrack.trading.providers.alpaca_crypto import AlpacaCryptoAdapter
from vicekrack.trading.quantity import notional, round_down, run_is_fractional, to_decimal, to_json
from vicekrack.trading.simulation import cli as sim_cli
from vicekrack.trading.indicators.store import load_indicator_config
from vicekrack.trading.signals.store import load_signal_config
from vicekrack.trading.simulation.engine import run_simulation, validate_run
from vicekrack.trading.simulation.store import SimulationStore

PORT = 8765
HOST = {"host": f"127.0.0.1:{PORT}"}
CRYPTO_POLICY = "config/simulation.crypto.json"


def wave(count, base=60000.0):
    """BTC-like 5-minute bars that rise and fall, so the EMA crossover rule enters and exits."""
    out = []
    for i in range(count):
        price = round(base + 900 * math.sin(i / 6.0) + 7 * i, 2)
        close = round(price + 50 * math.cos(i / 6.0), 2)
        out.append({"t": stamp(SATURDAY_NIGHT + timedelta(minutes=5 * i)), "o": price,
                    "h": round(max(price, close) + 30, 2), "l": round(min(price, close) - 30, 2), "c": close,
                    "v": round(0.5 + (i % 5) / 10, 2), "n": 9, "vw": price})
    return out


def call(main, *argv, root):
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(list(argv), root=root)
    return code, json.loads(output.getvalue())


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.market_config, self.market_sha = load_market_config()

    def crypto_dataset(self, count=120):
        adapter = AlpacaCryptoAdapter(symbol="BTC/USD", interval="5m", start="2026-09-05", end="2026-09-06",
                                      allow_network=True, config=load_provider_config(),
                                      transport=FakeAlpaca(ok(wave(count))), clock=lambda: NOW, environ=ENV)
        return MarketStore(self.root, clock=lambda: NOW).import_dataset(
            adapter, config=self.market_config, config_sha256=self.market_sha, label="historical")

    def stock_dataset(self):
        return MarketStore(self.root, clock=lambda: NOW).import_dataset(
            make_adapter("synthetic", config=self.market_config, fixture="synth1-5m-reclaim"),
            config=self.market_config, config_sha256=self.market_sha)

    def run_crypto(self, policy=CRYPTO_POLICY, record=True):
        dataset = self.crypto_dataset()
        argv = ["sim-run", dataset["dataset_id"], "--policy", policy, "--save"] + (["--record-events"] if record else [])
        code, out = call(sim_cli.main, *argv, root=self.root)
        self.assertEqual(code, 0, out)
        return SimulationStore(self.root).load(out["run_id"]), out


class HelperTests(unittest.TestCase):
    def test_round_down_never_exceeds_budget(self):
        self.assertEqual(round_down(Decimal("500") / Decimal("60030.01")), Decimal("0.00832916"))
        self.assertLessEqual(notional(Decimal("60030.01"), "0.00832916"), Decimal("500"))

    def test_whole_and_fractional_forms(self):
        self.assertEqual(to_json(Decimal(10), False), 10)
        self.assertEqual(to_json(Decimal("0.5"), True), "0.5")
        self.assertEqual(to_json(Decimal(0), True), "0")
        with self.assertRaises(TradingError):
            to_json(Decimal("0.5"), False)
        self.assertEqual(to_decimal(7), Decimal(7))
        self.assertEqual(to_decimal("0.00000001"), Decimal("0.00000001"))
        for bad in (True, "1e-3", "0.000000001", 1.5, "-1"):
            with self.subTest(bad=bad), self.assertRaises(TradingError):
                to_decimal(bad)

    def test_whole_unit_notional_is_exact(self):
        self.assertEqual(notional(Decimal("187.12345678"), 10), Decimal("1871.2345678"))


class CryptoSimulationTests(Base):
    def test_crypto_run_trades_fractions_within_budget(self):
        run, out = self.run_crypto()
        validate_run(run)
        fills = run["fills"]
        self.assertGreater(len(fills), 0, "the crypto practice run should fill orders now")
        self.assertTrue(run_is_fractional(run))
        for fill in fills:
            self.assertIsInstance(fill["quantity"], str)
            quantity = Decimal(fill["quantity"])
            self.assertGreater(quantity, 0)
            self.assertLess(quantity, 1, "$500 buys a fraction of one BTC")
            self.assertLessEqual(-quantity.as_tuple().exponent, 8)
            if fill["side"] == "buy":
                # sized from the last close (estimate <= $500); it fills at the next open, so it can differ slightly,
                # and the order limit is checked again at fill time
                order = next(o for o in run["orders"] if o["order_id"] == fill["order_id"])
                self.assertLessEqual(Decimal(order["estimate"]["notional"]), Decimal("500"))
                self.assertLessEqual(Decimal(fill["notional"]), Decimal(run["policy"]["limits"]["max_order_notional"]))
            # fee = 25 bps of notional, in cents
            self.assertEqual(Decimal(fill["fee"]), (Decimal(fill["notional"]) * Decimal("0.0025")).quantize(Decimal("0.01")))
        self.assertIsInstance(run["summary"]["open_position_quantity"], str)

    def test_exit_sells_the_exact_quantity_bought(self):
        run, _ = self.run_crypto()
        fills = {f["fill_id"]: f for f in run["fills"]}
        closed = [p for p in run["positions"] if p["status"] == "closed"]
        self.assertGreater(len(closed), 0)
        for position in closed:
            self.assertEqual(fills[position["entry_fill_id"]]["quantity"], fills[position["exit_fill_id"]]["quantity"])

    def test_cash_ledger_adds_up_exactly(self):
        run, _ = self.run_crypto()
        total = sum((Decimal(e["amount"]) for e in run["cash_ledger"]), Decimal(0))
        self.assertEqual(str(total.normalize()) if total != total.to_integral() else str(int(total)),
                         run["summary"]["ending_cash"])

    def test_tampered_fractional_quantities_are_refused(self):
        run, _ = self.run_crypto(record=False)
        for change in (lambda r: r["fills"][0].update(quantity="0.123456789"),
                       lambda r: r["fills"][0].update(quantity="0"),
                       lambda r: r["fills"][0].update(quantity=1.5)):
            broken = deepcopy(run)
            change(broken)
            with self.subTest(change=change), self.assertRaises(TradingError):
                validate_run(broken)

    def test_analytics_and_results_desk_accept_fractions(self):
        run, out = self.run_crypto()
        code, report = call(analytics_cli.main, "analytics-generate", run["run_id"], "--save", root=self.root)
        self.assertEqual(code, 0, report)
        saved = AnalyticsStore(self.root).load(report["report_id"])
        self.assertGreater(len(saved["equity_curve"]), 1)
        self.assertTrue(all(isinstance(p["position_quantity"], str) for p in saved["equity_curve"]))
        self.assertTrue(any(Decimal(p["position_quantity"]) > 0 for p in saved["equity_curve"]))
        self.assertTrue(all(isinstance(t["quantity"], str) for t in saved["closed_trades"]["trades"]))
        timeline = out["events"]["timeline_id"]
        for route in (f"/api/results?timeline={timeline}", f"/api/results/summary?timeline={timeline}",
                      f"/api/results/at?timeline={timeline}&position=10"):
            status, _headers, body = api.respond("GET", route, dict(HOST), port=PORT, root=self.root)
            with self.subTest(route=route):
                self.assertEqual(status, 200, body[:300])
        summary = json.loads(api.respond("GET", f"/api/results/summary?timeline={timeline}", dict(HOST), port=PORT,
                                         root=self.root)[2])
        self.assertIsInstance(summary["account"]["open_position_quantity"], str)


class StockBehaviourTests(Base):
    def test_stock_runs_keep_whole_number_quantities(self):
        dataset = self.stock_dataset()
        code, out = call(sim_cli.main, "sim-run", dataset["dataset_id"], "--save", root=self.root)
        self.assertEqual(code, 0, out)
        run = SimulationStore(self.root).load(out["run_id"])
        self.assertFalse(run_is_fractional(run))
        self.assertTrue(all(isinstance(f["quantity"], int) for f in run["fills"]))
        self.assertIsInstance(run["summary"]["open_position_quantity"], int)

    def test_fractional_quantity_refused_for_stocks(self):
        dataset = self.stock_dataset()
        policy = json.loads((ROOT / "config/simulation.paper.json").read_text(encoding="utf-8"))
        policy["sizing"] = {"method": "fixed_quantity", "quantity": "0.5"}
        with self.assertRaises(TradingError) as caught:
            run_simulation(dataset, policy, market_config=self.market_config, indicator_config=load_indicator_config()[0],
                           signal_config=load_signal_config()[0], kill_switch=(False, None), created_at=NOW)
        self.assertEqual(caught.exception.code, "fractional_quantity_not_allowed")


class ScreenTests(unittest.TestCase):
    def test_results_desk_labels_fractions_as_units_and_zero_as_none(self):
        js = (ROOT / "vicekrack/hq/static/app.js").read_text(encoding="utf-8")
        self.assertIn("function quantityText(q)", js)
        self.assertIn('Number(q) > 0', js)
        self.assertNotIn('p.open_quantity ? p.open_quantity + " shares"', js)
        self.assertNotIn('a.open_position_quantity ? a.open_position_quantity + " shares"', js)


if __name__ == "__main__":
    unittest.main()
