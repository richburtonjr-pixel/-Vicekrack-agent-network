"""Risk Manager, Position Monitor and Performance Reviewer (Trading PR 3). Plain code, no AI.

Risk Manager   signs or vetoes a trade idea using the research Risk Review's verdict plus the
               practice account itself: the $80 guard must say ok, there must be room above the
               floor, no position already in that coin, daily and losing-streak limits, and a short
               wait after closing a coin before buying it again.
Position Monitor  checks every open position every cycle and asks to close it when: the price
               falls to the stop-loss, the position has been held too long, or the coin's own short
               EMA drops below its long EMA (the uptrend that justified the buy is gone).
Performance Reviewer  reads the practice ledger and reports closed trades, wins, losses and P&L.

The $80 guard itself stays in `practice/guard.py`; nothing here can loosen it.
"""

from datetime import datetime, timezone
from decimal import Decimal, localcontext

from ..money import fmt
from .verifier import ema_series


def _ctx(context):
    context.prec = 50


def _time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _minutes_between(earlier, later):
    return (_time(later) - _time(earlier)).total_seconds() / 60


def risk_manager(symbol, research_verdict, practice, history, now, config):
    """practice: the practice account summary. history: {"entries_today", "last_exit_at", "losing_streak",
    "last_loss_at"} for the current account."""
    reasons = []
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
    if history["losing_streak"] >= config["pause_after_losses"] and history["last_loss_at"] \
            and _minutes_between(history["last_loss_at"], now) < config["pause_minutes"]:
        reasons.append("losing_streak_pause")
    if reasons:
        return {"agent": "risk_review", "decision": "veto", "reasons": reasons,
                "summary": "Account risk check failed: " + ", ".join(reasons) + "."}
    return {"agent": "risk_review", "decision": "approve", "reasons": ["research_complete", "account_checks_passed"],
            "summary": f"Research complete; guard ok with ${fmt(Decimal(room))} above the floor; limits respected."}


def position_monitor(symbol, position, bars, mark, now, config):
    """Return exit reasons for one open position ([] = keep holding)."""
    reasons = []
    with localcontext() as context:
        _ctx(context)
        quantity = Decimal(position["quantity"])
        cost_each = Decimal(position["cost_basis"]) / quantity
        stop = cost_each * (1 - Decimal(config["stop_loss_pct"]) / 100)
        if mark is not None and Decimal(mark["price"]) <= stop:
            reasons.append("stop_loss")
        if _minutes_between(position["opened_at"], now) >= config["max_hold_minutes"]:
            reasons.append("held_too_long")
        if bars:
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
