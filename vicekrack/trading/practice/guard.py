"""The $80 shutdown guard (Trading PR 1). PLAIN CODE, NOT AI.

This module decides one thing: is the practice account still allowed to trade?

    equity = cash + sum(quantity x latest price) for every open position

- If any open position has no price, or its price is older than `max_price_age_seconds`,
  equity cannot be verified: new buys are blocked (`equity_unverified`). Nothing is closed
  on a guess.
- If verified equity is at or below the floor, the account must close every position at the
  latest price and stop (`equity_floor_reached`). Only a person can restart it.

It imports nothing from the research agents, AI providers, brokers or the network, and a
test checks that. Agents can ask the account to buy; they cannot reach or change this check.
The floor is a trigger, not a guaranteed maximum loss: a price can gap below it between
checks, and closing costs slippage and fees.
"""

from datetime import datetime, timezone
from decimal import localcontext

from ..errors import TradingError
from ..money import fmt, parse
from ..quantity import to_decimal


def _ctx(context):
    context.prec = 50


def _seconds_between(earlier, later):
    def parse_time(text):
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            raise TradingError("invalid_price_time", "A price time is not a valid UTC time.") from None
        if moment.tzinfo is None:
            raise TradingError("invalid_price_time", "A price time must include its time zone.")
        return moment.astimezone(timezone.utc)
    return (parse_time(later) - parse_time(earlier)).total_seconds()


def equity(state, marks, now, max_age_seconds):
    """Return (equity Decimal or None, problems list). None means it cannot be verified."""
    problems = []
    with localcontext() as context:
        _ctx(context)
        total = parse(state["cash"], "decimal")
        for symbol, position in sorted(state["positions"].items()):
            mark = marks.get(symbol)
            if mark is None:
                problems.append(f"no_price:{symbol}")
                continue
            age = _seconds_between(mark["at"], now)
            if age < -5:
                problems.append(f"price_from_future:{symbol}")
                continue
            if age > max_age_seconds:
                problems.append(f"stale_price:{symbol}")
                continue
            total += to_decimal(position["quantity"]) * parse(mark["price"])
    return (None if problems else total), problems


def check(state, marks, now, config):
    """The guard's verdict. Pure: reads its inputs, changes nothing."""
    floor = parse(config["shutdown"]["equity_floor"], "money")
    if state["status"] == "halted":
        return {"verdict": "halted", "equity": None, "floor": fmt(floor), "problems": ["account_halted"]}
    value, problems = equity(state, marks, now, config["limits"]["max_price_age_seconds"])
    if value is None:
        return {"verdict": "unverified", "equity": None, "floor": fmt(floor), "problems": problems}
    if value <= floor:
        return {"verdict": "shutdown", "equity": fmt(value), "floor": fmt(floor), "problems": ["equity_floor_reached"]}
    return {"verdict": "ok", "equity": fmt(value), "floor": fmt(floor), "problems": []}


def headroom(value, config):
    """Dollars between verified equity and the floor."""
    with localcontext() as context:
        _ctx(context)
        return value - parse(config["shutdown"]["equity_floor"], "money")

