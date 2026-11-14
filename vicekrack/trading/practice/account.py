"""Practice account bookkeeping (Trading PR 1). SIMULATED MONEY ONLY.

A small, persistent simulated account: $100 of pretend cash, long-only crypto positions in
fractions, no leverage, no broker. Prices come in from the caller (later, the watch loop); this
module never fetches anything.

Every operation runs the shutdown guard first:
  1. `mark`  records the latest prices, then runs the guard. At or below the floor it closes
     every position at the latest price (with the same slippage and fee rules as the offline
     simulator) and halts the account.
  2. `buy`   is refused unless the guard says "ok", the account is active and the purchase
     can't push equity to the floor by itself (its slippage and fee are counted first).
     Cash may never go below zero: that is what "no leverage" means here.
  3. `sell`  closes a whole position. Selling is always allowed while the account is active,
     because closing reduces risk.

Trading PR 4 (whole market): any Kraken USD coin can be allowed (`"allowed_symbols": "any_usd"`),
your own buys can add to a coin you already hold, sells can be partial, and every position and fill
records who placed it ("you" or "agents"). The agents' own limits live in the trade desk.

Fill price, fee and quantity rules are reused from the offline simulator
(`simulation.engine.fill_price` / `fee_for`, `quantity.notional` / `round_down`).
"""

import re
from decimal import ROUND_DOWN, localcontext

from ..contracts import sha256
from ..errors import TradingError
from ..money import fmt, parse
from ..quantity import notional as quantity_notional, round_down, to_decimal
from ..simulation.engine import fee_for, fill_price
from . import guard

NOTICE = ("PRACTICE ACCOUNT: simulated money only. No broker, no real orders, no leverage. "
          "The $80 shutdown is a trigger, not a guaranteed maximum loss.")
REQUIRED_ACTION = "close_all_and_halt"
ANY_USD = "any_usd"
SYMBOL = re.compile(r"^[A-Z0-9]{1,12}USD$")
WHO = ("you", "agents")


def allowed(symbol, config):
    symbols = config["allowed_symbols"]
    if symbols == ANY_USD:
        return isinstance(symbol, str) and bool(SYMBOL.match(symbol))
    return symbol in symbols


def _ctx(context):
    context.prec = 50


def validate_config(config):
    """Refuse any configuration that would loosen the safety rules."""
    try:
        if config.get("config_version") != "1.0" or config.get("mode") != "practice":
            raise ValueError
        start = parse(config["account"]["starting_cash"], "money")
        floor = parse(config["shutdown"]["equity_floor"], "money")
        if config["account"]["currency"] != "USD" or not 0 < floor < start:
            raise ValueError
        if config["shutdown"]["action"] != REQUIRED_ACTION:
            raise ValueError
        if config["leverage"] is not False or config["allow_short"] is not False:
            raise ValueError
        symbols = config["allowed_symbols"]
        if symbols != ANY_USD and (not isinstance(symbols, list) or not symbols
                                   or not all(isinstance(s, str) and s.isalnum() and s.isupper() for s in symbols)):
            raise ValueError
        limits = config["limits"]
        if not 0 < parse(limits["max_order_notional"], "money") <= start:
            raise ValueError
        if not isinstance(limits["max_open_positions"], int) or isinstance(limits["max_open_positions"], bool) \
                or limits["max_open_positions"] < 1:
            raise ValueError
        age = limits["max_price_age_seconds"]
        if not isinstance(age, int) or isinstance(age, bool) or not 0 < age <= 3600:
            raise ValueError
        costs = config["costs"]
        parse(costs["fee_per_order"], "money")
        if parse(costs["slippage_bps"]) >= 10000 or parse(costs["fee_bps"]) >= 10000:
            raise ValueError
    except TradingError:
        raise TradingError("invalid_practice_config", "The practice account configuration is invalid.") from None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TradingError("invalid_practice_config", "The practice account configuration is invalid.") from None


def new_state(config, created_at):
    validate_config(config)
    cash = config["account"]["starting_cash"]
    account_id = "practice-" + sha256({"created_at": created_at, "config": config})[:20]
    return {"contract": "practice_account_state", "version": "1.0", "simulated": True, "account_id": account_id,
            "created_at": created_at, "config_sha256": sha256(config), "starting_cash": cash, "cash": cash,
            "floor": config["shutdown"]["equity_floor"], "status": "active", "halt": None, "positions": {},
            "marks": {}, "realized_pnl": "0", "fills": 0, "sequence": 0, "updated_at": created_at}


def validate_state(state, config):
    try:
        if state["contract"] != "practice_account_state" or state["version"] != "1.0" or state["simulated"] is not True:
            raise ValueError
        if state["status"] not in ("active", "halted") or (state["status"] == "halted") != (state["halt"] is not None):
            raise ValueError
        if parse(state["cash"], "signed_decimal") < 0:
            raise ValueError
        for symbol, position in state["positions"].items():
            if not allowed(symbol, config) or to_decimal(position["quantity"]) <= 0:
                raise ValueError
            parse(position["cost_basis"])
        for mark in state["marks"].values():
            parse(mark["price"])
        if state["status"] == "halted" and state["positions"]:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, TradingError):
        raise TradingError("practice_state_corrupt", "The saved practice account failed validation.") from None
    if state["config_sha256"] != sha256(config):
        raise TradingError("practice_config_changed",
                           "The practice settings changed since this account started. Restart the account to use new settings.")


def _fill(state, config, symbol, side, quantity, price, at, reason, by="agents"):
    """Apply one simulated fill (a whole or partial sell, or a buy that may add to your own position)."""
    costs = config["costs"]
    with localcontext() as context:
        _ctx(context)
        executed = fill_price(parse(price), side, parse(costs["slippage_bps"]))
        notional = quantity_notional(executed, quantity)
        fee = fee_for(notional, costs)
        cash = parse(state["cash"], "signed_decimal")
        if side == "buy":
            cash -= notional + fee
            if cash < 0:
                raise TradingError("insufficient_cash", "Not enough simulated cash; borrowing (leverage) is not allowed.")
            held = state["positions"].get(symbol)
            if held is None:
                state["positions"][symbol] = {"quantity": quantity, "cost_basis": fmt(notional + fee), "opened_at": at,
                                              "opened_by": by}
            else:
                held["quantity"] = fmt(to_decimal(held["quantity"]) + to_decimal(quantity))
                held["cost_basis"] = fmt(parse(held["cost_basis"]) + notional + fee)
            pnl = None
        else:
            position = state["positions"][symbol]
            held_quantity = to_decimal(position["quantity"])
            sold = to_decimal(quantity)
            basis = parse(position["cost_basis"])
            share = basis if sold >= held_quantity else (basis * sold / held_quantity)
            cash += notional - fee
            pnl = notional - fee - share
            if sold >= held_quantity:
                state["positions"].pop(symbol)
            else:
                position["quantity"] = fmt(held_quantity - sold)
                position["cost_basis"] = fmt(basis - share)
            state["realized_pnl"] = fmt(parse(state["realized_pnl"], "signed_decimal") + pnl)
        state["cash"] = fmt(cash)
    state["fills"] += 1
    return {"kind": "fill", "at": at, "symbol": symbol, "side": side, "quantity": quantity, "price": price,
            "fill_price": fmt(executed), "notional": fmt(notional), "fee": fmt(fee), "cash_after": state["cash"],
            "realized_pnl": None if pnl is None else fmt(pnl), "reason": reason, "by": by}


def _record_marks(state, config, prices, now):
    for symbol, mark in prices.items():
        if not allowed(symbol, config):
            raise TradingError("symbol_not_allowed", "That symbol is not on the practice account's list.")
        parse(mark["price"])
        if guard._seconds_between(mark["at"], now) < -5:
            raise TradingError("price_from_future", "A price is timed in the future; nothing was recorded.")
        previous = state["marks"].get(symbol)
        if previous is None or guard._seconds_between(previous["at"], mark["at"]) >= 0:
            state["marks"][symbol] = {"price": mark["price"], "at": mark["at"]}


def run_guard(state, config, now):
    """Run the guard; on shutdown close everything at the latest prices and halt. Returns (verdict, ledger records)."""
    verdict = guard.check(state, state["marks"], now, config)
    records = []
    if verdict["verdict"] == "shutdown":
        for symbol in sorted(state["positions"]):
            position = state["positions"][symbol]
            records.append(_fill(state, config, symbol, "sell", position["quantity"], state["marks"][symbol]["price"],
                                 now, "equity_floor_reached", by="guard"))
        state["status"] = "halted"
        state["halt"] = {"at": now, "reason": "equity_floor_reached", "equity_at_trigger": verdict["equity"],
                         "floor": verdict["floor"], "cash_after_closing": state["cash"]}
        records.append({"kind": "halt", "at": now, **state["halt"]})
    return verdict, records


def mark(state, config, prices, now):
    _record_marks(state, config, prices, now)
    return run_guard(state, config, now)


def buy(state, config, symbol, dollars, price, now, by="agents", marks=None):
    """Buy up to `dollars` (money string, or "all" for your own trades) of `symbol` at `price` observed `now`.
    `marks`: other fresh prices to record first (so equity is checked with every holding up to date)."""
    if state["status"] != "active":
        raise TradingError("account_halted", "The practice account is stopped. Only a person can restart it.")
    if by not in WHO:
        raise TradingError("invalid_requester", "Unknown trader.")
    if not allowed(symbol, config):
        raise TradingError("symbol_not_allowed", "That symbol is not on the practice account's list.")
    verdict, records = mark(state, config, {**(marks or {}), symbol: {"price": price, "at": now}}, now)
    if verdict["verdict"] == "shutdown":
        return verdict, records, TradingError("equity_floor_reached", "Equity hit the floor; everything was closed and the account stopped.")
    if verdict["verdict"] != "ok":
        return verdict, records, TradingError("equity_unverified", "Equity can't be verified with fresh prices; no new buys.")
    held = state["positions"].get(symbol)
    if held is not None and by == "you" and held.get("opened_by", "agents") != "you":
        return verdict, records, TradingError("position_already_open",
                                              "The agents are holding this coin right now. Buy it after they sell, or sell theirs.")
    if held is not None and by != "you":
        return verdict, records, TradingError("position_already_open", "There is already an open position in that coin.")
    if held is None and len(state["positions"]) >= config["limits"]["max_open_positions"]:
        return verdict, records, TradingError("too_many_positions", "The open-position limit is reached.")
    costs = config["costs"]
    limit = parse(config["limits"]["max_order_notional"], "money")
    with localcontext() as context:
        _ctx(context)
        if dollars == "all" and by == "you":
            room = parse(state["cash"], "signed_decimal") - parse(costs["fee_per_order"], "money")
            budget = (room / (1 + parse(costs["fee_bps"]) / 10000)).quantize(parse("0.01"), rounding=ROUND_DOWN)
            budget = min(budget, limit)
        elif dollars == "all":
            return verdict, records, TradingError("order_too_large", "Only you can buy with all your cash.")
        else:
            budget = parse(dollars, "money")
        if not 0 < budget <= limit:
            return verdict, records, TradingError("order_too_large", "Order size is outside the practice limit.")
        executed = fill_price(parse(price), "buy", parse(costs["slippage_bps"]))
        quantity = round_down(budget / executed)
        if quantity <= 0:
            return verdict, records, TradingError("sizing_zero_quantity", "The order is too small to buy anything.")
        notional = quantity_notional(executed, fmt(quantity))
        fee = fee_for(notional, costs)
        if notional + fee > parse(state["cash"], "signed_decimal"):
            return verdict, records, TradingError("insufficient_cash", "Not enough simulated cash; leverage is not allowed.")
        # Buying costs slippage and a fee immediately; refuse if that alone would reach the floor.
        cost_now = notional + fee - quantity * parse(price)
        if parse(verdict["equity"]) - cost_now <= parse(config["shutdown"]["equity_floor"], "money"):
            return verdict, records, TradingError("would_reach_floor", "This buy's costs would put equity at the floor.")
    records.append(_fill(state, config, symbol, "buy", fmt(quantity), price, now, "buy_requested", by=by))
    return verdict, records, None


def sell(state, config, symbol, price, now, by="agents", dollars=None, marks=None):
    """Sell `symbol` at `price`: the whole position, or about `dollars` worth of it. Exits are allowed while active."""
    if state["status"] != "active":
        raise TradingError("account_halted", "The practice account is stopped. Only a person can restart it.")
    if by not in WHO:
        raise TradingError("invalid_requester", "Unknown trader.")
    if symbol not in state["positions"]:
        raise TradingError("no_position", "There is no open position in that coin.")
    verdict, records = mark(state, config, {**(marks or {}), symbol: {"price": price, "at": now}}, now)
    if verdict["verdict"] == "shutdown":
        return verdict, records, None            # the guard already closed it
    held = to_decimal(state["positions"][symbol]["quantity"])
    quantity = held
    if dollars is not None and dollars != "all":
        with localcontext() as context:
            _ctx(context)
            amount = parse(dollars, "money")
            if amount <= 0:
                return verdict, records, TradingError("order_too_small", "Enter an amount to sell.")
            executed = fill_price(parse(price), "sell", parse(config["costs"]["slippage_bps"]))
            wanted = round_down(amount / executed)
            if wanted <= 0:
                return verdict, records, TradingError("sizing_zero_quantity", "The amount is too small to sell anything.")
            quantity = held if wanted >= held * parse("0.99") else wanted
    records.append(_fill(state, config, symbol, "sell", fmt(quantity), price, now, "sell_requested", by=by))
    return verdict, records, None


def summary(state, config, now):
    verdict = guard.check(state, state["marks"], now, config)
    room = None
    if verdict["equity"] is not None:
        room = fmt(guard.headroom(parse(verdict["equity"]), config))
    return {"notice": NOTICE, "simulated": True, "account_id": state["account_id"], "status": state["status"],
            "starting_cash": state["starting_cash"], "cash": state["cash"], "equity": verdict["equity"],
            "shutdown_floor": state["floor"], "dollars_above_floor": room, "guard": verdict["verdict"],
            "guard_problems": verdict["problems"], "positions": state["positions"], "latest_prices": state["marks"],
            "realized_pnl": state["realized_pnl"], "fills": state["fills"], "halt": state["halt"],
            "updated_at": state["updated_at"]}
