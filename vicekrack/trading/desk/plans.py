"""Trade plans: every agent position gets explicit risk limits BEFORE it is bought. Plain code, no AI.

Sizing (risk-based)
  1. The coin's normal wiggle: ATR = average true range of the last `atr_bars` one-minute bars, as % of price.
  2. Initial stop distance = `atr_multiple` x ATR, kept between `min_stop_pct` and `max_stop_pct`.
     A jumpy coin gets a wider stop; a calm coin a tighter one.
  3. Dollars at risk = `risk_per_trade_pct` of the account's equity.
  4. Trade size = dollars at risk / (stop distance + round-trip costs), so hitting the stop loses about the
     planned amount after fees and slippage. Capped at `max_position_pct` of equity, `max_order_dollars` and the
     per-trade limit `order_dollars` (never above it);
     below `min_order_dollars` the trade is skipped (too small to beat its own costs).
     `mode: "fixed"` keeps the old flat `order_dollars` (the stop still comes from the plan).

The plan is saved in runtime/trading/desk/plans.json (per account and position) and drives every exit:
  stop          the initial stop price; moved to break-even (plus costs) once the trade has gained
                `breakeven_at_r` x its risk ("R")
  partial       at `partial_at_r` R, sell `partial_fraction` of the position once, then the rest runs
  profit trail  unchanged (profit_trail.py): lets the rest run and protects gains in points
  time stop     after `max_hold_minutes`, close only if it has not reached `time_stop_min_r` R
  trend exit    only after `min_hold_minutes`, and only on `trend_bar_minutes`-minute bars
Risk events (Emergency Stop, the stop, the $80 shutdown) always act immediately.
"""

import json
import os
import tempfile
from decimal import Decimal, localcontext
from pathlib import Path

from ..contracts import ROOT


def _ctx(context):
    context.prec = 50


def atr_pct(bars, length):
    """Average true range of the last `length` bars, as % of the last close. None if not enough bars."""
    if len(bars) < length + 1:
        return None
    with localcontext() as context:
        _ctx(context)
        ranges = []
        for before, bar in zip(bars[-length - 1:-1], bars[-length:]):
            high, low, prev = Decimal(bar["high"]), Decimal(bar["low"]), Decimal(before["close"])
            ranges.append(max(high - low, abs(high - prev), abs(low - prev)))
        last = Decimal(bars[-1]["close"])
        return None if last <= 0 else sum(ranges, Decimal(0)) / len(ranges) / last * 100


def size_entry(bars, equity, sizing, round_trip_cost_pct, order_dollars):
    """-> {"dollars", "stop_pct", "risk_dollars", "atr_pct"} or {"skip": reason}."""
    with localcontext() as context:
        _ctx(context)
        atr = atr_pct(bars, sizing["atr_bars"])
        low, high = Decimal(sizing["min_stop_pct"]), Decimal(sizing["max_stop_pct"])
        stop = low if atr is None else min(max(atr * Decimal(sizing["atr_multiple"]), low), high)
        equity = Decimal(equity)
        risk = equity * Decimal(sizing["risk_per_trade_pct"]) / 100
        if sizing.get("mode", "risk") == "fixed":
            dollars = Decimal(order_dollars)
        else:
            dollars = risk / ((stop + Decimal(round_trip_cost_pct)) / 100)
            # order_dollars is the person's per-trade limit (dashboard): risk sizing may go below it, never above.
            dollars = min(dollars, equity * Decimal(sizing["max_position_pct"]) / 100, Decimal(sizing["max_order_dollars"]),
                          Decimal(order_dollars))
        dollars = dollars.quantize(Decimal("0.01"), rounding="ROUND_DOWN")
        if dollars < Decimal(sizing["min_order_dollars"]):
            return {"skip": "position_too_small", "stop_pct": stop, "atr_pct": atr}
        planned_loss = dollars * (stop + Decimal(round_trip_cost_pct)) / 100
        return {"dollars": format(dollars, "f"), "stop_pct": stop, "atr_pct": atr,
                "risk_dollars": format(planned_loss.quantize(Decimal("0.01")), "f")}


def new_plan(position, sized, exits, case_id, idea, round_trip_cost_pct):
    """The plan for a position that was just bought (cost_each includes the buy's slippage and fee)."""
    with localcontext() as context:
        _ctx(context)
        cost_each = Decimal(position["cost_basis"]) / Decimal(position["quantity"])
        stop_pct = Decimal(sized["stop_pct"])
        risk_each = cost_each * stop_pct / 100
        return {"opened_at": position["opened_at"], "entry_case_id": case_id, "idea": idea,
                "cost_each": format(cost_each, "f"), "stop_pct": format(stop_pct.quantize(Decimal("0.01")), "f"),
                "stop_price": format(cost_each - risk_each, "f"), "risk_each": format(risk_each, "f"),
                "risk_dollars": sized["risk_dollars"], "atr_pct": None if sized["atr_pct"] is None
                else format(Decimal(sized["atr_pct"]).quantize(Decimal("0.01")), "f"),
                "breakeven_price": format(cost_each * (1 + Decimal(round_trip_cost_pct) / 200), "f"),
                "breakeven_done": False, "partial_done": False,
                "partial_at_r": exits.get("partial_at_r"), "partial_fraction": exits.get("partial_fraction")}


def r_multiple(plan, price):
    """How many R the trade is up (or down) at this price, before selling costs."""
    with localcontext() as context:
        _ctx(context)
        risk = Decimal(plan["risk_each"])
        return None if risk <= 0 else (Decimal(price) - Decimal(plan["cost_each"])) / risk


def resample(bars, minutes):
    """One-minute bars -> `minutes`-minute closes (only complete groups, oldest first; no future bars)."""
    closes = [Decimal(b["close"]) for b in bars]
    usable = len(closes) - len(closes) % minutes
    return [closes[i + minutes - 1] for i in range(len(closes) - usable, len(closes), minutes)] if usable else []


class Plans:
    def __init__(self, root=None):
        self.path = Path(root if root is not None else ROOT) / "runtime/trading/desk/plans.json"

    def load(self, account_id):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            mine = data.get(account_id) if isinstance(data, dict) else None
            return mine if isinstance(mine, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def save(self, account_id, plans, positions):
        """Keep plans only for positions still open with the same opened_at."""
        kept = {s: p for s, p in plans.items() if s in positions and positions[s].get("opened_at") == p.get("opened_at")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False) as out:
            json.dump({account_id: kept}, out)
            temporary = out.name
        os.replace(temporary, self.path)
        return kept
