"""Deterministic paper risk engine.

`evaluate` runs a fixed list of checks and returns a `risk_decision` 1.0 document. The
outcome is "allowed" only when every check passes. Missing, malformed or stale inputs,
an engaged (or unreadable) kill switch, and any limit breach block authorization.
No randomness, no clock reads, no network: the caller supplies `as_of`.
"""

from decimal import Decimal

from .contracts import parse_time, sha256, snapshot_age_problem, validate_risk_decision
from .errors import TradingError
from .money import add, fmt, multiply, parse


def _check(checks, name, status, limit=None, observed=None):
    checks.append({"check": name, "status": status,
                   "limit": None if limit is None else fmt(limit) if isinstance(limit, Decimal) else str(limit),
                   "observed": None if observed is None else fmt(observed) if isinstance(observed, Decimal) else str(observed)})


def evaluate(*, config, config_sha256, snapshot, signal, portfolio, as_of, kill_switch,
             orders_today=0, used_signal_ids=(), input_problems=()):
    """Return a validated risk decision. Inputs that failed validation must be passed as None
    and named in `input_problems` (reason codes such as 'invalid_snapshot')."""
    checks, reasons = [], []

    def block(name, code, limit=None, observed=None):
        _check(checks, name, "fail", limit, observed)
        if code not in reasons:
            reasons.append(code)

    engaged, source = kill_switch
    if engaged:
        block("kill_switch", "kill_switch_engaged" if source != "switch_file_unreadable" else "kill_switch_unreadable")
    else:
        _check(checks, "kill_switch", "pass")

    missing = [name for name, value in (("config", config), ("snapshot", snapshot), ("signal", signal),
                                         ("portfolio", portfolio)) if value is None]
    if missing or input_problems:
        block("inputs_valid", "invalid_or_missing_inputs", observed=len(missing))
        for problem in input_problems:
            if problem not in reasons:
                reasons.append(problem)
        for name in missing:
            if f"missing_{name}" not in reasons and not any(p.endswith(name) for p in input_problems):
                reasons.append(f"missing_{name}")
    else:
        _check(checks, "inputs_valid", "pass")

    complete = not missing and not input_problems
    names = ("snapshot_fresh", "signal_active", "symbol_allowed", "symbol_consistent", "order_type_allowed",
             "quantity_limit", "order_notional_limit", "position_notional_limit", "position_fraction_limit",
             "no_short_position", "daily_loss_limit", "orders_per_day_limit", "signal_not_reused")
    if not complete:
        for name in names:
            _check(checks, name, "skipped")
    else:
        limits = config["limits"]
        proposal = signal["proposal"]
        problem = snapshot_age_problem(snapshot, as_of, limits["max_snapshot_age_seconds"])
        if problem:
            block("snapshot_fresh", problem, limits["max_snapshot_age_seconds"],
                  int((parse_time(as_of) - parse_time(snapshot["observed_at"])).total_seconds()))
        else:
            _check(checks, "snapshot_fresh", "pass", limits["max_snapshot_age_seconds"],
                   int((parse_time(as_of) - parse_time(snapshot["observed_at"])).total_seconds()))

        now, created, expires = parse_time(as_of), parse_time(signal["created_at"]), parse_time(signal["expires_at"])
        lifetime = int((expires - created).total_seconds())
        if now >= expires:
            block("signal_active", "signal_expired", limits["max_signal_lifetime_seconds"], lifetime)
        elif created > now:
            block("signal_active", "signal_from_future", limits["max_signal_lifetime_seconds"], lifetime)
        elif lifetime > limits["max_signal_lifetime_seconds"]:
            block("signal_active", "signal_lifetime_exceeded", limits["max_signal_lifetime_seconds"], lifetime)
        else:
            _check(checks, "signal_active", "pass", limits["max_signal_lifetime_seconds"], lifetime)

        symbol = signal["symbol"]
        if symbol in config["allowed_symbols"]:
            _check(checks, "symbol_allowed", "pass")
        else:
            block("symbol_allowed", "symbol_not_allowed")
        if snapshot["symbol"] == symbol:
            _check(checks, "symbol_consistent", "pass")
        else:
            block("symbol_consistent", "symbol_mismatch")
        if proposal["order_type"] in config["allowed_order_types"]:
            _check(checks, "order_type_allowed", "pass")
        else:
            block("order_type_allowed", "order_type_not_allowed")

        quantity = parse(proposal["quantity"])
        max_quantity = parse(limits["max_quantity"])
        if quantity <= 0 or quantity > max_quantity:
            block("quantity_limit", "quantity_limit_exceeded", max_quantity, quantity)
        else:
            _check(checks, "quantity_limit", "pass", max_quantity, quantity)

        price = parse(proposal["limit_price"]) if proposal["order_type"] == "limit" else parse(snapshot["price"]["last"])
        notional = multiply(quantity, price)
        max_order = parse(limits["max_order_notional"], "money")
        if notional > max_order:
            block("order_notional_limit", "order_notional_exceeded", max_order, notional)
        else:
            _check(checks, "order_notional_limit", "pass", max_order, notional)

        current = Decimal(0)
        for position in portfolio["positions"]:
            if position["symbol"] == symbol:
                current = parse(position["quantity"], "signed_decimal")
        delta = quantity if proposal["side"] == "buy" else -quantity
        resulting = add(current, delta)
        exposure = multiply(abs(resulting), price)
        max_position = parse(limits["max_position_notional"], "money")
        if exposure > max_position:
            block("position_notional_limit", "position_exposure_exceeded", max_position, exposure)
        else:
            _check(checks, "position_notional_limit", "pass", max_position, exposure)
        equity = parse(portfolio["equity"], "money")
        fraction_cap = multiply(parse(limits["max_position_fraction"]), equity)
        if exposure > fraction_cap:
            block("position_fraction_limit", "position_fraction_exceeded", fraction_cap, exposure)
        else:
            _check(checks, "position_fraction_limit", "pass", fraction_cap, exposure)
        if resulting < 0 and not config["allow_short"]:
            block("no_short_position", "short_selling_not_allowed", 0, resulting)
        else:
            _check(checks, "no_short_position", "pass", 0, resulting)

        pnl = parse(portfolio["day"]["pnl"], "signed_money")
        max_loss = parse(limits["max_daily_loss"], "money")
        if portfolio["day"]["trading_date"] != as_of[:10]:
            block("daily_loss_limit", "portfolio_day_mismatch")
        elif -pnl >= max_loss:
            block("daily_loss_limit", "daily_loss_limit_reached", max_loss, -pnl)
        else:
            _check(checks, "daily_loss_limit", "pass", max_loss, -pnl)

        if orders_today >= limits["max_orders_per_day"]:
            block("orders_per_day_limit", "orders_per_day_exceeded", limits["max_orders_per_day"], orders_today)
        else:
            _check(checks, "orders_per_day_limit", "pass", limits["max_orders_per_day"], orders_today)
        if signal["signal_id"] in set(used_signal_ids):
            block("signal_not_reused", "duplicate_signal")
        else:
            _check(checks, "signal_not_reused", "pass")

    inputs = {"config_sha256": config_sha256 if config is not None else None,
              "signal_sha256": sha256(signal) if signal is not None else None,
              "snapshot_sha256": sha256(snapshot) if snapshot is not None else None,
              "portfolio_sha256": sha256(portfolio) if portfolio is not None else None}
    signal_id = signal["signal_id"] if signal is not None else None
    snapshot_id = snapshot["snapshot_id"] if snapshot is not None else None
    seed = {"inputs": inputs, "as_of": as_of, "checks": checks, "reasons": reasons,
            "orders_today": orders_today, "signal_id": signal_id, "snapshot_id": snapshot_id}
    decision = {
        "contract": "risk_decision", "version": "1.0", "decision_id": "rdec-" + sha256(seed)[:24], "mode": "paper",
        "signal_id": signal_id, "snapshot_id": snapshot_id, "decided_at": as_of,
        "outcome": "blocked" if reasons else "allowed", "reason_codes": reasons, "checks": checks, "inputs": inputs,
    }
    try:
        validate_risk_decision(decision)
    except TradingError:
        raise TradingError("risk_engine_failed", "The risk engine produced an invalid decision; nothing was authorized.") from None
    return decision


def reference_price(signal, snapshot):
    proposal = signal["proposal"]
    return parse(proposal["limit_price"]) if proposal["order_type"] == "limit" else parse(snapshot["price"]["last"])
