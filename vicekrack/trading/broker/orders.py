"""Alpaca PAPER orders (Step 41): prepare -> explicit consent -> submit once -> refresh -> cancel.

Scope: long-only, whole-share LIMIT orders with time_in_force `day`, regular session only,
eligible active US stocks. `buy` opens or adds to a long position; `sell` only reduces an
existing long position (never short). Nothing here reads research signals, datasets or
simulations: every order starts as a person's explicit `broker-paper-prepare` command.

Exposure: a buy limit order can never pay more than its limit price per share, so its
worst-case cost is quantity x limit price (`max_notional`), the figure checked against
max_order_notional, max_position_notional (with the position's market value and every
open buy order's remaining quantity x its own limit) and the account's buying power.
Alpaca reduces buying power by open buy orders until they fill or are canceled, so pending
orders reserve buying power on the broker side as well.

Uncertainty: the client order ID (`vk-...`) is fixed when the intent is prepared, and
submission.json is written BEFORE the order request. A submission is attempted at most
once per intent. A timeout, dropped connection, 5xx or unreadable answer records the
outcome as `unknown`; only `broker-paper-status` (lookup by client order ID) may resolve
it. While any intent is unknown or does not match the broker, new submissions are blocked.
This gives at-most-once submission per intent and explicit reconciliation; it does not and
cannot promise exactly-once execution.

Cancellation: a 204 answer only means Alpaca accepted the cancel REQUEST. The order is
canceled when a later status refresh shows `canceled`; fills can still arrive first.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from ..contracts import sha256, utc_now, validate_schema
from ..errors import TradingError
from .client import PAPER_BASE, SYMBOL, Ambiguous, AlpacaPaperClient
from .store import BrokerPaperStore, check_id

NEW_YORK = ZoneInfo("America/New_York")
NOTICE = ("ALPACA PAPER ACCOUNT: simulated brokerage with no real money. Not the offline simulator, not a Step 24 local "
          "paper account. Orders are sent only by an explicit command with consent; status and fills come from the broker.")
EXPOSURE_NOTE = ("A buy limit order never pays more than its limit price per share, so its worst-case cost is quantity x "
                 "limit price. Alpaca reserves buying power for open buy orders until they fill or are canceled.")
BROKER_STATES = {"new": "accepted", "accepted": "accepted", "pending_new": "accepted", "accepted_for_bidding": "accepted",
                 "partially_filled": "partially_filled", "filled": "filled", "canceled": "canceled", "expired": "expired",
                 "rejected": "rejected", "pending_cancel": "cancel_pending", "done_for_day": "done_for_day"}
TERMINAL = {"filled", "canceled", "expired", "rejected", "not_placed"}
UNRECONCILED = {"unknown", "mismatch", "other_broker_state"}
OPEN_STATES = {"accepted", "partially_filled", "cancel_pending", "cancel_requested", "done_for_day"}
EXCLUDED_EXCHANGES = {"OTC", "CRYPTO", ""}


def _now(clock):
    return datetime.strptime(clock(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _time(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dec(text):
    return Decimal(str(text))


def _fmt(value):
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def consent_phrase(intent_id, action="execute"):
    return f"paper-{action}:{intent_id}"


# ---------------------------------------------------------------- static validation (no network)
def order_terms(symbol, side, qty, limit_price, time_in_force, config):
    limits = config["limits"]
    if not isinstance(symbol, str) or not SYMBOL.match(symbol):
        raise TradingError("invalid_symbol", "Use one US stock symbol in capitals, like AAPL.")
    if side not in ("buy", "sell"):
        raise TradingError("invalid_side", "Side must be buy (open or add to a long) or sell (reduce a long).")
    try:
        quantity = int(str(qty))
    except ValueError:
        raise TradingError("invalid_quantity", "Quantity must be a whole number of shares.") from None
    if str(qty) != str(quantity) or quantity < 1 or quantity > limits["max_quantity"]:
        raise TradingError("invalid_quantity", "Quantity must be a whole number of shares from 1 to max_quantity.")
    try:
        price = Decimal(str(limit_price))
    except (InvalidOperation, ValueError):
        raise TradingError("invalid_limit_price", "The limit price must be a plain decimal like 190.25.") from None
    if not price.is_finite() or price <= 0 or "e" in str(limit_price).lower():
        raise TradingError("invalid_limit_price", "The limit price must be a plain decimal greater than zero.")
    places = -price.normalize().as_tuple().exponent if price.normalize().as_tuple().exponent < 0 else 0
    if (price >= 1 and places > 2) or (price < 1 and places > 4):
        raise TradingError("invalid_limit_price", "Limit prices use at most 2 decimals at $1.00 or more (4 below $1.00).")
    if price < _dec(limits["min_limit_price"]):
        raise TradingError("limit_price_too_low", "The limit price is below min_limit_price.")
    if time_in_force not in config["allowed_time_in_force"]:
        raise TradingError("invalid_time_in_force", "Only time_in_force day (regular session) is allowed.")
    notional = price * quantity
    if notional > _dec(limits["max_order_notional"]):
        raise TradingError("order_notional_too_large", "Quantity x limit price exceeds max_order_notional.")
    return {"symbol": symbol, "side": side, "qty": quantity, "type": "limit", "time_in_force": time_in_force,
            "limit_price": _fmt(price), "extended_hours": False}, notional


def _intent_body(intent):
    return {k: v for k, v in intent.items() if k not in ("intent_id", "client_order_id", "consent_phrase")}


def validate_intent(intent):
    validate_schema("broker_paper_intent", intent)
    intent_id = "bpi-" + sha256(_intent_body(intent))[:24]
    order = intent["order"]
    expected_notional = _fmt(_dec(order["limit_price"]) * order["qty"])
    if (intent["intent_id"] != intent_id or intent["client_order_id"] != client_order_id(intent_id)
            or intent["consent_phrase"] != consent_phrase(intent_id) or intent["exposure"]["max_notional"] != expected_notional
            or _time(intent["expires_at"]) <= _time(intent["created_at"])):
        raise TradingError("broker_record_corrupt", "The prepared intent failed its integrity check.")
    return intent


def client_order_id(intent_id):
    return "vk-" + sha256({"broker_paper_intent": intent_id})[:24]


# ---------------------------------------------------------------- derived order state
def derive(intent, submission, outcome, observations, cancels, config, now):
    """The current local view of one intent, from broker-confirmed records only."""
    settle = config["limits"]["not_found_settle_seconds"]
    if submission is None:
        return "prepared_expired" if now > _time(intent["expires_at"]) else "prepared"
    last = observations[-1] if observations else None
    if last is not None and last["found"]:
        if not last["matches_intent"]:
            state = "mismatch"
        else:
            state = BROKER_STATES.get(last["order"]["status"], "other_broker_state")
    elif last is not None:                                   # the broker has no order with this client order ID
        if outcome is not None and outcome["state"] == "rejected":
            state = "rejected"
        elif (_time(last["observed_at"]) - _time(submission["started_at"])).total_seconds() >= settle:
            state = "not_placed"
        else:
            state = "unknown"
    elif outcome is None or outcome["state"] == "unknown":
        state = "unknown"
    else:
        state = "rejected" if outcome["state"] == "rejected" else "unknown"
    if state in OPEN_STATES and cancels:
        request = cancels[-1]
        if request["result"] == "cancel_requested" and (last is None or request["requested_at"] >= last["observed_at"]):
            state = "cancel_requested"
    return state


def summary(store, intent_id, config, now):
    intent, submission, outcome, observations, cancels, events = store.records(intent_id)
    state = derive(intent, submission, outcome, observations, cancels, config, now)
    found = [o for o in observations if o["found"]]
    order = found[-1]["order"] if found else None
    return {"intent_id": intent_id, "client_order_id": intent["client_order_id"], "created_at": intent["created_at"],
            "expires_at": intent["expires_at"], "order": intent["order"], "max_notional": intent["exposure"]["max_notional"],
            "state": state, "terminal": state in TERMINAL, "reconciled": state not in UNRECONCILED,
            "submitted_at": submission["started_at"] if submission else None,
            "outcome": outcome["state"] if outcome else (None if submission is None else "no_answer_recorded"),
            "broker_order_id": order["broker_order_id"] if order else None,
            "broker_status": order["status"] if order else None, "filled_qty": order["filled_qty"] if order else "0",
            "filled_avg_price": order["filled_avg_price"] if order else None,
            "last_observed_at": observations[-1]["observed_at"] if observations else None,
            "observations": len(observations), "cancel_requests": len(cancels), "events": events}


# ---------------------------------------------------------------- the broker workflow
class PaperBroker:
    def __init__(self, root=None, *, config, allow_network=False, transport=None, environ=None, clock=utc_now):
        self.root, self.config, self.clock = root, config, clock
        self.store = BrokerPaperStore(root)
        self.allow_network, self.transport, self.environ = allow_network, transport, environ

    def client(self):
        extra = {"transport": self.transport} if self.transport is not None else {}
        return AlpacaPaperClient(config=self.config, allow_network=self.allow_network, environ=self.environ, **extra)

    def _event(self, intent_id, event_type, reason_codes=(), **details):
        clean = {k: v for k, v in details.items() if v is not None}
        self.store.append(intent_id, "events", {"contract": "broker_paper_event", "version": "1.0", "intent_id": intent_id,
                                                "event_type": event_type, "recorded_at": self.clock(),
                                                "reason_codes": list(reason_codes)[:10], "details": clean})

    # ------------------------------------------------------------------ connect / check
    def check(self):
        client = self.client()
        account = client.account()
        clock = client.clock()
        body = {"contract": "broker_paper_check", "version": "1.0", "mode": "alpaca_paper", "endpoint": PAPER_BASE,
                "checked_at": self.clock(), "account_fingerprint": account["fingerprint"],
                "account_status": account["status"], "currency": account["currency"],
                "trading_blocked": account["trading_blocked"], "account_blocked": account["account_blocked"],
                "trade_suspended_by_user": account["trade_suspended_by_user"],
                "trading_allowed": self._account_ok(account), "market_open": clock["is_open"],
                "clock_time_utc": clock["timestamp_utc"], "next_open_utc": clock["next_open_utc"],
                "next_close_utc": clock["next_close_utc"], "credentials_recorded": False}
        check = {"check_id": check_id(body), **body}
        self.store.save_check(check)
        return check

    @staticmethod
    def _account_ok(account):
        return (account["status"] == "ACTIVE" and account["currency"] == "USD" and not account["trading_blocked"]
                and not account["account_blocked"] and not account["trade_suspended_by_user"])

    # ------------------------------------------------------------------ prepare (saves an immutable intent)
    def prepare(self, *, symbol, side, qty, limit_price, time_in_force="day"):
        order, notional = order_terms(symbol, side, qty, limit_price, time_in_force, self.config)
        client = self.client()
        account, clock = client.account(), client.clock()
        asset = client.asset(symbol)
        quote = None
        reasons = []
        try:
            quote = client.latest_quote(symbol)
        except TradingError as error:
            reasons.append(error.code)
        preview = self._market_checks(order, notional, account, clock, asset, quote, now=_now(self.clock))
        created = _now(self.clock)
        body = {"contract": "broker_paper_intent", "version": "1.0", "mode": "alpaca_paper", "broker_paper": True,
                "simulated": False, "real_money": False, "source": {"kind": "manual_command"},
                "account_fingerprint": account["fingerprint"], "created_at": _stamp(created),
                "expires_at": _stamp(created + timedelta(seconds=self.config["limits"]["intent_ttl_seconds"])),
                "order": order, "exposure": {"max_notional": _fmt(notional), "explanation": EXPOSURE_NOTE},
                "inputs_at_preparation": {"quote": quote, "market_open": clock["is_open"],
                                          "clock_time_utc": clock["timestamp_utc"],
                                          "asset": None if asset is None else {k: asset[k] for k in
                                                                               ("symbol", "class", "exchange", "status", "tradable")}},
                "preview_checks": preview + [{"check": "quote_available", "passed": not reasons,
                                              "reason": reasons[0] if reasons else None}],
                "config_sha256": sha256(self.config), "notice": NOTICE}
        intent_id = "bpi-" + sha256(body)[:24]
        intent = {"intent_id": intent_id, **body, "client_order_id": client_order_id(intent_id),
                  "consent_phrase": consent_phrase(intent_id)}
        validate_intent(intent)
        self.store.save_intent(intent)
        self._event(intent_id, "prepared", [c["reason"] for c in intent["preview_checks"] if not c["passed"]],
                    state="prepared")
        return intent

    # ------------------------------------------------------------------ gates (all must pass)
    def _market_checks(self, order, notional, account, clock, asset, quote, now, open_orders=None, positions=None):
        limits = self.config["limits"]
        checks = []

        def gate(name, passed, reason):
            checks.append({"check": name, "passed": bool(passed), "reason": None if passed else reason})
        gate("account_paper_active", self._account_ok(account), "account_not_tradable")
        clock_time = _time(clock["timestamp_utc"])
        gate("market_regular_session_open", clock["is_open"], "market_closed")
        gate("clock_in_sync", abs((now - clock_time).total_seconds()) <= limits["max_clock_skew_seconds"], "clock_skew")
        gate("not_too_close_to_close",
             (_time(clock["next_close_utc"]) - clock_time).total_seconds() >= limits["min_seconds_before_close"],
             "too_close_to_close")
        eligible = (asset is not None and asset["symbol"] == order["symbol"] and asset["class"] == "us_equity"
                    and asset["status"] == "active" and asset["tradable"] and asset["exchange"] not in EXCLUDED_EXCHANGES)
        gate("asset_eligible", eligible, "asset_not_eligible")
        if quote is None:
            gate("quote_fresh", False, "quote_unavailable")
            gate("limit_within_band", False, "quote_unavailable")
        else:
            bid, ask = _dec(quote["bid"]), _dec(quote["ask"])
            age = (clock_time - _time(quote["quote_time_utc"])).total_seconds()
            usable = bid > 0 and ask > 0 and ask >= bid
            gate("quote_fresh", usable and -limits["max_clock_skew_seconds"] <= age <= limits["max_quote_age_seconds"],
                 "quote_stale" if usable else "quote_unusable")
            spread_ok = usable and (ask - bid) / ask * 10000 <= _dec(limits["max_spread_bps"])
            gate("spread_within_limit", spread_ok, "spread_too_wide")
            price = _dec(order["limit_price"])
            above, below = _dec(limits["max_limit_above_ask_bps"]) / 10000, _dec(limits["max_limit_below_bid_bps"]) / 10000
            if order["side"] == "buy":
                band = usable and bid * (1 - below) <= price <= ask * (1 + above)
            else:
                band = usable and bid * (1 - above) <= price <= ask * (1 + below)
            gate("limit_within_band", band, "limit_outside_band")
        if positions is not None and open_orders is not None:
            mine = [p for p in positions if p["symbol"] == order["symbol"]]
            gate("long_only", not any(p["side"] == "short" or p["qty"] < 0 for p in mine), "short_position_exists")
            gate("open_orders_within_limit", len(open_orders) < limits["max_open_orders"], "too_many_open_orders")
            if order["side"] == "buy":
                reserved = sum((_dec(o["limit_price"] or 0) * (_dec(o["qty"] or 0) - _dec(o["filled_qty"]))
                                for o in open_orders if o["symbol"] == order["symbol"] and o["side"] == "buy"), Decimal(0))
                value = sum((p["market_value"] for p in mine), Decimal(0))
                gate("position_within_limit", value + reserved + notional <= _dec(limits["max_position_notional"]),
                     "position_limit_exceeded")
                gate("buying_power_sufficient", account["buying_power"] >= notional, "insufficient_buying_power")
            else:
                available = sum((p["qty_available"] for p in mine if p["side"] == "long"), Decimal(0))
                gate("sell_reduces_long_only", available >= order["qty"], "not_enough_long_shares")
        return checks

    def _local_gates(self, intent, now):
        """Local reconciliation, daily cap and kill switch (no network)."""
        checks = []
        engaged, source = self.store.kill_switch(self.config)
        checks.append({"check": "kill_switch_released", "passed": not engaged, "reason": None if not engaged else "kill_switch_engaged"})
        checks.append({"check": "intent_not_expired", "passed": now <= _time(intent["expires_at"]), "reason": "intent_expired"})
        unreconciled, today, local_open = [], 0, {}
        day = now.astimezone(NEW_YORK).date()
        for other in self.store.intent_ids():
            item = summary(self.store, other, self.config, now)
            if item["submitted_at"] and _time(item["submitted_at"]).astimezone(NEW_YORK).date() == day:
                today += 1
            if item["submitted_at"] and not item["reconciled"]:
                unreconciled.append(other)
            if item["state"] in OPEN_STATES:
                local_open[item["client_order_id"]] = item
        checks.append({"check": "local_state_reconciled", "passed": not unreconciled, "reason": "reconciliation_required"})
        checks.append({"check": "daily_submission_limit", "passed": today < self.config["limits"]["max_submissions_per_day"],
                       "reason": "daily_limit_reached"})
        for check in checks:
            if check["passed"]:
                check["reason"] = None
        return checks, local_open

    def submit(self, intent_id, consent):
        lock = self.store.lock()
        try:
            intent = self.store.load_intent(intent_id)
            if consent != intent["consent_phrase"]:
                raise TradingError("consent_required", f"Submission needs explicit consent: --consent {intent['consent_phrase']}")
            if (self.store.intent_folder(intent_id) / "submission.json").exists():
                raise TradingError("already_submitted", "This intent was already submitted once; it is never sent again. "
                                                        "Refresh its status instead.")
            now = _now(self.clock)
            checks, local_open = self._local_gates(intent, now)
            client = self.client()
            if all(c["passed"] for c in checks):
                order = intent["order"]
                account, clock = client.account(), client.clock()
                asset = client.asset(order["symbol"])
                try:
                    quote = client.latest_quote(order["symbol"])
                except TradingError as error:
                    if error.code not in ("quote_unavailable",):
                        raise
                    quote = None
                positions, open_orders = client.positions(), client.open_orders()
                checks += [{"check": "same_paper_account", "passed": account["fingerprint"] == intent["account_fingerprint"],
                            "reason": None if account["fingerprint"] == intent["account_fingerprint"] else "account_changed"}]
                checks += self._market_checks(order, _dec(intent["exposure"]["max_notional"]), account, clock, asset, quote,
                                              now=_now(self.clock), open_orders=open_orders, positions=positions)
                ours = {o["client_order_id"] for o in open_orders if o["client_order_id"].startswith("vk-")}
                stray = ours - set(local_open)
                missing = set(local_open) - ours
                checks.append({"check": "broker_orders_known_locally", "passed": not stray,
                               "reason": None if not stray else "unreconciled_broker_order"})
                checks.append({"check": "local_open_orders_current", "passed": not missing,
                               "reason": None if not missing else "local_state_stale"})
            failed = [c["reason"] for c in checks if not c["passed"]]
            if failed:
                self._event(intent_id, "submission_blocked", failed, state="prepared")
                return {"submitted": False, "blocked": True, "reasons": failed, "checks": checks, "intent_id": intent_id}
            submission = {"contract": "broker_paper_submission", "version": "1.0", "intent_id": intent_id,
                          "client_order_id": intent["client_order_id"], "started_at": self.clock(), "checks": checks,
                          "endpoint": PAPER_BASE}
            self.store.begin_submission(intent_id, submission)        # BEFORE the request: an attempt began
            self._event(intent_id, "submission_started", [], state="unknown")
            payload = {"symbol": intent["order"]["symbol"], "qty": str(intent["order"]["qty"]), "side": intent["order"]["side"],
                       "type": "limit", "time_in_force": intent["order"]["time_in_force"],
                       "limit_price": intent["order"]["limit_price"], "extended_hours": False,
                       "client_order_id": intent["client_order_id"]}
            try:
                result, data = client.submit(payload)
            except Ambiguous as error:
                self._outcome(intent, "unknown", reason=error.code)
                self._event(intent_id, "submission_unknown", [error.code], state="unknown")
                return {"submitted": True, "outcome": "unknown", "reason": error.code, "intent_id": intent_id,
                        "next": f"python -m vicekrack broker-paper-status {intent_id} --allow-network"}
            if result == "rejected":
                self._outcome(intent, "rejected", http_status=data["http_status"], broker_code=data["broker_code"])
                self._event(intent_id, "submission_rejected", [f"http_{data['http_status']}"], state="rejected")
                return {"submitted": True, "outcome": "rejected", "http_status": data["http_status"],
                        "broker_code": data["broker_code"], "intent_id": intent_id}
            self._outcome(intent, "accepted", broker_order_id=data["broker_order_id"], http_status=200)
            observation = self._observe(intent, data, "submit_response")
            state = derive(intent, submission, {"state": "accepted"}, [observation], [], self.config, _now(self.clock))
            self._event(intent_id, "submission_accepted", [], state=state, broker_status=data["status"],
                        filled_qty=data["filled_qty"])
            return {"submitted": True, "outcome": "accepted", "state": state, "broker_status": data["status"],
                    "intent_id": intent_id}
        finally:
            lock.release()

    def _outcome(self, intent, state, reason=None, http_status=None, broker_code=None, broker_order_id=None):
        self.store.save_outcome(intent["intent_id"], {
            "contract": "broker_paper_outcome", "version": "1.0", "intent_id": intent["intent_id"],
            "client_order_id": intent["client_order_id"], "state": state, "recorded_at": self.clock(),
            "reason": reason, "http_status": http_status, "broker_code": broker_code, "broker_order_id": broker_order_id})

    def _observe(self, intent, order, source):
        matches = None
        if order is not None:
            expected = intent["order"]
            matches = (order["client_order_id"] == intent["client_order_id"] and order["symbol"] == expected["symbol"]
                       and order["side"] == expected["side"] and order["type"] == "limit"
                       and order["time_in_force"] == expected["time_in_force"] and order["qty"] == str(expected["qty"])
                       and order["limit_price"] == expected["limit_price"] and not order["extended_hours"])
        return self.store.append(intent["intent_id"], "observations", {
            "contract": "broker_paper_observation", "version": "1.0", "intent_id": intent["intent_id"],
            "observed_at": self.clock(), "source": source, "found": order is not None,
            "matches_intent": matches, "order": order})

    # ------------------------------------------------------------------ refresh / reconcile (allowed with the kill switch)
    def refresh(self, intent_id):
        lock = self.store.lock()
        try:
            intent, submission, outcome, observations, cancels, _ = self.store.records(intent_id)
            if submission is None:
                raise TradingError("not_submitted", "This intent was never submitted; there is no order to check.")
            order = self.client().order_by_client_id(intent["client_order_id"])
            observation = self._observe(intent, order, "status_lookup")
            state = derive(intent, submission, outcome, observations + [observation], cancels, self.config, _now(self.clock))
            self._event(intent_id, "status_observed" if order else "reconciliation_not_found",
                        [] if order else ["no_order_with_client_order_id"], state=state,
                        broker_status=order["status"] if order else None, filled_qty=order["filled_qty"] if order else None)
            return {"intent_id": intent_id, "found": order is not None, "state": state,
                    "broker_status": order["status"] if order else None,
                    "filled_qty": order["filled_qty"] if order else "0",
                    "filled_avg_price": order["filled_avg_price"] if order else None,
                    "terminal": state in TERMINAL, "reconciled": state not in UNRECONCILED}
        finally:
            lock.release()

    # ------------------------------------------------------------------ cancel request (allowed with the kill switch)
    def cancel(self, intent_id, consent):
        lock = self.store.lock()
        try:
            intent, submission, outcome, observations, cancels, _ = self.store.records(intent_id)
            if consent != consent_phrase(intent_id, "cancel"):
                raise TradingError("consent_required", f"Cancellation needs: --consent {consent_phrase(intent_id, 'cancel')}")
            state = derive(intent, submission, outcome, observations, cancels, self.config, _now(self.clock))
            found = [o for o in observations if o["found"]]
            if state in UNRECONCILED or not found:
                raise TradingError("reconciliation_required", "Refresh the status first; the broker order is not confirmed.")
            if state in TERMINAL:
                raise TradingError("order_already_final", f"The order is already {state}; nothing to cancel.")
            order_id = found[-1]["order"]["broker_order_id"]
            try:
                result = self.client().cancel(order_id)
            except Ambiguous as error:
                result = "unknown"
                reason = error.code
            else:
                reason = None
            self.store.append(intent_id, "cancels", {"contract": "broker_paper_cancel", "version": "1.0",
                                                     "intent_id": intent_id, "requested_at": self.clock(),
                                                     "broker_order_id": order_id, "result": result, "reason": reason})
            event = {"cancel_requested": "cancel_requested", "not_cancelable": "cancel_not_cancelable",
                     "not_found": "cancel_not_found", "unknown": "cancel_unknown"}[result]
            self._event(intent_id, event, [reason] if reason else [],
                        state="cancel_requested" if result == "cancel_requested" else state)
            return {"intent_id": intent_id, "cancel_request": result, "confirmed_canceled": False,
                    "note": "A cancel request is not a confirmed cancellation; fills may still arrive. Refresh the status.",
                    "next": f"python -m vicekrack broker-paper-status {intent_id} --allow-network"}
        finally:
            lock.release()

    # ------------------------------------------------------------------ offline views
    def list(self, limit=100):
        now = _now(self.clock)
        items = []
        for intent_id in self.store.intent_ids()[-limit:]:
            try:
                item = summary(self.store, intent_id, self.config, now)
                item.pop("events")
                items.append(item)
            except TradingError as error:
                items.append({"intent_id": intent_id, "readable": False, "code": error.code})
        return items

    def show(self, intent_id):
        return summary(self.store, intent_id, self.config, _now(self.clock))
