"""Demo results for the Living HQ trading results desk (Step 34). SYNTHETIC, never stored, never executed.

A tiny hand-written run that matches the demo house's simulator events exactly (they are
correlated with the same rules as real runs): ten 5-minute bars of a demo symbol, one
ema-cross-3-5 entry decided on bar 7 and filled at bar 8's open, and a breakout-3 signal on
bar 9 rejected because the position is already open. The position is still open when the
data ends, so there are no closed trades and every closed-trade statistic is `unavailable`
(`no_closed_trades`), which the desk shows instead of a zero.

Figures follow the documented Step 29 cost model (buy price = open x (1 + 5 bps), rounded
half-even to 8 places; fee 1.00 per order) and Step 30 definitions, computed here with plain
Decimal arithmetic on constants. Nothing here runs the simulator or the analytics engine.
"""

from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from functools import lru_cache

from ..trading.money import fmt
from .demo import DATASET, FILL_1, ORDER_1, ORDER_2, RUN, SIGNAL_1, SIGNAL_2, START, _id, _stamp, demo_events

# (open, close) per bar; high/low are not used by the next-open fill model.
BARS = (("100.00", "100.20"), ("100.20", "100.50"), ("100.50", "100.10"), ("100.10", "100.60"), ("100.60", "101.00"),
        ("101.00", "101.30"), ("101.30", "101.80"), ("101.90", "101.40"), ("101.40", "101.10"), ("101.10", "101.60"))
INITIAL, QUANTITY, SLIPPAGE_BPS, FEE = Decimal("10000.00"), 10, Decimal(5), Decimal("1.00")
STRATEGIES = ("ema-cross-3-5", "breakout-3")
REPORT = _id("sarp", "report")


def _at(minutes):
    return _stamp(START + timedelta(minutes=minutes))


def _buy_price(price):
    with localcontext() as context:
        context.prec = 50
        return (Decimal(price) * (1 + SLIPPAGE_BPS / Decimal(10000))).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)


def _metric(value):
    return {"status": "available", "value": fmt(value), "reason": None}


def _missing(reason):
    return {"status": "unavailable", "value": None, "reason": reason}


@lru_cache(maxsize=None)
def _build():
    estimate_1 = _buy_price(BARS[6][1])
    estimate_2 = _buy_price(BARS[8][1])
    price = _buy_price(BARS[7][0])
    notional = price * QUANTITY
    cash_change = -(notional + FEE)
    cost_basis = notional + FEE
    cash = INITIAL + cash_change
    last_close = Decimal(BARS[-1][1])
    market_value = last_close * QUANTITY
    source = {"kind": "research_signal", "rule": None}
    orders = [
        {"order_id": ORDER_1, "purpose": "entry", "side": "buy", "quantity": QUANTITY, "order_type": "market",
         "source": {**source, "signal_id": SIGNAL_1, "strategy": STRATEGIES[0], "decision_bar_sequence": 7,
                    "decision_at_utc": _at(35)},
         "estimate": {"reference_price": fmt(estimate_1), "notional": fmt(estimate_1 * QUANTITY)},
         "expires_at_utc": _at(45), "status": "filled", "fill_id": FILL_1,
         "history": [{"at_utc": _at(35), "status": "accepted",
                      "reason_codes": ["signal_accepted_by_policy", "fills_at_next_available_open"]},
                     {"at_utc": _at(35), "status": "filled", "reason_codes": ["filled_at_bar_open"]}]},
        {"order_id": ORDER_2, "purpose": "entry", "side": "buy", "quantity": QUANTITY, "order_type": "market",
         "source": {**source, "signal_id": SIGNAL_2, "strategy": STRATEGIES[1], "decision_bar_sequence": 9,
                    "decision_at_utc": _at(45)},
         "estimate": {"reference_price": fmt(estimate_2), "notional": fmt(estimate_2 * QUANTITY)},
         "expires_at_utc": _at(55), "status": "rejected", "fill_id": None,
         "history": [{"at_utc": _at(45), "status": "rejected", "reason_codes": ["position_already_open"]}]},
    ]
    fills = [{"fill_id": FILL_1, "order_id": ORDER_1, "side": "buy", "symbol": "DEMO", "quantity": QUANTITY,
              "bar_sequence": 8, "bar_open_utc": _at(35), "open_price": fmt(Decimal(BARS[7][0])),
              "slippage_bps": str(SLIPPAGE_BPS), "fill_price": fmt(price), "notional": fmt(notional), "fee": fmt(FEE),
              "cash_change": fmt(cash_change), "gap_before_fill": 0, "reason_codes": ["filled_at_bar_open"]}]
    positions = [{"status": "open", "quantity": QUANTITY, "entry_fill_id": FILL_1, "exit_fill_id": None,
                  "opened_at_utc": _at(35), "closed_at_utc": None, "cost_basis": fmt(cost_basis), "proceeds": None,
                  "exit_fee": None, "realized_pnl": None, "mark_price": fmt(last_close), "marked_at_utc": _at(50),
                  "market_value": fmt(market_value), "unrealized_pnl": fmt(market_value - cost_basis), "bars_held": 3}]
    summary = {"bars_processed": 10, "signals_seen": 2, "orders_created": 2, "orders_rejected": 1, "orders_filled": 1,
               "orders_pending_at_end": 0, "fills": 1, "fees_total": fmt(FEE), "realized_pnl": "0",
               "unrealized_pnl": fmt(market_value - cost_basis), "ending_cash": fmt(cash),
               "ending_equity": fmt(cash + market_value), "open_position_quantity": QUANTITY,
               "last_close": fmt(last_close), "last_bar_close_utc": _at(50)}
    run = {"run_id": RUN, "results_sha256": "d" * 64, "policy_sha256": "e" * 64, "created_at": _stamp(START),
           "account": {"simulation_account_id": _id("simacct", "account"), "initial_cash": "10000.00", "currency": "USD"},
           "dataset": {"dataset_id": DATASET["id"], "symbol": "DEMO", "interval": "5m", "timezone": "UTC",
                       "data_label": "synthetic", "bars_sha256": "f" * 64},
           "strategies": [{"name": name, "config_sha256": "c" * 64} for name in STRATEGIES],
           "policy": {"entry": {"strategies": list(STRATEGIES), "reject_across_gaps": True},
                      "sizing": {"method": "fixed_quantity", "quantity": QUANTITY},
                      "exits": {"opposite_ema_crossover": None, "max_holding_bars": None},
                      "costs": {"slippage_bps": str(SLIPPAGE_BPS), "fee_per_order": "1.00", "fee_bps": "0"},
                      "limits": {"max_order_notional": "2000.00", "max_position_notional": "2000.00", "max_orders": 20,
                                 "max_bars": 5000}},
           "kill_switch": {"engaged": False, "source": None},
           "replay": {"start_utc": _at(0), "end_utc": _at(50), "step_seconds": 300, "steps": 10},
           "orders": orders, "fills": fills, "positions": positions, "summary": summary}
    return run, _report(run, cash, cost_basis, market_value)


def _report(run, cash_after, cost_basis, market_value):
    points, peak = [], INITIAL
    worst_dollars, worst_percent = (Decimal(0), _at(0)), (Decimal(0), _at(0))
    rows = [(None, _at(0), None, INITIAL, 0)]
    for sequence, (_, close) in enumerate(BARS, start=1):
        holding = sequence >= 8
        rows.append((sequence, _at(5 * sequence), Decimal(close), cash_after if holding else INITIAL,
                     QUANTITY if holding else 0))
    for sequence, at, close, cash, shares in rows:
        value = close * shares if close is not None else Decimal(0)
        equity = cash + value
        peak = max(peak, equity)
        drop = peak - equity
        with localcontext() as context:
            context.prec = 50
            percent = drop * 100 / peak
        if drop > worst_dollars[0]:
            worst_dollars = (drop, at)
        if percent > worst_percent[0]:
            worst_percent = (percent, at)
        points.append({"sequence": sequence, "at_utc": at, "close": fmt(close) if close is not None else None,
                       "cash": fmt(cash), "position_quantity": shares, "market_value": fmt(value), "equity": fmt(equity),
                       "peak_equity": fmt(peak), "drawdown": fmt(drop), "drawdown_percent": fmt(percent)})
    ending = cash_after + market_value
    missing = _missing("no_closed_trades")
    with localcontext() as context:
        context.prec = 50
        net_percent = (ending - INITIAL) * 100 / INITIAL
    return {
        "report_id": REPORT, "results_sha256": "a" * 64, "created_at": _stamp(START),
        "source": {"analytics_config_sha256": "b" * 64},
        "period": {"replay_start_utc": _at(0), "replay_end_utc": _at(50), "first_bar_close_utc": _at(5),
                   "last_bar_close_utc": _at(50), "bars": 10},
        "account": {"net_return": fmt(ending - INITIAL), "net_return_percent": _metric(net_percent)},
        "closed_trades": {"count": 0, "wins": 0, "losses": 0, "breakeven": 0, "gross_profit": "0", "gross_loss": "0",
                          "net_pnl": "0", "fees": "0", "win_rate_percent": missing, "average_net": missing,
                          "average_win": missing, "average_loss": missing, "expectancy": missing, "profit_factor": missing,
                          "trades": []},
        "open_positions": [{"strategy": STRATEGIES[0], "entry_fill_id": FILL_1, "quantity": QUANTITY,
                            "opened_at_utc": _at(35), "cost_basis": fmt(cost_basis), "mark_price": BARS[-1][1],
                            "market_value": fmt(market_value), "unrealized_pnl": fmt(market_value - cost_basis),
                            "bars_held": 3}],
        "equity_curve": points,
        "drawdown": {"max_dollars": fmt(worst_dollars[0]), "max_dollars_at_utc": worst_dollars[1],
                     "max_percent": fmt(worst_percent[0]), "max_percent_at_utc": worst_percent[1], "peak_equity": fmt(peak)},
        "holding": {"closed_average_bars": missing, "closed_min_bars": missing, "closed_max_bars": missing,
                    "closed_average_seconds": missing, "open_bars": [3]},
        "exposure": {"bars_total": 10, "bars_with_position": 3, "exposure_percent": _metric(Decimal(30)),
                     "max_position_market_value": fmt(max(Decimal(BARS[i][1]) * QUANTITY for i in range(7, 10)))},
        "orders": {"created": 2, "filled": 1, "rejected": 1, "pending_at_end": 0,
                   "rejected_by_reason": {"position_already_open": 1}, "pending_orders": []},
        "attribution": {
            "shared_account": True,
            "explanation": ("DEMO: two strategies (ema-cross-3-5, breakout-3) shared ONE simulation account with one cash "
                            "balance and at most one position, so breakout-3's signal was rejected because ema-cross-3-5's "
                            "position was open. Per-strategy figures are shares of one run, not independent tests."),
            "strategies": [
                {"strategy": STRATEGIES[0], "closed_trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "net_pnl": "0",
                 "fees": "0", "win_rate_percent": missing, "open_positions": 1,
                 "unrealized_pnl": fmt(market_value - cost_basis), "signals_accepted": 1, "signals_rejected": 0,
                 "blocked_by_shared_account": 0},
                {"strategy": STRATEGIES[1], "closed_trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "net_pnl": "0",
                 "fees": "0", "win_rate_percent": missing, "open_positions": 0, "unrealized_pnl": "0",
                 "signals_accepted": 0, "signals_rejected": 1, "blocked_by_shared_account": 1}]},
    }


def demo_inputs():
    """(view, run) shaped like a Step 31 view and a Step 29 run, for the demo house's events."""
    run, _ = _build()
    events = demo_events()
    view = {"timeline_id": "demo", "origin": "demo", "kind": "hq_demo", "completeness": "complete",
            "time_basis": "synthetic", "issues": [], "events": events}
    return view, run


def demo_report():
    return _build()[1]
