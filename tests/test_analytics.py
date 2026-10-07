"""Step 30: read-only analytics of offline simulations. Synthetic data only; no network.

Expected trade P&L, fees, equity, drawdown and ratios are computed here independently, with
exact Fractions and explicit half-even rounding, from the bars and the documented Step 29
cost model. They are never computed with the analytics module's own helpers.
"""

import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction

from test_simulation import BARS, NOW, Base, cents, r8, text
from vicekrack.trading.analytics.cli import main as analytics_main
from vicekrack.trading.analytics.report import EquityConsumer, build_report, validate_report
from vicekrack.trading.analytics.store import AnalyticsStore, load_analytics_config
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.market.store import make_adapter
from vicekrack.trading.simulation.cli import main as sim_main
from vicekrack.trading.simulation.store import SimulationStore
from vicekrack.trading.state import PaperAccount

QUANTITY, SLIP, INITIAL = 10, Fraction(10, 10000), Fraction(10000)
# Win then loss (breakouts on bars 3 and 6, two-bar holding exits).
WIN_LOSS = BARS + [("11.0", "11.25", "10.8", "10.9"), ("10.0", "10.2", "9.8", "10.1"), ("10.1", "10.2", "10.0", "10.1")]
# One breakout on bar 3; the exit opens at the same price as the entry (breakeven at zero cost).
EVEN = BARS[:3] + [("10.6", "10.8", "10.4", "10.7"), ("10.7", "10.9", "10.4", "10.5"), ("10.6", "10.7", "10.5", "10.6")]
FLAT = [("10", "10", "10", "10")] * 6


def fill(bars, sequence, side, costs=True):
    """Independent Step 29 fill model: next-bar open, slippage, fee = 1 + 5 bps (to cents)."""
    open_price = Fraction(bars[sequence - 1][0])
    if not costs:
        return open_price * QUANTITY, Fraction(0)
    price = r8(open_price * (1 + SLIP if side == "buy" else 1 - SLIP))
    notional = price * QUANTITY
    return notional, cents(1 + notional * Fraction(5, 10000))


def model(bars, plan, costs=True):
    """Equity per closed bar plus closed trades, from bars and (sequence, side) fills."""
    cash, shares, points, trades, entry = INITIAL, 0, [(INITIAL, 0, INITIAL)], [], None
    for sequence, bar in enumerate(bars, start=1):
        for when, side in plan:
            if when != sequence:
                continue
            notional, fee = fill(bars, sequence, side, costs)
            if side == "buy":
                cash, shares, entry = cash - notional - fee, shares + QUANTITY, (notional, fee, sequence)
            else:
                cash, shares = cash + notional - fee, shares - QUANTITY
                gross, fees = notional - entry[0], entry[1] + fee
                trades.append({"gross": gross, "fees": fees, "net": gross - fees, "bars": sequence - entry[2]})
                entry = None
        points.append((cash, shares, cash + shares * Fraction(bar[3])))
    return points, trades


def metric(report_metric):
    return None if report_metric["status"] == "unavailable" else Fraction(report_metric["value"])


class AnalyticsBase(Base):
    def setUp(self):
        super().setUp()
        self.config, self.config_sha = load_analytics_config()

    def report(self, bars, policy=None, config=None, created_at=NOW):
        run = self.simulate(self.dataset(bars), policy)
        return run, build_report(run, self.dataset(bars), market_config=self.market_config,
                                 analytics_config=config or self.config, analytics_config_sha256=self.config_sha,
                                 created_at=created_at)

    def zero_cost(self):
        policy = deepcopy(self.policy)
        policy["costs"] = {"slippage_bps": "0", "fee_per_order": "0", "fee_bps": "0"}
        return policy

    def assertPlan(self, run, plan):
        self.assertEqual([(f["bar_sequence"], f["side"]) for f in run["fills"]], plan)

    def assertCurve(self, report, points):
        curve = report["equity_curve"]
        self.assertEqual(len(curve), len(points))
        peak = Fraction(0)
        for point, (cash, shares, equity) in zip(curve, points):
            peak = max(peak, equity)
            self.assertEqual((point["cash"], point["position_quantity"], point["equity"], point["peak_equity"],
                              point["drawdown"], point["drawdown_percent"]),
                             (text(cash), shares, text(equity), text(peak), text(peak - equity),
                              text(r8((peak - equity) / peak * 100))))


class TradeStatisticsTests(AnalyticsBase):
    def test_profitable_trade_with_open_position_kept_separate(self):
        run, report = self.report(BARS)
        plan = [(4, "buy"), (6, "sell"), (7, "buy")]
        self.assertPlan(run, plan)
        points, trades = model(BARS, plan)
        net = trades[0]["net"]
        self.assertGreater(net, 0)
        closed = report["closed_trades"]
        self.assertEqual((closed["count"], closed["wins"], closed["losses"], closed["breakeven"]), (1, 1, 0, 0))
        self.assertEqual((closed["net_pnl"], closed["gross_profit"], closed["gross_loss"], closed["fees"]),
                         (text(net), text(net), "0", text(trades[0]["fees"])))
        self.assertEqual(closed["trades"][0]["gross_pnl"], text(trades[0]["gross"]))
        self.assertEqual(closed["trades"][0]["outcome"], "win")
        self.assertEqual(metric(closed["win_rate_percent"]), 100)
        self.assertEqual(metric(closed["average_win"]), net)
        self.assertEqual(metric(closed["expectancy"]), net)
        for name in ("average_loss", "profit_factor"):
            self.assertEqual(closed[name], {"status": "unavailable", "value": None, "reason": "no_losing_trades"})
        # The open position is not a trade: it has its own section and unrealized P&L.
        notional, fee = fill(BARS, 7, "buy")
        unrealized = QUANTITY * Fraction("11.2") - (notional + fee)
        self.assertEqual(len(report["open_positions"]), 1)
        self.assertEqual(report["open_positions"][0]["unrealized_pnl"], text(unrealized))
        account = report["account"]
        all_fees = trades[0]["fees"] + fee
        self.assertEqual((account["realized_pnl"], account["unrealized_pnl"], account["fees_total"]),
                         (text(net), text(unrealized), text(all_fees)))
        self.assertEqual(account["ending_equity"], text(points[-1][2]))
        self.assertEqual(account["net_return"], text(points[-1][2] - INITIAL))
        self.assertEqual(metric(account["net_return_percent"]), r8((points[-1][2] - INITIAL) / INITIAL * 100))
        self.assertEqual(account["net_return"], text(net + unrealized))      # costs counted once, consistently

    def test_losing_and_winning_trades_profit_factor(self):
        run, report = self.report(WIN_LOSS)
        plan = [(4, "buy"), (6, "sell"), (7, "buy"), (9, "sell")]
        self.assertPlan(run, plan)
        _, trades = model(WIN_LOSS, plan)
        win, loss = trades[0]["net"], trades[1]["net"]
        self.assertTrue(win > 0 > loss)
        closed = report["closed_trades"]
        self.assertEqual([t["outcome"] for t in closed["trades"]], ["win", "loss"])
        self.assertEqual(metric(closed["win_rate_percent"]), 50)
        self.assertEqual(metric(closed["average_win"]), win)
        self.assertEqual(metric(closed["average_loss"]), loss)
        self.assertEqual(metric(closed["average_net"]), r8((win + loss) / 2))
        self.assertEqual(metric(closed["expectancy"]), r8((win + loss) / 2))
        self.assertEqual(metric(closed["profit_factor"]), r8(win / -loss))
        self.assertEqual(report["open_positions"], [])
        self.assertEqual(report["account"]["unrealized_pnl"], "0")

    def test_only_losses_gives_real_zero_profit_factor(self):
        bars = BARS[:5] + [("10.0", "11.2", "9.9", "11.1"), ("11.2", "11.3", "11.1", "11.2")]
        _, report = self.report(bars)
        closed = report["closed_trades"]
        self.assertEqual((closed["wins"], closed["losses"]), (0, 1))
        self.assertEqual(closed["profit_factor"], {"status": "available", "value": "0", "reason": None})
        self.assertEqual(closed["win_rate_percent"]["value"], "0")
        self.assertEqual(closed["average_win"]["reason"], "no_winning_trades")

    def test_breakeven_trade(self):
        run, report = self.report(EVEN, self.zero_cost())
        self.assertPlan(run, [(4, "buy"), (6, "sell")])
        closed = report["closed_trades"]
        self.assertEqual((closed["count"], closed["breakeven"], closed["trades"][0]["outcome"]), (1, 1, "breakeven"))
        self.assertEqual(closed["expectancy"], {"status": "available", "value": "0", "reason": None})
        self.assertEqual(closed["win_rate_percent"]["value"], "0")
        self.assertEqual(closed["average_win"]["reason"], "no_winning_trades")
        self.assertEqual(closed["average_loss"]["reason"], "no_losing_trades")
        self.assertEqual(closed["profit_factor"]["reason"], "no_losing_trades")

    def test_no_trades_metrics_unavailable_never_zero(self):
        run, report = self.report(FLAT)
        self.assertEqual(run["fills"], [])
        closed = report["closed_trades"]
        for name in ("win_rate_percent", "average_net", "average_win", "average_loss", "expectancy", "profit_factor"):
            self.assertEqual(closed[name], {"status": "unavailable", "value": None, "reason": "no_closed_trades"}, name)
        for name in ("closed_average_bars", "closed_min_bars", "closed_max_bars", "closed_average_seconds"):
            self.assertEqual(report["holding"][name]["reason"], "no_closed_trades")
        self.assertEqual(report["account"]["net_return_percent"]["value"], "0")   # a real, measured zero
        self.assertEqual(report["drawdown"]["max_dollars"], "0")
        self.assertEqual(report["exposure"]["exposure_percent"]["value"], "0")
        self.assertNotIn("Infinity", json.dumps(report))
        self.assertNotIn("NaN", json.dumps(report))

    def test_fees_and_holding(self):
        _, report = self.report(WIN_LOSS)
        _, trades = model(WIN_LOSS, [(4, "buy"), (6, "sell"), (7, "buy"), (9, "sell")])
        self.assertEqual(report["closed_trades"]["fees"], text(sum(t["fees"] for t in trades)))
        self.assertEqual(report["account"]["fees_total"], report["closed_trades"]["fees"])
        for row, expected in zip(report["closed_trades"]["trades"], trades):
            self.assertEqual(Fraction(row["gross_pnl"]) - Fraction(row["fees"]), Fraction(row["net_pnl"]))
            self.assertEqual(row["seconds_held"], expected["bars"] * 300)       # entry open to exit open
        self.assertEqual(metric(report["holding"]["closed_average_bars"]), 2)


class EquityTests(AnalyticsBase):
    def test_equity_curve_and_drawdown_by_hand(self):
        for bars, plan in ((BARS, [(4, "buy"), (6, "sell"), (7, "buy")]),
                           (WIN_LOSS, [(4, "buy"), (6, "sell"), (7, "buy"), (9, "sell")])):
            with self.subTest(bars=len(bars)):
                run, report = self.report(bars)
                points, _ = model(bars, plan)
                self.assertCurve(report, points)
                self.assertEqual(report["equity_curve"][0]["sequence"], None)
                self.assertEqual(report["equity_curve"][0]["at_utc"], run["replay"]["start_utc"])
                peak, worst, worst_pct = Fraction(0), Fraction(0), Fraction(0)
                for _, _, equity in points:
                    peak = max(peak, equity)
                    worst, worst_pct = max(worst, peak - equity), max(worst_pct, r8((peak - equity) / peak * 100))
                self.assertEqual((report["drawdown"]["max_dollars"], report["drawdown"]["max_percent"]),
                                 (text(worst), text(worst_pct)))
                held = sum(1 for _, shares, _ in points[1:] if shares)
                self.assertEqual(report["exposure"]["bars_with_position"], held)
                self.assertEqual(metric(report["exposure"]["exposure_percent"]), r8(Fraction(held * 100, len(bars))))

    def test_no_future_data(self):
        _, full = self.report(WIN_LOSS)
        for cut in (4, 6, 8):
            with self.subTest(cut=cut):
                _, partial = self.report(WIN_LOSS[:cut])
                self.assertEqual(partial["equity_curve"], full["equity_curve"][:cut + 1])

    def test_consumer_rejects_future_bars(self):
        class View:
            now, visible_count = "2026-01-15T14:35:00Z", 1

            def bars(self):
                return [{"sequence": 1, "available_at_utc": "2026-01-15T14:40:00Z", "timestamp_utc": "x",
                         "open": "1", "close": "1"}]
        consumer = EquityConsumer([], Decimal(1), "2026-01-15T14:30:00Z")
        self.assertCode("future_bar_leak", consumer.on_step, View())

    def test_deterministic(self):
        _, first = self.report(WIN_LOSS)
        _, second = self.report(WIN_LOSS, created_at="2026-10-06T00:00:00Z")
        self.assertEqual({k: v for k, v in first.items() if k != "created_at"},
                         {k: v for k, v in second.items() if k != "created_at"})
        self.assertRegex(first["report_id"], r"^sarp-[0-9a-f]{24}$")
        self.assertEqual((first["annualized"], first["predictive"], first["read_only"]), (False, False, True))


class AttributionTests(AnalyticsBase):
    def test_single_strategy(self):
        _, report = self.report(BARS)
        attribution = report["attribution"]
        self.assertFalse(attribution["shared_account"])
        row = attribution["strategies"][0]
        self.assertEqual((row["strategy"], row["closed_trades"], row["open_positions"], row["signals_accepted"]),
                         ("b1", 1, 1, 2))
        self.assertEqual(row["net_pnl"], report["closed_trades"]["net_pnl"])

    def test_mixed_strategies_explain_shared_account(self):
        policy = deepcopy(self.policy)
        policy["entry"] = {"strategies": ["b1", "b1x3"], "reject_across_gaps": True}
        _, report = self.report(BARS, policy)
        attribution = report["attribution"]
        self.assertTrue(attribution["shared_account"])
        self.assertIn("not independent strategy tests", attribution["explanation"])
        rows = {row["strategy"]: row for row in attribution["strategies"]}
        self.assertEqual((rows["b1"]["closed_trades"], rows["b1"]["blocked_by_shared_account"]), (1, 0))
        self.assertEqual((rows["b1x3"]["closed_trades"], rows["b1x3"]["signals_rejected"],
                          rows["b1x3"]["blocked_by_shared_account"]), (0, 2, 2))
        self.assertEqual(rows["b1x3"]["win_rate_percent"]["reason"], "no_closed_trades")
        self.assertEqual(report["orders"]["rejected_by_reason"], {"pending_order_exists": 2})
        total = sum(Fraction(row["net_pnl"]) for row in rows.values())
        self.assertEqual(total, Fraction(report["closed_trades"]["net_pnl"]))


class SafetyTests(AnalyticsBase):
    def test_input_validation_and_bounds(self):
        run = self.simulate(self.dataset(BARS))
        arguments = dict(market_config=self.market_config, analytics_config=self.config,
                         analytics_config_sha256=self.config_sha, created_at=NOW)
        self.assertCode("analytics_dataset_mismatch", build_report, run, self.dataset(WIN_LOSS), **arguments)
        tampered = deepcopy(run)
        tampered["fills"][0]["open_price"] = "10.5"
        self.assertRaises(TradingError, build_report, tampered, self.dataset(BARS), **arguments)
        small = deepcopy(self.config)
        small["limits"]["max_curve_points"] = 3
        self.assertCode("analytics_too_many_points", build_report, run, self.dataset(BARS), **{**arguments, "analytics_config": small})
        small = deepcopy(self.config)
        small["limits"]["max_trades"] = 0
        self.assertCode("analytics_too_many_trades", build_report, run, self.dataset(BARS), **{**arguments, "analytics_config": small})
        report = build_report(run, self.dataset(BARS), **arguments)
        for change in (lambda r: r["account"].update(ending_equity="1"), lambda r: r["closed_trades"].update(wins=5),
                       lambda r: r.update(annualized=True), lambda r: r["equity_curve"].pop()):
            bad = deepcopy(report)
            change(bad)
            with self.subTest(change):
                self.assertRaises(TradingError, validate_report, bad)

    def test_read_only_isolation(self):
        PaperAccount("analytics-guard", root=self.root, clock=lambda: NOW).initialize()
        state = self.root / "runtime/trading/accounts/acct-analytics-guard/state.json"
        before = state.read_bytes()
        run = self.simulate(self.dataset(BARS))
        SimulationStore(self.root).save(run)
        run_path = SimulationStore(self.root).runs / f"{run['run_id']}.json"
        run_bytes = run_path.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(analytics_main(["analytics-generate", run["run_id"], "--save"], root=self.root), 0)
        self.assertEqual((state.read_bytes(), run_path.read_bytes()), (before, run_bytes))
        self.assertEqual(sorted(p.name for p in (self.root / "runtime/trading").iterdir()),
                         ["accounts", "analytics", "market", "simulation"])
        for path in (ROOT / "vicekrack/trading/analytics").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("from ..state", "from ..risk", "from ..orders", "from ..journal", "from ..agents",
                              "from ..signals", "from ..indicators", "PaperAccount", "run_simulation", ".set_kill_switch",
                              "openai", "anthropic", "socket", "urllib", "requests", "http.client", "subprocess",
                              "threading", "while True", "os.environ"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


class CliTests(AnalyticsBase):
    def cli(self, main, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(list(argv), root=self.root)
        return code, json.loads(output.getvalue())

    def test_commands_and_tampering(self):
        dataset = self.store.import_dataset(make_adapter("synthetic", config=self.market_config, fixture="synth1-5m-reclaim"),
                                            config=self.market_config, config_sha256="0" * 64)
        run_id = self.cli(sim_main, "sim-run", dataset["dataset_id"], "--save")[1]["run_id"]
        code, preview = self.cli(analytics_main, "analytics-generate", run_id)
        self.assertEqual((code, preview["saved"], preview["simulated"], preview["annualized"]), (0, False, True, False))
        self.assertNotIn("trades", preview["closed_trades"])
        code, saved = self.cli(analytics_main, "analytics-generate", run_id, "--save")
        self.assertEqual(saved["report_id"], preview["report_id"])
        self.assertEqual(self.cli(analytics_main, "analytics-generate", run_id, "--save")[1]["error"]["code"], "report_exists")
        listing = self.cli(analytics_main, "analytics-list")[1]
        self.assertEqual((listing["total"], listing["reports"][0]["report_id"]), (1, saved["report_id"]))
        code, curve = self.cli(analytics_main, "analytics-inspect", saved["report_id"], "--section", "equity_curve")
        self.assertEqual(curve["equity_curve_points"], len(curve["equity_curve"]))
        self.assertLessEqual(len(curve["equity_curve"]), self.config["limits"]["max_cli_items"])
        self.assertEqual(self.cli(analytics_main, "analytics-inspect", "sarp-x")[1]["error"]["code"], "invalid_report_id")
        # Tampered report, run and dataset are all rejected.
        path = AnalyticsStore(self.root).reports / f"{saved['report_id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["account"]["net_return"] = "1000"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.cli(analytics_main, "analytics-inspect", saved["report_id"])[1]["error"]["code"], "report_corrupt")
        self.assertFalse(self.cli(analytics_main, "analytics-list")[1]["reports"][0]["readable"])
        run_path = SimulationStore(self.root).runs / f"{run_id}.json"
        original = run_path.read_text(encoding="utf-8")
        run_path.write_text(original.replace('"ending_cash": "', '"ending_cash": "1', 1), encoding="utf-8")
        self.assertEqual(self.cli(analytics_main, "analytics-generate", run_id)[1]["error"]["code"], "sim_run_corrupt")
        run_path.write_text(original, encoding="utf-8")
        data_path = self.root / "runtime/trading/market/datasets" / f"{dataset['dataset_id']}.json"
        payload = json.loads(data_path.read_text(encoding="utf-8"))
        payload["bars"][3]["open"] = "1"
        data_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.cli(analytics_main, "analytics-generate", run_id)[1]["error"]["code"], "dataset_corrupt")
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), hashlib.sha256(run_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
