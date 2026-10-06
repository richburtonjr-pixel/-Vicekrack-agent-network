"""Offline paper-execution simulator (Step 29). SIMULATED ONLY.

One bounded Step 25 replay drives a SimulationConsumer that, for every newly closed bar
(in sequence order):

1. FILLS pending orders whose `not_before_utc` <= the bar's start, at the bar's OPEN
   (the only price of that bar used for execution):
     buy  fill_price = open x (1 + slippage_bps / 10000)   rounded half-even to 8 places
     sell fill_price = open x (1 - slippage_bps / 10000)   rounded half-even to 8 places
     notional = quantity x fill_price (exact)
     fee = fee_per_order + notional x fee_bps / 10000      rounded half-even to cents
   Entry checks at fill time (the order is REJECTED, never resized): a gap since the
   order was accepted (`entry_gap`), the signal's expiry reached (`signal_expired_before_fill`),
   order or position notional above the limits at the fill price, or cash < notional + fee
   (`insufficient_cash`). Exits sell the whole position; a gap before an exit fill is
   recorded (`gap_before_fill`) but the exit still fills at the next available open.
2. After the bar closes: counts the bar for any open position, evaluates exit rules on the
   closed bar (opposite EMA crossover: fast(t-1) >= slow(t-1) and fast(t) < slow(t), both
   ready and consecutive; or bars held >= max_holding_bars) and accepts research signals
   detected on this bar as entry orders. New orders get not_before_utc = the simulated
   time they were decided, so they fill at the next available bar's open at the earliest.

Accounting: buy fees are capitalised into the position's cost basis; realized P&L =
proceeds - exit fee - cost basis; open positions at the end are marked to the last close
(unrealized P&L = quantity x last close - cost basis) and NEVER liquidated; orders still
pending at the end are `pending_at_end_of_data`.

Isolation: own in-run account (`simacct-...`), own storage, own kill switch. This module
never imports Step 24 paper accounts, the paper risk engine, paper intents, the journal or
the Step 28 agents; research signals stay non-authorizing everywhere else.
"""

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, localcontext

from ..contracts import sha256, validate_schema
from ..errors import TradingError
from ..indicators.engine import IndicatorConsumer, build_settings
from ..market.bars import from_utc_text, missing_between
from ..market.replay import drive, plan
from ..money import fmt, parse
from ..signals.engine import SignalConsumer, indicator_settings_for, load_strategies, provenance, validate_signal_record

NOTICE = ("SIMULATED offline execution on historical data. No broker, no live or paper-account orders; results are "
          "not a prediction or profitability claim.")
PASSTHROUGH = {"indicator_window_too_small", "indicator_too_many_points", "signal_too_many_evaluations",
               "sim_too_many_orders"}
CENT, PRICE = Decimal("0.01"), Decimal("0.00000001")
TEN_THOUSAND = Decimal(10000)


def _ctx(context):
    context.prec = 50
    context.rounding = ROUND_HALF_EVEN


def fill_price(open_price, side, slippage_bps):
    with localcontext() as context:
        _ctx(context)
        factor = (1 + slippage_bps / TEN_THOUSAND) if side == "buy" else (1 - slippage_bps / TEN_THOUSAND)
        return (open_price * factor).quantize(PRICE, rounding=ROUND_HALF_EVEN)


def fee_for(notional, costs):
    with localcontext() as context:
        _ctx(context)
        raw = parse(costs["fee_per_order"], "money") + notional * parse(costs["fee_bps"]) / TEN_THOUSAND
        return raw.quantize(CENT, rounding=ROUND_HALF_EVEN)


def validate_policy(policy):
    validate_schema("simulation_policy", policy)
    exits = policy["exits"]
    if exits["opposite_ema_crossover"] and not exits["opposite_ema_crossover"]["fast"] < exits["opposite_ema_crossover"]["slow"]:
        raise TradingError("invalid_simulation_policy", "The exit EMA crossover needs fast < slow.")
    if parse(policy["costs"]["slippage_bps"]) >= TEN_THOUSAND:
        raise TradingError("invalid_simulation_policy", "slippage_bps must be below 10000.")
    if parse(policy["account"]["initial_cash"], "money") <= 0:
        raise TradingError("invalid_simulation_policy", "initial_cash must be greater than zero.")
    if policy["sizing"]["method"] == "fixed_notional" and parse(policy["sizing"]["notional"], "money") <= 0:
        raise TradingError("invalid_simulation_policy", "Sizing notional must be greater than zero.")


class SimulationConsumer:
    name = "paper_simulation"

    def __init__(self, dataset, policy, strategies, signal_settings, indicator_config, signal_config, kill_switch, run_id,
                 start_utc):
        self.dataset, self.policy, self.run_id, self.kill_switch = dataset, policy, run_id, kill_switch
        self.signals = SignalConsumer(dataset, strategies, signal_settings, indicator_config,
                                      signal_config["limits"]["max_evaluations"])
        exit_ema = policy["exits"]["opposite_ema_crossover"]
        self.exit_keys = None
        self.exit_indicators = None
        if exit_ema:
            settings = build_settings(dataset, config=indicator_config, ema=sorted({exit_ema["fast"], exit_ema["slow"]}),
                                      gap_policy="reset")
            self.exit_indicators = IndicatorConsumer(dataset, settings, indicator_config["limits"]["max_points"])
            self.exit_keys = (f"ema_{exit_ema['fast']}", f"ema_{exit_ema['slow']}")
        self.costs = {k: policy["costs"][k] for k in ("fee_per_order", "fee_bps")}
        self.slippage = parse(policy["costs"]["slippage_bps"])
        self.limits = policy["limits"]
        self.cash = parse(policy["account"]["initial_cash"], "money")
        self.ledger = [self._ledger_entry(start_utc, "initial_cash", self.cash, None)]
        self.orders, self.fills, self.positions, self.pending = [], [], [], []
        self.position = None
        self.used_signals, self.signals_seen = set(), 0
        self.accepted = 0
        self.index, self.previous_start, self.last_bar = -1, None, None
        self.signal_cursor = 0

    # ------------------------------------------------------------------ replay hook
    def on_step(self, view):
        before = self.signals.indicators.last_sequence
        self.signals.on_step(view)                       # validates window; processes new closed bars
        if self.exit_indicators is not None:
            self.exit_indicators.on_step(view)
        for sequence in range(before + 1, self.signals.indicators.last_sequence + 1):
            bar = view.bar(sequence)
            if bar["available_at_utc"] > view.now:
                raise TradingError("future_bar_leak", "A future bar reached the simulator.")
            self._bar(bar, view.now)

    def final(self):
        return {"bars_processed": self.index + 1}

    # ------------------------------------------------------------------ per bar
    def _bar(self, bar, now):
        self.index += 1
        start = from_utc_text(bar["timestamp_utc"])
        missing = (missing_between(self.previous_start, start, self.dataset["interval"], self.dataset["timezone"], "sim")
                   if self.previous_start is not None else 0)
        self.previous_start = start
        for order in list(self.pending):
            if order["purpose"] == "entry" and missing:
                self._reject(order, bar["timestamp_utc"], ["entry_gap"])
            elif bar["timestamp_utc"] >= order["not_before_utc"]:
                self._fill(order, bar, missing)
        self.last_bar = bar
        if self.position is not None:
            self.position["bars_held"] += 1
        self._exit_rules(bar, now, missing)
        signals = self.signals.signals                   # only signals for bars up to this one, in order
        while self.signal_cursor < len(signals) and signals[self.signal_cursor]["bar"]["sequence"] <= bar["sequence"]:
            signal = signals[self.signal_cursor]
            self.signal_cursor += 1
            self.signals_seen += 1
            self._entry(signal, bar, now)

    # ------------------------------------------------------------------ orders
    def _order(self, purpose, side, quantity, source, now, expires, estimate, status, reasons):
        identity = source["signal_id"] or f"{source['rule']}@{source['decision_bar_sequence']}"
        order = {
            "contract": "simulation_order", "version": "1.0",
            "order_id": "sord-" + sha256({"run_id": self.run_id, "purpose": purpose, "source": identity})[:24],
            "simulated": True, "run_id": self.run_id, "side": side, "order_type": "market", "purpose": purpose,
            "symbol": self.dataset["symbol"], "quantity": quantity, "source": source, "not_before_utc": now,
            "expires_at_utc": expires, "estimate": estimate, "status": status, "fill_id": None,
            "history": [{"at_utc": now, "status": "accepted" if status == "pending" else "rejected", "reason_codes": reasons}],
        }
        if any(o["order_id"] == order["order_id"] for o in self.orders):
            raise TradingError("sim_duplicate_order", "A simulated order with this ID already exists.")
        self.orders.append(order)
        if status == "pending":
            self.pending.append(order)
            self.accepted += 1
        return order

    def _entry(self, signal, bar, now):
        validate_signal_record(signal)
        if signal["dataset"]["dataset_id"] != self.dataset["dataset_id"] or signal["bar"]["sequence"] != bar["sequence"]:
            raise TradingError("sim_signal_provenance", "A research signal does not belong to this simulation's bar.")
        source = {"kind": "research_signal", "signal_id": signal["signal_id"], "strategy": signal["strategy"]["name"],
                  "rule": None, "decision_bar_sequence": bar["sequence"], "decision_at_utc": now}
        reasons, quantity, estimate = [], None, {"reference_price": None, "notional": None}
        if self.kill_switch[0]:
            reasons.append("kill_switch_engaged")
        if signal["expired_when_detected"] or signal["expires_at_utc"] <= now:
            reasons.append("signal_expired")
        if signal["signal_id"] in self.used_signals:
            reasons.append("duplicate_signal")
        if self.position is not None:
            reasons.append("position_already_open")
        if self.pending:
            reasons.append("pending_order_exists")
        if self.accepted >= self.limits["max_orders"]:
            reasons.append("order_limit_reached")
        self.used_signals.add(signal["signal_id"])
        close = parse(bar["close"])
        reference = fill_price(close, "buy", self.slippage)
        sizing = self.policy["sizing"]
        if sizing["method"] == "fixed_quantity":
            quantity = sizing["quantity"]
        else:
            with localcontext() as context:
                _ctx(context)
                quantity = int((parse(sizing["notional"], "money") / reference).to_integral_value(rounding=ROUND_DOWN))
            if quantity < 1:
                reasons.append("sizing_zero_quantity")
                quantity = None
        if quantity is not None:
            notional = reference * quantity
            estimate = {"reference_price": fmt(reference), "notional": fmt(notional)}
            if notional > parse(self.limits["max_order_notional"], "money"):
                reasons.append("order_notional_limit")
            if notional > parse(self.limits["max_position_notional"], "money"):
                reasons.append("position_exposure_limit")
            if notional + fee_for(notional, self.costs) > self.cash:
                reasons.append("insufficient_cash_estimate")
        if reasons:
            self._order("entry", "buy", quantity, source, now, signal["expires_at_utc"], estimate, "rejected", reasons[:10])
        else:
            self._order("entry", "buy", quantity, source, now, signal["expires_at_utc"], estimate, "pending",
                        ["signal_accepted_by_policy", "fills_at_next_available_open"])

    def _exit_rules(self, bar, now, missing):
        if self.position is None or any(o["purpose"] == "exit" for o in self.pending):
            return
        reasons = []
        held = self.policy["exits"]["max_holding_bars"]
        if held is not None and self.position["bars_held"] >= held:
            reasons.append("max_holding_bars_reached")
        if self.exit_keys is not None and not missing:
            fast_series = self.exit_indicators.series[self.exit_keys[0]]
            slow_series = self.exit_indicators.series[self.exit_keys[1]]
            if self.index >= 1:
                points = [fast_series[self.index - 1], slow_series[self.index - 1], fast_series[self.index], slow_series[self.index]]
                if all(p["status"] == "ready" for p in points):
                    fp, sp, fc, sc = (parse(p["value"]) for p in points)
                    if fp >= sp and fc < sc:
                        reasons.append("ema_crossed_below")
        if not reasons:
            return
        rule = "opposite_ema_crossover" if "ema_crossed_below" in reasons else "max_holding_bars"
        source = {"kind": "exit_rule", "signal_id": None, "strategy": None, "rule": rule,
                  "decision_bar_sequence": bar["sequence"], "decision_at_utc": now}
        self._order("exit", "sell", self.position["quantity"], source, now, None,
                    {"reference_price": bar["close"], "notional": fmt(parse(bar["close"]) * self.position["quantity"])},
                    "pending", reasons + ["fills_at_next_available_open"])

    def _reject(self, order, at, reasons):
        order["status"] = "rejected"
        order["history"].append({"at_utc": at, "status": "rejected", "reason_codes": reasons})
        self.pending.remove(order)

    # ------------------------------------------------------------------ fills
    def _fill(self, order, bar, missing):
        at = bar["timestamp_utc"]
        open_price = parse(bar["open"])
        price = fill_price(open_price, order["side"], self.slippage)
        quantity = order["quantity"]
        notional = price * quantity
        fee = fee_for(notional, self.costs)
        if order["purpose"] == "entry":
            reasons = []
            if order["expires_at_utc"] is not None and at >= order["expires_at_utc"]:
                reasons.append("signal_expired_before_fill")
            if notional > parse(self.limits["max_order_notional"], "money"):
                reasons.append("order_notional_limit_at_fill")
            if notional > parse(self.limits["max_position_notional"], "money"):
                reasons.append("position_exposure_limit_at_fill")
            if notional + fee > self.cash:
                reasons.append("insufficient_cash")
            if reasons:
                self._reject(order, at, reasons)
                return
            cash_change = -(notional + fee)
        else:
            cash_change = notional - fee
        fill_id = "sfil-" + sha256({"order_id": order["order_id"]})[:24]
        if any(f["fill_id"] == fill_id for f in self.fills) or order["fill_id"] is not None:
            raise TradingError("sim_duplicate_fill", "An order can be filled only once.")
        reasons = ["filled_at_bar_open"] + (["gap_before_fill"] if missing else [])
        fill = {"contract": "simulation_fill", "version": "1.0", "fill_id": fill_id, "simulated": True,
                "order_id": order["order_id"], "side": order["side"], "symbol": order["symbol"], "quantity": quantity,
                "bar_sequence": bar["sequence"], "bar_open_utc": at, "open_price": bar["open"],
                "slippage_bps": self.policy["costs"]["slippage_bps"], "fill_price": fmt(price), "notional": fmt(notional),
                "fee": fmt(fee), "cash_change": fmt(cash_change), "gap_before_fill": missing, "reason_codes": reasons}
        self.fills.append(fill)
        order.update(status="filled", fill_id=fill_id)
        order["history"].append({"at_utc": at, "status": "filled", "reason_codes": reasons})
        self.pending.remove(order)
        if order["side"] == "buy":
            self.cash -= notional
            self.ledger.append(self._ledger_entry(at, "buy", -notional, fill_id))
            if fee:
                self.cash -= fee
                self.ledger.append(self._ledger_entry(at, "fee", -fee, fill_id))
            self.position = {"contract": "simulation_position", "version": "1.0", "simulated": True,
                             "symbol": order["symbol"], "status": "open", "quantity": quantity, "entry_fill_id": fill_id,
                             "exit_fill_id": None, "opened_at_utc": at, "closed_at_utc": None, "cost_basis": notional + fee,
                             "average_cost": None, "proceeds": None, "exit_fee": None, "realized_pnl": None,
                             "mark_price": None, "marked_at_utc": None, "market_value": None, "unrealized_pnl": None,
                             "bars_held": 0}
            self.positions.append(self.position)
        else:
            self.cash += notional
            self.ledger.append(self._ledger_entry(at, "sell", notional, fill_id))
            if fee:
                self.cash -= fee
                self.ledger.append(self._ledger_entry(at, "fee", -fee, fill_id))
            position = self.position
            position.update(status="closed", exit_fill_id=fill_id, closed_at_utc=at, proceeds=notional, exit_fee=fee,
                            realized_pnl=notional - fee - position["cost_basis"])
            self.position = None

    def _ledger_entry(self, at, kind, amount, fill_id):
        balance = self.cash
        return {"contract": "cash_ledger_entry", "version": "1.0", "simulated": True,
                "entry": len(getattr(self, "ledger", [])) + 1, "at_utc": at, "kind": kind, "amount": fmt(amount),
                "balance_after": fmt(balance), "fill_id": fill_id}


# ---------------------------------------------------------------- run
def run_simulation(dataset, policy, *, market_config, indicator_config, signal_config, kill_switch, created_at,
                   start=None, end=None, step_seconds=None):
    validate_policy(policy)
    if dataset["bar_count"] > policy["limits"]["max_bars"]:
        raise TradingError("sim_too_many_bars", "The dataset has more bars than limits.max_bars allows.")
    strategies = load_strategies(policy["entry"]["strategies"], signal_config)
    signal_settings = indicator_settings_for(dataset, strategies, indicator_config)
    start_at, end_at, step, steps = plan(dataset, start, end, step_seconds, market_config["replay"]["max_steps"])
    window = {"start_utc": _utc(start_at), "end_utc": _utc(end_at), "step_seconds": step, "steps": steps, "clock": "simulation"}
    policy_sha = sha256(policy)
    run_id = "srun-" + sha256({"dataset_id": dataset["dataset_id"], "bars": dataset["bars_sha256"], "policy": policy_sha,
                               "replay": window, "kill_switch": kill_switch[0]})[:24]
    consumer = SimulationConsumer(dataset, policy, strategies, signal_settings, indicator_config, signal_config,
                                  kill_switch, run_id, window["start_utc"])
    drive(dataset, [consumer], config=market_config, start=start, end=end, step_seconds=step_seconds, passthrough=PASSTHROUGH)

    for order in list(consumer.pending):
        at = consumer.last_bar["available_at_utc"] if consumer.last_bar else window["end_utc"]
        order["status"] = "pending_at_end_of_data"
        order["history"].append({"at_utc": max(at, order["not_before_utc"]), "status": "pending_at_end_of_data",
                                 "reason_codes": ["no_later_bar_to_fill"]})
    last = consumer.last_bar
    unrealized = Decimal(0)
    for position in consumer.positions:
        with localcontext() as context:
            _ctx(context)
            position["average_cost"] = fmt(position["cost_basis"] / position["quantity"])
        if position["status"] == "open":
            mark = parse(last["close"])
            position.update(mark_price=last["close"], marked_at_utc=last["available_at_utc"],
                            market_value=mark * position["quantity"],
                            unrealized_pnl=mark * position["quantity"] - position["cost_basis"])
            unrealized += position["unrealized_pnl"]
    realized = sum((p["realized_pnl"] for p in consumer.positions if p["realized_pnl"] is not None), Decimal(0))
    market_value = sum((p["market_value"] for p in consumer.positions if p["status"] == "open"), Decimal(0))
    positions = [{k: (fmt(v) if isinstance(v, Decimal) else v) for k, v in p.items()} for p in consumer.positions]
    statuses = [o["status"] for o in consumer.orders]
    summary = {
        "bars_processed": consumer.index + 1, "signals_seen": consumer.signals_seen, "orders_created": len(consumer.orders),
        "orders_rejected": statuses.count("rejected"), "orders_filled": statuses.count("filled"),
        "orders_pending_at_end": statuses.count("pending_at_end_of_data"), "fills": len(consumer.fills),
        "fees_total": fmt(sum((parse(f["fee"]) for f in consumer.fills), Decimal(0))),
        "realized_pnl": fmt(realized), "unrealized_pnl": fmt(unrealized), "ending_cash": fmt(consumer.cash),
        "ending_equity": fmt(consumer.cash + market_value),
        "open_position_quantity": sum(p["quantity"] for p in consumer.positions if p["status"] == "open"),
        "last_close": last["close"] if last else None, "last_bar_close_utc": last["available_at_utc"] if last else None,
    }
    if len(consumer.orders) > 2000:
        raise TradingError("sim_too_many_orders", "The simulation created more orders than a run may record.")
    body = {"account": {"simulation_account_id": "simacct-" + sha256({"run_id": run_id})[:24],
                        "initial_cash": policy["account"]["initial_cash"], "currency": "USD"},
            "dataset": provenance(dataset), "policy": policy, "policy_sha256": policy_sha,
            "strategies": [{"name": n, "config_sha256": c} for n, _, c in strategies], "replay": window,
            "kill_switch": {"engaged": kill_switch[0], "source": kill_switch[1]},
            "orders": consumer.orders, "fills": consumer.fills, "cash_ledger": consumer.ledger, "positions": positions,
            "summary": summary}
    run = {"contract": "simulation_run", "version": "1.0", "run_id": run_id, "simulated": True, "mode": "offline_simulation",
           "paper_account_access": False, "broker": None, **body, "results_sha256": sha256(body), "created_at": created_at,
           "notice": NOTICE}
    validate_run(run)
    return run


def _utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- validation
def validate_run(run):
    """Schema, hash, and a full recomputation of ledger, fills, positions and summary."""
    validate_schema("simulation_run", run)
    body = {k: run[k] for k in ("account", "dataset", "policy", "policy_sha256", "strategies", "replay", "kill_switch",
                                "orders", "fills", "cash_ledger", "positions", "summary")}
    if sha256(body) != run["results_sha256"] or sha256(run["policy"]) != run["policy_sha256"]:
        raise TradingError("invalid_simulation_run", "Simulation run hash mismatch.")

    def bad(reason):
        raise TradingError("invalid_simulation_run", f"Simulation run is inconsistent: {reason}.")
    orders = {o["order_id"]: o for o in run["orders"]}
    if len(orders) != len(run["orders"]):
        bad("duplicate order IDs")
    fills = {f["fill_id"]: f for f in run["fills"]}
    if len(fills) != len(run["fills"]) or len({f["order_id"] for f in run["fills"]}) != len(run["fills"]):
        bad("an order was filled more than once")
    for order in run["orders"]:
        if (order["status"] == "filled") != (order["fill_id"] is not None) or order["run_id"] != run["run_id"]:
            bad("order fill linkage")
        if order["fill_id"] is not None and fills.get(order["fill_id"], {}).get("order_id") != order["order_id"]:
            bad("order fill linkage")
    costs = run["policy"]["costs"]
    for fill in run["fills"]:
        order = orders.get(fill["order_id"])
        if order is None or order["status"] != "filled" or fill["quantity"] != order["quantity"]:
            bad("fill without filled order")
        if fill["bar_open_utc"] < order["not_before_utc"]:
            bad("fill before the order could execute")
        price = fill_price(parse(fill["open_price"]), fill["side"], parse(costs["slippage_bps"]))
        notional = price * fill["quantity"]
        fee = fee_for(notional, costs)
        change = (notional - fee) if fill["side"] == "sell" else -(notional + fee)
        if (fmt(price), fmt(notional), fmt(fee), fmt(change)) != (fill["fill_price"], fill["notional"], fill["fee"], fill["cash_change"]):
            bad("fill arithmetic")
    balance = Decimal(0)
    for number, entry in enumerate(run["cash_ledger"], start=1):
        if entry["entry"] != number or (number == 1) != (entry["kind"] == "initial_cash"):
            bad("ledger order")
        balance += parse(entry["amount"], "signed_decimal")
        if fmt(balance) != entry["balance_after"]:
            bad("ledger balance")
    if fmt(balance) != run["summary"]["ending_cash"] or run["cash_ledger"][0]["amount"] != fmt(parse(run["account"]["initial_cash"], "money")):
        bad("ending cash")
    realized = Decimal(0)
    for position in run["positions"]:
        entry = fills.get(position["entry_fill_id"])
        if entry is None or fmt(parse(entry["notional"]) + parse(entry["fee"])) != position["cost_basis"]:
            bad("position cost basis")
        if position["status"] == "closed":
            exit_fill = fills.get(position["exit_fill_id"])
            pnl = parse(exit_fill["notional"]) - parse(exit_fill["fee"]) - parse(position["cost_basis"])
            if exit_fill is None or fmt(pnl) != position["realized_pnl"]:
                bad("realized P&L")
            realized += pnl
    if fmt(realized) != run["summary"]["realized_pnl"]:
        bad("summary realized P&L")
    if run["summary"]["fills"] != len(run["fills"]) or run["summary"]["orders_created"] != len(run["orders"]):
        bad("summary counts")
