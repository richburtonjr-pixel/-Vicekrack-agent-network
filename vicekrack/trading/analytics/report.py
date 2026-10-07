"""Read-only analytics for completed offline simulations (Step 30). SIMULATED, descriptive only.

Inputs are a saved Step 29 run (re-validated on load) and its dataset (re-validated, and
it must match the run's dataset ID, bars hash and source-file hash). Nothing is written
back to either.

Equity curve: a bounded Step 25 replay over the run's own replay window. For each closed
bar k (in order) the validated fills executed at bar k's open are applied (their bar start
and open price must match the bar), then
    equity_k = cash_k + shares_k x close_k
using only bars already closed at that simulated time. A starting point at the replay
start holds the initial cash. The reconstructed ending cash, equity and unrealized P&L
must equal the run's own summary, otherwise the report is refused.

Closed trades (open positions are never counted as trades):
    gross P&L  = exit notional - entry notional
    fees       = entry fee + exit fee
    net P&L    = gross - fees  (= Step 29 realized P&L, whose cost basis includes the entry fee)
    outcome    = win (net > 0) | loss (net < 0) | breakeven (net = 0)
    win rate % = wins / closed x 100
    average win / loss / net = mean net of wins / of losses / of all closed trades
    expectancy = sum(net) / closed            (average net per closed trade)
    profit factor = sum(net of wins) / |sum(net of losses)|
Account: net return = ending equity - initial cash; net return % = net return / initial x 100.
Never annualized. Drawdown at a point = running peak equity - equity (peak includes the
starting cash); drawdown % = drawdown / peak x 100. Maximum $ and maximum % are reported
separately with the first time each occurred. Exposure % = closed bars ending with a
position / closed bars x 100.

All arithmetic is exact Decimal (50 digits); ratios and percentages are rounded half-even
to 8 decimal places. A metric that is not defined is {"status": "unavailable", "reason": ...};
it is never zero or infinity by substitution.
"""

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from ..contracts import sha256, validate_schema
from ..errors import TradingError
from ..market.replay import drive
from ..money import fmt, parse
from ..simulation.engine import validate_run

NOTICE = ("SIMULATED, descriptive analytics of one offline simulation on historical data. Not annualized, not a "
          "prediction of future performance, not advice; no orders or accounts are affected.")
HUNDRED = Decimal(100)


def _time(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def available(value):
    return {"status": "available", "value": fmt(value), "reason": None}


def unavailable(reason):
    return {"status": "unavailable", "value": None, "reason": reason}


def ratio(numerator, denominator, scale=Decimal(1)):
    with localcontext() as context:
        context.prec = 50
        return numerator * scale / denominator


class EquityConsumer:
    """Rebuilds cash, shares and equity bar by bar from validated fills (closed bars only)."""
    name = "analytics_equity"

    def __init__(self, fills, initial_cash, start_utc):
        self.by_bar = {}
        for fill in fills:
            self.by_bar.setdefault(fill["bar_sequence"], []).append(fill)
        self.cash, self.shares, self.last = initial_cash, 0, 0
        self.points = [self._point(None, start_utc, None)]
        self.applied = 0

    def on_step(self, view):
        if view.visible_count <= self.last:
            return
        new = [bar for bar in view.bars() if bar["sequence"] > self.last]
        if not new or new[0]["sequence"] != self.last + 1:
            raise TradingError("analytics_window_too_small", "More new bars arrived in one step than the replay window holds.")
        for bar in new:
            if bar["available_at_utc"] > view.now:
                raise TradingError("future_bar_leak", "A future bar reached the analytics engine.")
            for fill in self.by_bar.get(bar["sequence"], []):
                if fill["bar_open_utc"] != bar["timestamp_utc"] or parse(fill["open_price"]) != parse(bar["open"]):
                    raise TradingError("analytics_inconsistent", "A fill does not match the dataset bar it claims.")
                self.cash += parse(fill["cash_change"], "signed_decimal")
                self.shares += fill["quantity"] if fill["side"] == "buy" else -fill["quantity"]
                if self.shares < 0:
                    raise TradingError("analytics_inconsistent", "Fills imply a short position.")
                self.applied += 1
            self.points.append(self._point(bar["sequence"], bar["available_at_utc"], parse(bar["close"])))
            self.last = bar["sequence"]

    def final(self):
        return {}

    def _point(self, sequence, at, close):
        value = (close * self.shares) if close is not None else Decimal(0)
        return {"sequence": sequence, "at_utc": at, "close": fmt(close) if close is not None else None,
                "cash": self.cash, "position_quantity": self.shares, "market_value": value, "equity": self.cash + value}


def build_report(run, dataset, *, market_config, analytics_config, analytics_config_sha256, created_at):
    """Derive a validated simulation_analytics_report from a validated run and its dataset."""
    validate_run(run)                     # the run is re-validated here even if the caller already did
    source = run["dataset"]
    if (dataset["dataset_id"], dataset["bars_sha256"], dataset["source"]["file_sha256"]) != (
            source["dataset_id"], source["bars_sha256"], source["source_file_sha256"]):
        raise TradingError("analytics_dataset_mismatch", "The dataset does not match the simulation run's provenance.")
    limits = analytics_config["limits"]
    if run["summary"]["bars_processed"] + 1 > limits["max_curve_points"]:
        raise TradingError("analytics_too_many_points", "The run has more bars than max_curve_points allows.")
    initial = parse(run["account"]["initial_cash"], "money")
    replay = run["replay"]
    equity = EquityConsumer(run["fills"], initial, replay["start_utc"])
    drive(dataset, [equity], config=market_config, start=replay["start_utc"], end=replay["end_utc"],
          step_seconds=replay["step_seconds"], passthrough={"analytics_window_too_small", "analytics_inconsistent"})
    summary = run["summary"]
    bars = len(equity.points) - 1
    final = equity.points[-1]
    if (bars != summary["bars_processed"] or equity.applied != len(run["fills"])
            or fmt(equity.cash) != summary["ending_cash"] or fmt(final["equity"]) != summary["ending_equity"]):
        raise TradingError("analytics_inconsistent", "The reconstructed equity does not match the simulation run.")

    fills = {f["fill_id"]: f for f in run["fills"]}
    orders = {o["order_id"]: o for o in run["orders"]}
    strategy_of = {f["fill_id"]: orders[f["order_id"]]["source"]["strategy"] for f in run["fills"]}

    trades, open_positions = [], []
    for number, position in enumerate(run["positions"], start=1):
        entry = fills[position["entry_fill_id"]]
        if position["status"] == "closed":
            exit_fill = fills[position["exit_fill_id"]]
            gross = parse(exit_fill["notional"]) - parse(entry["notional"])
            fees = parse(entry["fee"]) + parse(exit_fill["fee"])
            net = gross - fees
            if fmt(net) != position["realized_pnl"]:
                raise TradingError("analytics_inconsistent", "Trade P&L does not match the run's realized P&L.")
            trades.append({
                "position": number, "strategy": strategy_of[entry["fill_id"]], "entry_fill_id": entry["fill_id"],
                "exit_fill_id": exit_fill["fill_id"], "quantity": position["quantity"],
                "opened_at_utc": position["opened_at_utc"], "closed_at_utc": position["closed_at_utc"],
                "entry_price": entry["fill_price"], "exit_price": exit_fill["fill_price"], "gross_pnl": fmt(gross),
                "fees": fmt(fees), "net_pnl": fmt(net), "outcome": "win" if net > 0 else "loss" if net < 0 else "breakeven",
                "bars_held": position["bars_held"],
                "seconds_held": int((_time(position["closed_at_utc"]) - _time(position["opened_at_utc"])).total_seconds())})
        else:
            open_positions.append({"strategy": strategy_of[entry["fill_id"]], "entry_fill_id": entry["fill_id"],
                                   "quantity": position["quantity"], "opened_at_utc": position["opened_at_utc"],
                                   "cost_basis": position["cost_basis"], "mark_price": position["mark_price"],
                                   "market_value": position["market_value"], "unrealized_pnl": position["unrealized_pnl"],
                                   "bars_held": position["bars_held"]})
    if len(trades) > limits["max_trades"]:
        raise TradingError("analytics_too_many_trades", "The run has more closed trades than max_trades allows.")
    unrealized = sum((parse(p["unrealized_pnl"], "signed_decimal") for p in open_positions), Decimal(0))
    if fmt(unrealized) != summary["unrealized_pnl"]:
        raise TradingError("analytics_inconsistent", "Open-position marks do not match the run.")

    closed = _closed_statistics(trades)
    curve, drawdown = _curve(equity.points)
    ending_equity = final["equity"]
    net_return = ending_equity - initial
    holding = _holding(trades, open_positions)
    with_position = sum(1 for p in equity.points[1:] if p["position_quantity"] > 0)
    exposure = {"bars_total": bars, "bars_with_position": with_position,
                "exposure_percent": available(ratio(Decimal(with_position), Decimal(bars), HUNDRED)) if bars
                else unavailable("no_bars_processed"),
                "max_position_market_value": fmt(max((p["market_value"] for p in equity.points), default=Decimal(0)))}
    report_body = {
        "source": {"run_id": run["run_id"], "run_results_sha256": run["results_sha256"], "policy_sha256": run["policy_sha256"],
                   "simulation_account_id": run["account"]["simulation_account_id"], "dataset_id": source["dataset_id"],
                   "dataset_bars_sha256": source["bars_sha256"], "symbol": source["symbol"], "interval": source["interval"],
                   "data_label": source["data_label"], "analytics_config_sha256": analytics_config_sha256},
        "period": {"replay_start_utc": replay["start_utc"], "replay_end_utc": replay["end_utc"],
                   "first_bar_close_utc": equity.points[1]["at_utc"] if bars else None,
                   "last_bar_close_utc": final["at_utc"] if bars else None, "bars": bars},
        "account": {"initial_equity": fmt(initial), "ending_cash": fmt(equity.cash), "ending_equity": fmt(ending_equity),
                    "realized_pnl": summary["realized_pnl"], "unrealized_pnl": summary["unrealized_pnl"],
                    "fees_total": summary["fees_total"], "net_return": fmt(net_return),
                    "net_return_percent": available(ratio(net_return, initial, HUNDRED))},
        "closed_trades": {**closed, "trades": trades},
        "open_positions": open_positions, "equity_curve": curve, "drawdown": drawdown, "holding": holding,
        "exposure": exposure, "orders": _orders(run), "attribution": _attribution(run, trades, open_positions),
    }
    report = {"contract": "simulation_analytics_report", "version": "1.0",
              "report_id": "sarp-" + sha256({"run_id": run["run_id"], "run": run["results_sha256"], "policy": run["policy_sha256"],
                                             "bars": source["bars_sha256"], "config": analytics_config_sha256})[:24],
              "simulated": True, "read_only": True, "annualized": False, "predictive": False, **report_body,
              "results_sha256": sha256(report_body), "created_at": created_at, "notice": NOTICE}
    validate_report(report)
    return report


def _closed_statistics(trades):
    nets = [parse(t["net_pnl"], "signed_decimal") for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    count = len(nets)
    gross_profit, gross_loss = sum(wins, Decimal(0)), sum(losses, Decimal(0))
    total = sum(nets, Decimal(0))
    return {
        "count": count, "wins": len(wins), "losses": len(losses), "breakeven": count - len(wins) - len(losses),
        "gross_profit": fmt(gross_profit), "gross_loss": fmt(gross_loss), "net_pnl": fmt(total),
        "fees": fmt(sum((parse(t["fees"]) for t in trades), Decimal(0))),
        "win_rate_percent": available(ratio(Decimal(len(wins)), Decimal(count), HUNDRED)) if count else unavailable("no_closed_trades"),
        "average_net": available(ratio(total, Decimal(count))) if count else unavailable("no_closed_trades"),
        "average_win": available(ratio(gross_profit, Decimal(len(wins)))) if wins
        else unavailable("no_closed_trades" if not count else "no_winning_trades"),
        "average_loss": available(ratio(gross_loss, Decimal(len(losses)))) if losses
        else unavailable("no_closed_trades" if not count else "no_losing_trades"),
        "expectancy": available(ratio(total, Decimal(count))) if count else unavailable("no_closed_trades"),
        "profit_factor": available(ratio(gross_profit, -gross_loss)) if losses
        else unavailable("no_closed_trades" if not count else "no_losing_trades"),
    }


def _curve(points):
    peak = points[0]["equity"]
    curve, max_dollars, max_percent = [], (Decimal(0), points[0]["at_utc"]), (Decimal(0), points[0]["at_utc"])
    for point in points:
        peak = max(peak, point["equity"])
        drop = peak - point["equity"]
        percent = ratio(drop, peak, HUNDRED) if peak > 0 else Decimal(0)
        if drop > max_dollars[0]:
            max_dollars = (drop, point["at_utc"])
        if percent > max_percent[0]:
            max_percent = (percent, point["at_utc"])
        curve.append({"sequence": point["sequence"], "at_utc": point["at_utc"], "close": point["close"],
                      "cash": fmt(point["cash"]), "position_quantity": point["position_quantity"],
                      "market_value": fmt(point["market_value"]), "equity": fmt(point["equity"]), "peak_equity": fmt(peak),
                      "drawdown": fmt(drop), "drawdown_percent": fmt(percent)})
    return curve, {"max_dollars": fmt(max_dollars[0]), "max_dollars_at_utc": max_dollars[1],
                   "max_percent": fmt(max_percent[0]), "max_percent_at_utc": max_percent[1], "peak_equity": fmt(peak)}


def _holding(trades, open_positions):
    if not trades:
        missing = unavailable("no_closed_trades")
        return {"closed_average_bars": missing, "closed_min_bars": missing, "closed_max_bars": missing,
                "closed_average_seconds": missing, "open_bars": [p["bars_held"] for p in open_positions]}
    bars = [Decimal(t["bars_held"]) for t in trades]
    seconds = [Decimal(t["seconds_held"]) for t in trades]
    return {"closed_average_bars": available(ratio(sum(bars), Decimal(len(bars)))),
            "closed_min_bars": available(min(bars)), "closed_max_bars": available(max(bars)),
            "closed_average_seconds": available(ratio(sum(seconds), Decimal(len(seconds)))),
            "open_bars": [p["bars_held"] for p in open_positions]}


def _orders(run):
    rejected = Counter()
    for order in run["orders"]:
        if order["status"] == "rejected":
            rejected.update(order["history"][-1]["reason_codes"])
    return {"created": len(run["orders"]), "filled": sum(o["status"] == "filled" for o in run["orders"]),
            "rejected": sum(o["status"] == "rejected" for o in run["orders"]),
            "pending_at_end": sum(o["status"] == "pending_at_end_of_data" for o in run["orders"]),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "pending_orders": [{"order_id": o["order_id"], "purpose": o["purpose"],
                                "decision_bar_sequence": o["source"]["decision_bar_sequence"],
                                "source": o["source"]["strategy"] or o["source"]["rule"]}
                               for o in run["orders"] if o["status"] == "pending_at_end_of_data"]}


def _attribution(run, trades, open_positions):
    strategies = run["policy"]["entry"]["strategies"]
    rows = []
    for name in strategies:
        mine = [t for t in trades if t["strategy"] == name]
        nets = [parse(t["net_pnl"], "signed_decimal") for t in mine]
        entries = [o for o in run["orders"] if o["purpose"] == "entry" and o["source"]["strategy"] == name]
        accepted = [o for o in entries if o["history"][0]["status"] == "accepted"]
        rejected = [o for o in entries if o["status"] == "rejected"]
        blocked = [o for o in rejected if {"position_already_open", "pending_order_exists"} & set(o["history"][-1]["reason_codes"])]
        wins = sum(1 for n in nets if n > 0)
        losses = sum(1 for n in nets if n < 0)
        opens = [p for p in open_positions if p["strategy"] == name]
        rows.append({"strategy": name, "closed_trades": len(mine), "wins": wins, "losses": losses,
                     "breakeven": len(mine) - wins - losses, "net_pnl": fmt(sum(nets, Decimal(0))),
                     "fees": fmt(sum((parse(t["fees"]) for t in mine), Decimal(0))),
                     "win_rate_percent": available(ratio(Decimal(wins), Decimal(len(mine)), HUNDRED)) if mine
                     else unavailable("no_closed_trades"),
                     "open_positions": len(opens),
                     "unrealized_pnl": fmt(sum((parse(p["unrealized_pnl"], "signed_decimal") for p in opens), Decimal(0))),
                     "signals_accepted": len(accepted), "signals_rejected": len(rejected),
                     "blocked_by_shared_account": len(blocked)})
    if len(strategies) > 1:
        explanation = (f"This run used {len(strategies)} strategies ({', '.join(strategies)}) in ONE simulation account with "
                       "one cash balance and at most one position at a time. A signal from one strategy could be rejected "
                       "because another strategy's position or pending order already existed, and cash used by one affected "
                       "the others. Per-strategy figures describe each strategy's share of this single shared run; they are "
                       "not independent strategy tests and should not be compared as such.")
    else:
        explanation = ("One strategy in this run, so its attribution equals the whole run. 'blocked_by_shared_account' counts "
                       "signals rejected because this strategy's own position or pending order already existed. Results "
                       "describe one simulation on one dataset.")
    return {"shared_account": len(strategies) > 1, "explanation": explanation, "strategies": rows}


def validate_report(report):
    validate_schema("simulation_analytics_report", report)
    body = {k: report[k] for k in ("source", "period", "account", "closed_trades", "open_positions", "equity_curve",
                                   "drawdown", "holding", "exposure", "orders", "attribution")}
    if sha256(body) != report["results_sha256"]:
        raise TradingError("invalid_analytics_report", "Analytics report hash mismatch.")
    curve = report["equity_curve"]
    if curve[-1]["equity"] != report["account"]["ending_equity"] or len(curve) - 1 != report["period"]["bars"]:
        raise TradingError("invalid_analytics_report", "Equity curve does not match the account summary.")
    trades = report["closed_trades"]
    if (trades["count"] != len(trades["trades"]) or trades["wins"] + trades["losses"] + trades["breakeven"] != trades["count"]):
        raise TradingError("invalid_analytics_report", "Closed-trade counts are inconsistent.")
