"""Step 29: bounded offline paper-execution simulation. Synthetic data only; no network, no credits.

Expected prices, fees, cash, P&L and timing are computed independently in this file
(exact Fractions with explicit half-even rounding), not with the simulator's helpers.
"""

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.indicators.store import load_indicator_config
from vicekrack.trading.market.store import MarketStore, load_market_config, make_adapter
from vicekrack.trading.signals.store import load_signal_config
from vicekrack.trading.simulation.cli import main as sim_main
from vicekrack.trading.simulation.engine import run_simulation, validate_run
from vicekrack.trading.simulation.store import SimulationStore, load_policy
from vicekrack.trading.state import PaperAccount

NOW = "2026-10-05T12:00:00Z"

# Hand-built bars (open, high, low, close). Breakout lookback 1 triggers on bars 3 and 6.
BARS = [("10", "10", "10", "10"), ("10", "10", "9", "9.5"), ("9.5", "11", "9.5", "10.5"), ("10.6", "10.8", "10.4", "10.7"),
        ("10.7", "10.9", "10.6", "10.8"), ("11.0", "11.2", "10.9", "11.1"), ("11.2", "11.3", "11.1", "11.2")]


def r8(value):
    """Exact half-even rounding to 8 places (independent of the module)."""
    return _round(Fraction(value), 8)


def cents(value):
    return _round(Fraction(value), 2)


def _round(value, places):
    scaled = value * 10 ** places
    whole = scaled.numerator // scaled.denominator
    rest = scaled - whole
    if rest > Fraction(1, 2) or (rest == Fraction(1, 2) and whole % 2):
        whole += 1
    return Fraction(whole, 10 ** places)


def text(value):
    """Decimal text without trailing zeros, like the contracts."""
    value = Fraction(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for places in range(0, 9):
        scaled = value * 10 ** places
        if scaled.denominator == 1:
            digits = str(scaled.numerator).rjust(places + 1, "0")
            out = digits if places == 0 else f"{digits[:-places]}.{digits[-places:]}"
            return "0" if out == "0" else sign + out
    raise AssertionError("more than 8 places")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.market_config, _ = load_market_config()
        self.indicator_config, _ = load_indicator_config()
        shipped, _ = load_signal_config()
        self.signal_config = deepcopy(shipped)
        self.signal_config["strategies"].update({
            "b1": {"strategy": "breakout", "version": "1.0",
                   "params": {"lookback": 1, "volume_filter": None, "cooldown_bars": 0, "expiry_bars": 1}},
            "b1x3": {"strategy": "breakout", "version": "1.0",
                     "params": {"lookback": 1, "volume_filter": None, "cooldown_bars": 0, "expiry_bars": 3}}})
        self.policy, _ = load_policy()
        self.policy = deepcopy(self.policy)
        self.policy.update(entry={"strategies": ["b1"], "reject_across_gaps": True},
                           sizing={"method": "fixed_quantity", "quantity": 10},
                           exits={"opposite_ema_crossover": None, "max_holding_bars": 2},
                           costs={"slippage_bps": "10", "fee_per_order": "1.00", "fee_bps": "5"})
        self.store = MarketStore(self.root, clock=lambda: NOW)
        self.files = 0

    def dataset(self, bars=BARS, times=None, volume="1000"):
        key = (tuple(bars), tuple(times or ()), volume)
        cache = self.__dict__.setdefault("_datasets", {})
        if key in cache:                                   # the same bytes may only be imported once
            return cache[key]
        cache[key] = self._import(bars, times, volume)
        return cache[key]

    def _import(self, bars, times, volume):
        self.files += 1
        lines = ["timestamp,open,high,low,close,volume"]
        first = datetime(2026, 1, 15, 9, 30)
        for i, (o, h, low, c) in enumerate(bars):
            moment = datetime.fromisoformat(times[i]) if times else first + timedelta(minutes=5 * i)
            lines.append(f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}-05:00,{o},{h},{low},{c},{volume}")
        path = self.root / "inputs" / f"bars-{self.files}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.store.import_dataset(make_adapter("csv", config=self.market_config, file=str(path), symbol="SYNTH1"),
                                         config=self.market_config, config_sha256="0" * 64, interval="5m",
                                         tz="America/New_York", label="synthetic")

    def simulate(self, dataset, policy=None, kill_switch=(False, None), **window):
        return run_simulation(dataset, policy or self.policy, market_config=self.market_config,
                              indicator_config=self.indicator_config, signal_config=self.signal_config,
                              kill_switch=kill_switch, created_at=NOW, **window)

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception

    @staticmethod
    def order_view(run):
        return [(o["purpose"], o["source"]["decision_bar_sequence"], o["status"], o["history"][-1]["reason_codes"][0])
                for o in run["orders"]]


class AccountingTests(Base):
    def test_fills_cash_and_pnl_by_hand(self):
        run = self.simulate(self.dataset())
        bps, fixed, fee_bps = Fraction(10, 10000), Fraction(1), Fraction(5, 10000)
        # Entry: signal on bar 3 -> buy at bar 4 open 10.6 (+10 bps).
        buy1 = r8(Fraction("10.6") * (1 + bps))
        notional1 = buy1 * 10
        fee1 = cents(fixed + notional1 * fee_bps)
        cost1 = notional1 + fee1
        # Exit: held bars 4 and 5 -> max_holding_bars=2 decided at bar 5 close -> sell at bar 6 open 11.0 (-10 bps).
        sell1 = r8(Fraction("11.0") * (1 - bps))
        proceeds1 = sell1 * 10
        fee2 = cents(fixed + proceeds1 * fee_bps)
        realized = proceeds1 - fee2 - cost1
        # Re-entry: signal on bar 6 -> buy at bar 7 open 11.2; still open at the end, marked at close 11.2.
        buy2 = r8(Fraction("11.2") * (1 + bps))
        notional2 = buy2 * 10
        fee3 = cents(fixed + notional2 * fee_bps)
        cost2 = notional2 + fee3
        cash = 10000 - cost1 + proceeds1 - fee2 - cost2
        unrealized = Fraction("11.2") * 10 - cost2
        self.assertEqual([(f["bar_sequence"], f["side"], f["fill_price"], f["notional"], f["fee"]) for f in run["fills"]],
                         [(4, "buy", text(buy1), text(notional1), text(fee1)), (6, "sell", text(sell1), text(proceeds1), text(fee2)),
                          (7, "buy", text(buy2), text(notional2), text(fee3))])
        self.assertEqual((text(buy1), text(fee1), text(sell1), text(fee2)), ("10.6106", "1.05", "10.989", "1.05"))
        summary = run["summary"]
        self.assertEqual((summary["realized_pnl"], summary["unrealized_pnl"], summary["ending_cash"], summary["ending_equity"]),
                         (text(realized), text(unrealized), text(cash), text(cash + Fraction("11.2") * 10)))
        self.assertEqual(summary["fees_total"], text(fee1 + fee2 + fee3))
        self.assertEqual([e["kind"] for e in run["cash_ledger"]], ["initial_cash", "buy", "fee", "sell", "fee", "buy", "fee"])
        self.assertEqual(run["cash_ledger"][-1]["balance_after"], text(cash))
        closed, open_ = run["positions"]
        self.assertEqual((closed["status"], closed["realized_pnl"], closed["cost_basis"]), ("closed", text(realized), text(cost1)))
        self.assertEqual((open_["status"], open_["mark_price"], open_["unrealized_pnl"]), ("open", "11.2", text(unrealized)))
        self.assertEqual((summary["open_position_quantity"], summary["orders_filled"]), (10, 3))
        self.assertTrue(all(x["simulated"] for x in run["orders"] + run["fills"] + run["positions"] + run["cash_ledger"]))

    def test_zero_costs_and_half_even_fee_rounding(self):
        policy = deepcopy(self.policy)
        policy["costs"] = {"slippage_bps": "0", "fee_per_order": "1.00", "fee_bps": "0.5"}
        run = self.simulate(self.dataset(), policy)
        first = run["fills"][0]
        # 10 x 10.6 = 106 ; fee = 1 + 106 x 0.00005 = 1.0053 -> 1.01 (above half). Exit at 11.0: 110 -> 1.0055 -> 1.01.
        self.assertEqual((first["fill_price"], first["notional"], first["fee"]), ("10.6", "106", "1.01"))
        self.assertEqual(text(cents(1 + Fraction(100) * Fraction(5, 100000))), "1")       # 1.005 -> 1.00 (half-even)
        exact = deepcopy(policy)
        exact["costs"]["fee_bps"] = "0"
        self.assertEqual(self.simulate(self.dataset(BARS[:4] + [("10.7", "10.9", "10.6", "10.8")]), exact)["fills"][0]["fee"], "1")


class ExitTests(Base):
    def test_max_holding_exit_timing(self):
        run = self.simulate(self.dataset())
        exit_order = next(o for o in run["orders"] if o["purpose"] == "exit")
        self.assertEqual((exit_order["source"]["rule"], exit_order["source"]["decision_bar_sequence"]), ("max_holding_bars", 5))
        self.assertEqual(run["fills"][1]["bar_sequence"], 6)                         # next available open, never intrabar
        self.assertEqual(run["fills"][1]["open_price"], "11.0")

    def test_opposite_ema_crossover_exit(self):
        dataset = self.store.import_dataset(make_adapter("synthetic", config=self.market_config, fixture="synth1-5m-reclaim"),
                                            config=self.market_config, config_sha256="0" * 64)
        shipped, _ = load_policy()
        run = self.simulate(dataset, shipped)
        exit_order = next(o for o in run["orders"] if o["purpose"] == "exit")
        self.assertEqual((exit_order["source"]["rule"], exit_order["history"][0]["reason_codes"][0]),
                         ("opposite_ema_crossover", "ema_crossed_below"))
        sell = next(f for f in run["fills"] if f["side"] == "sell")
        self.assertEqual(sell["bar_sequence"], exit_order["source"]["decision_bar_sequence"] + 1)


class RejectionTests(Base):
    def test_insufficient_cash_at_acceptance_and_at_fill(self):
        policy = deepcopy(self.policy)
        policy["account"]["initial_cash"] = "100.00"
        run = self.simulate(self.dataset(), policy)
        self.assertIn("insufficient_cash_estimate", run["orders"][0]["history"][0]["reason_codes"])
        self.assertEqual(run["fills"], [])
        # Estimate at bar 3 close 10.5: 105.105 + 1.05 = 106.155 <= 107; fill at 10.6 open: 106.106 + 1.05 = 107.156 > 107.
        policy["account"]["initial_cash"] = "107.00"
        run = self.simulate(self.dataset(), policy)
        first = run["orders"][0]
        self.assertEqual((first["status"], first["quantity"], first["history"][-1]["reason_codes"]),
                         ("rejected", 10, ["insufficient_cash"]))                    # never resized
        self.assertEqual(run["summary"]["ending_cash"], "107")

    def test_exposure_limits(self):
        policy = deepcopy(self.policy)
        policy["limits"]["max_order_notional"] = "100.00"
        self.assertIn("order_notional_limit", self.simulate(self.dataset(), policy)["orders"][0]["history"][0]["reason_codes"])
        policy = deepcopy(self.policy)
        policy["limits"]["max_position_notional"] = "106.00"                      # estimate 105.105 ok, fill 106.106 not
        order = self.simulate(self.dataset(), policy)["orders"][0]
        self.assertEqual((order["status"], order["history"][-1]["reason_codes"]), ("rejected", ["position_exposure_limit_at_fill"]))

    def test_pending_and_open_position_block_new_entries(self):
        policy = deepcopy(self.policy)
        policy["entry"]["strategies"] = ["b1", "b1x3"]                              # two signals on the same bars
        policy["exits"]["max_holding_bars"] = 10
        run = self.simulate(self.dataset(), policy)
        views = self.order_view(run)
        self.assertIn(("entry", 3, "rejected", "pending_order_exists"), views)
        self.assertIn(("entry", 6, "rejected", "position_already_open"), views)
        self.assertEqual(len(run["fills"]), 1)

    def test_order_limit_and_sizing(self):
        policy = deepcopy(self.policy)
        policy["limits"]["max_orders"] = 1
        views = self.order_view(self.simulate(self.dataset(), policy))
        self.assertEqual(views[-1], ("entry", 6, "rejected", "order_limit_reached"))
        self.assertEqual(views[1][0], "exit")                                       # exits are never blocked by the limit
        notional = deepcopy(self.policy)
        notional["sizing"] = {"method": "fixed_notional", "notional": "105.00"}
        # Reference = 10.5 x 1.001 = 10.5105 -> floor(105 / 10.5105) = 9 shares.
        self.assertEqual(self.simulate(self.dataset(), notional)["orders"][0]["quantity"],
                         int(Fraction("105") / r8(Fraction("10.5") * Fraction(10010, 10000))))
        notional["sizing"]["notional"] = "5.00"
        order = self.simulate(self.dataset(), notional)["orders"][0]
        self.assertEqual((order["quantity"], order["history"][0]["reason_codes"]), (None, ["sizing_zero_quantity"]))

    def test_kill_switch(self):
        run = self.simulate(self.dataset(), kill_switch=(True, "switch_file"))
        self.assertEqual({o["history"][0]["reason_codes"][0] for o in run["orders"]}, {"kill_switch_engaged"})
        self.assertEqual(run["fills"], [])
        store = SimulationStore(self.root)
        self.assertEqual(store.kill_switch(self.policy), (False, None))
        store.set_kill_switch(True, NOW)
        self.assertEqual(store.kill_switch(self.policy), (True, "switch_file"))
        store.switch.write_text("{bad", encoding="utf-8")
        self.assertEqual(store.kill_switch(self.policy), (True, "switch_file_unreadable"))
        engaged = deepcopy(self.policy)
        engaged["kill_switch"]["engaged"] = True
        store.set_kill_switch(False, NOW)
        self.assertEqual(store.kill_switch(engaged), (True, "policy"))


class GapExpiryTests(Base):
    def test_entry_rejected_across_gap_and_exit_fills_across_gap(self):
        times = ["2026-01-15T09:30:00", "2026-01-15T09:35:00", "2026-01-15T09:40:00", "2026-01-15T09:50:00",
                 "2026-01-15T09:55:00", "2026-01-15T10:00:00", "2026-01-15T10:05:00"]       # 09:45 missing
        entry_gap = self.simulate(self.dataset(times=times))                        # signal bar 3, next bar after a gap
        self.assertEqual(entry_gap["orders"][0]["history"][-1]["reason_codes"], ["entry_gap"])
        # Exit across a gap: hold 2 bars (4, 5), exit decided at bar 5, next bar after a gap.
        times = [f"2026-01-15T09:{m:02d}:00" for m in (30, 35, 40, 45, 50)] + ["2026-01-15T10:00:00", "2026-01-15T10:05:00"]
        run = self.simulate(self.dataset(times=times))
        sell = next(f for f in run["fills"] if f["side"] == "sell")
        self.assertEqual((sell["bar_sequence"], sell["gap_before_fill"], sell["reason_codes"]),
                         (6, 1, ["filled_at_bar_open", "gap_before_fill"]))

    def test_expired_signals_rejected(self):
        # Two bars per replay step: the bar-3 signal is first seen at bar 4's close, already past its expiry.
        run = self.simulate(self.dataset(), step_seconds=600)
        self.assertIn("signal_expired", run["orders"][0]["history"][0]["reason_codes"])
        self.assertTrue(all(f["bar_open_utc"] >= next(o for o in run["orders"] if o["order_id"] == f["order_id"])["not_before_utc"]
                            for f in run["fills"]))


class IntegrityTests(Base):
    def test_deterministic_and_duplicate_prevention(self):
        dataset = self.dataset()
        one, two = self.simulate(dataset), self.simulate(dataset)
        self.assertEqual((one["run_id"], one["results_sha256"]), (two["run_id"], two["results_sha256"]))
        store = SimulationStore(self.root)
        store.save(one)
        self.assertCode("sim_run_exists", store.save, two)
        doubled = deepcopy(one)
        doubled["fills"].append(deepcopy(doubled["fills"][0]))
        self.assertRaises(TradingError, validate_run, doubled)
        self.assertEqual(len({o["source"]["signal_id"] for o in one["orders"] if o["source"]["signal_id"]}),
                         len([o for o in one["orders"] if o["purpose"] == "entry"]))

    def test_no_future_data(self):
        full = self.simulate(self.dataset())
        for cut in (4, 5, 6):
            partial = self.simulate(self.dataset(BARS[:cut]))
            with self.subTest(cut=cut):
                self.assertEqual([self._strip(f) for f in partial["fills"]],
                                 [self._strip(f) for f in full["fills"] if f["bar_sequence"] <= cut])
                decided = [o for o in full["orders"] if o["source"]["decision_bar_sequence"] <= cut]
                self.assertEqual([(o["purpose"], o["source"]["decision_bar_sequence"]) for o in partial["orders"]],
                                 [(o["purpose"], o["source"]["decision_bar_sequence"]) for o in decided])

    @staticmethod
    def _strip(fill):
        return {k: v for k, v in fill.items() if k not in ("fill_id", "order_id")}

    def test_tampering_rejected(self):
        dataset = self.dataset()
        run = self.simulate(dataset)
        for change in (lambda r: r["fills"][0].update(fill_price="1"),
                       lambda r: r["cash_ledger"][2].update(amount="-0.5"),
                       lambda r: r["summary"].update(realized_pnl="999"),
                       lambda r: r["positions"][0].update(realized_pnl="5")):
            tampered = deepcopy(run)
            change(tampered)
            with self.subTest(change):
                self.assertRaises(TradingError, validate_run, tampered)
        store = SimulationStore(self.root)
        store.save(run)
        path = store.runs / f"{run['run_id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["summary"]["ending_cash"] = "20000"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertCode("sim_run_corrupt", store.load, run["run_id"])
        data_path = self.root / "runtime/trading/market/datasets" / f"{dataset['dataset_id']}.json"
        payload = json.loads(data_path.read_text(encoding="utf-8"))
        payload["bars"][3]["open"] = "1"
        data_path.write_text(json.dumps(payload), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            sim_main(["sim-run", dataset["dataset_id"]], root=self.root)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "dataset_corrupt")

    def test_policy_validation_and_limits(self):
        dataset = self.dataset()
        for change in (lambda p: p["exits"].update(opposite_ema_crossover={"fast": 5, "slow": 5}),
                       lambda p: p["costs"].update(slippage_bps="10000"), lambda p: p["costs"].update(slippage_bps="-1"),
                       lambda p: p.update(mode="live"), lambda p: p["sizing"].update(quantity=1.5),
                       lambda p: p["account"].update(initial_cash="0"), lambda p: p.update(broker="x")):
            policy = deepcopy(self.policy)
            change(policy)
            with self.subTest(change):
                self.assertRaises(TradingError, self.simulate, dataset, policy)
        small = deepcopy(self.policy)
        small["limits"]["max_bars"] = 3
        self.assertCode("sim_too_many_bars", self.simulate, dataset, small)

    def test_interrupted_write(self):
        run = self.simulate(self.dataset())
        store = SimulationStore(self.root)
        with patch("vicekrack.trading.simulation.store.os.link", side_effect=OSError("disk /private")):
            error = self.assertCode("sim_write_failed", store.save, run)
        self.assertNotIn("/private", str(error))
        self.assertEqual(store.list(), [])

    def test_isolation_from_paper_accounts_and_content(self):
        PaperAccount("sim-guard", root=self.root, clock=lambda: NOW).initialize()
        state = self.root / "runtime/trading/accounts/acct-sim-guard/state.json"
        before = state.read_bytes()
        tracked = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / "examples").rglob("*")) if p.is_file()}
        SimulationStore(self.root).save(self.simulate(self.dataset()))
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.root / "runtime/trading").iterdir()), ["accounts", "market", "simulation"])
        self.assertEqual(tracked, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked})
        for path in (ROOT / "vicekrack/trading/simulation").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("from ..state", "from ..risk", "from ..orders", "from ..journal", "from ..agents",
                              "import state", "PaperAccount", "build_intent",
                              "authorize(", "openai", "anthropic", "socket", "urllib", "requests", "http.client",
                              "subprocess", "threading", "while True", "os.environ"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


class CliTests(Base):
    def cli(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = sim_main(list(argv), root=self.root)
        return code, json.loads(output.getvalue())

    def test_commands(self):
        dataset = self.store.import_dataset(make_adapter("synthetic", config=self.market_config, fixture="synth1-5m-reclaim"),
                                            config=self.market_config, config_sha256="0" * 64)
        code, run = self.cli("sim-run", dataset["dataset_id"])
        self.assertEqual((code, run["saved"], run["simulated"], run["paper_account_access"]), (0, False, True, False))
        code, saved = self.cli("sim-run", dataset["dataset_id"], "--save")
        self.assertEqual(self.cli("sim-run", dataset["dataset_id"], "--save")[1]["error"]["code"], "sim_run_exists")
        self.assertEqual(self.cli("sim-list")[1]["runs"][0]["run_id"], saved["run_id"])
        code, ledger = self.cli("sim-inspect", saved["run_id"], "--section", "ledger")
        self.assertEqual(ledger["ledger"][0]["kind"], "initial_cash")
        self.assertTrue(self.cli("sim-kill-switch", "engage")[1]["simulation_kill_switch_engaged"])
        blocked = self.cli("sim-run", dataset["dataset_id"])[1]
        self.assertEqual(blocked["kill_switch"], {"engaged": True, "source": "switch_file"})
        self.assertFalse(self.cli("sim-kill-switch", "release")[1]["simulation_kill_switch_engaged"])
        self.assertEqual(self.cli("sim-inspect", "srun-bad")[1]["error"]["code"], "invalid_sim_run_id")
        self.assertEqual(self.cli("sim-run", dataset["dataset_id"], "--policy", "../x.json")[1]["error"]["code"],
                         "invalid_simulation_policy")


if __name__ == "__main__":
    unittest.main()
