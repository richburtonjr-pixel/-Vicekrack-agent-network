"""Risk Manager, Position Monitor and Performance Reviewer (Trading PR 3). Plain code, no AI.

Risk Manager   signs or vetoes a trade idea using the research Risk Review's verdict plus the
               practice account itself: the $80 guard must say ok, there must be room above the
               floor, no position already in that coin, daily and losing-streak limits, and a short
               wait after closing a coin before buying it again.
Position Monitor  checks every open position every cycle and asks to close it when: the price
               falls to the stop-loss; the PROFIT TRAIL is hit (profit fell a set number of points
               from the best profit reached; see profit_trail.py; there is no fixed take-profit); the
               Emergency Stop is on; or, only while the profit trail isn't active yet, the position
               has been held too long or the coin's own short EMA drops below its long EMA. Once the
               trail is active (and let_winners_run is on) a winner is never sold for time or a
               wiggle in the trend: only the trail, the stop-loss or the Emergency Stop close it.
Performance Reviewer  reads the practice ledger and reports closed trades, wins, losses and P&L.

The $80 guard itself stays in `practice/guard.py`; nothing here can loosen it.
"""

from datetime import datetime, timezone
from decimal import Decimal, localcontext

from ..money import fmt
from . import profit_trail
from .verifier import ema_series


def _ctx(context):
    context.prec = 50


def _time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _minutes_between(earlier, later):
    return (_time(later) - _time(earlier)).total_seconds() / 60


def risk_manager(symbol, research_verdict, practice, history, now, config):
    """practice: the practice account summary. history: {"entries_today", "last_exit_at", "losing_streak",
    "last_loss_at", "agents_pnl_today", "emergency"} for the current account."""
    reasons = []
    if history.get("emergency"):
        reasons.append("emergency_stop")
    if research_verdict != "sufficient_for_future_paper_evaluation":
        reasons.append("research_incomplete")
    if practice["status"] != "active":
        reasons.append("account_stopped")
    if practice["guard"] != "ok":
        reasons.append(f"guard_{practice['guard']}")
    room = practice.get("dollars_above_floor")
    if room is None or Decimal(room) < Decimal(config["min_dollars_above_floor"]):
        reasons.append("too_close_to_floor")
    if symbol in practice["positions"]:
        reasons.append("already_holding")
    agent_held = sum(1 for p in practice["positions"].values() if p.get("opened_by", "agents") == "agents")
    if agent_held >= config["max_agent_positions"]:
        reasons.append("agent_position_limit")
    if history["entries_today"] >= config["max_entries_per_day"]:
        reasons.append("daily_entry_limit")
    last_exit = history["last_exit_at"].get(symbol)
    if last_exit and _minutes_between(last_exit, now) < config["reentry_wait_minutes"]:
        reasons.append("reentry_wait")
    limit = config.get("max_daily_loss_dollars")
    if limit is not None and Decimal(history.get("agents_pnl_today") or "0") <= -Decimal(limit):
        reasons.append("daily_loss_limit")
    if history["losing_streak"] >= config["pause_after_losses"] and history["last_loss_at"] \
            and _minutes_between(history["last_loss_at"], now) < config["pause_minutes"]:
        reasons.append("losing_streak_pause")
    if reasons:
        return {"agent": "risk_review", "decision": "veto", "reasons": reasons,
                "summary": "Account risk check failed: " + ", ".join(reasons) + "."}
    return {"agent": "risk_review", "decision": "approve", "reasons": ["research_complete", "account_checks_passed"],
            "summary": f"Research complete; guard ok with ${fmt(Decimal(room))} above the floor; limits respected."}


def position_monitor(symbol, position, bars, mark, now, config, peak=None, emergency=False):
    """Return exit reasons for one open position ([] = keep holding). `peak`: highest price seen since buying."""
    reasons = ["emergency_stop"] if emergency else []
    with localcontext() as context:
        _ctx(context)
        quantity = Decimal(position["quantity"])
        cost_each = Decimal(position["cost_basis"]) / quantity
        stop = cost_each * (1 - Decimal(config["stop_loss_pct"]) / 100)
        price = Decimal(mark["price"]) if mark is not None else None
        if price is not None and price <= stop:
            reasons.append("stop_loss")
        trail = config.get("profit_trail")
        trail_active = False
        if trail is not None and price is not None:
            state = profit_trail.check(cost_each, price, peak if peak is not None else price, trail,
                                       config.get("exit_cost_bps", "0"))
            trail_active = state["active"]
            if state["sell"]:
                reasons.append("profit_trail")
        winners_run = trail_active and trail.get("let_winners_run", True)
        if not winners_run and _minutes_between(position["opened_at"], now) >= config["max_hold_minutes"]:
            reasons.append("held_too_long")
        if bars and not winners_run:
            closes = [Decimal(b["close"]) for b in bars]
            fast, slow = ema_series(closes, config["ema_fast"]), ema_series(closes, config["ema_slow"])
            if fast[-1] is not None and slow[-1] is not None and fast[-1] < slow[-1]:
                reasons.append("trend_turned_down")
    return reasons


def performance(ledger_lines, account_id, practice):
    """Closed-trade statistics for the current practice account."""
    sells = [l for l in ledger_lines if l.get("account_id") == account_id and l.get("kind") == "fill"
             and l.get("side") == "sell" and l.get("realized_pnl") is not None]
    with localcontext() as context:
        _ctx(context)
        pnls = [Decimal(l["realized_pnl"]) for l in sells]
        wins = [p for p in pnls if p > 0]
        total = sum(pnls, Decimal(0))
        equity = practice.get("equity")
        change = None if equity is None else Decimal(equity) - Decimal(practice["starting_cash"])
    return {"agent": "performance_reviewer", "closed_trades": len(pnls), "wins": len(wins),
            "losses": len(pnls) - len(wins),
            "win_rate_pct": None if not pnls else fmt(Decimal(len(wins)) * 100 / len(pnls)),
            "realized_pnl": fmt(total), "best": fmt(max(pnls)) if pnls else None,
            "worst": fmt(min(pnls)) if pnls else None,
            "equity_change": None if change is None else fmt(change),
            "note": "Practice results on simulated money; not a prediction."}
