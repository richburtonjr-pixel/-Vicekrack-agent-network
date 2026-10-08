"""Step 41: Alpaca PAPER broker adapter. Every broker answer here comes from an in-memory FAKE Alpaca;
no request leaves this process, and no real or paper order is ever placed. The keys below are fake.
"""

import io
import json
import re
import shutil
import tempfile
import threading
import unittest
import uuid
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from vicekrack.trading.broker.cli import load_broker_config, main as broker_main
from vicekrack.trading.broker.client import AlpacaPaperClient, check_endpoint
from vicekrack.trading.broker.orders import PaperBroker, consent_phrase
from vicekrack.trading.broker.store import BrokerPaperStore
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.state import PaperAccount

NOW = "2026-10-08T14:00:00Z"                       # 10:00 New York, regular session
KEY, SECRET = "PKPAPERFAKEKEY01", "paperFakeSecret0123456789"
ACCOUNT_ID, ACCOUNT_NUMBER = "11111111-2222-3333-4444-555555555555", "PA3FAKE00001"
ENV = {"ALPACA_PAPER_API_KEY_ID": KEY, "ALPACA_PAPER_API_SECRET_KEY": SECRET}


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        from datetime import datetime, timedelta
        moment = datetime.strptime(self.now, "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=seconds)
        self.now = moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeAlpaca:
    """A tiny in-memory Alpaca paper API. `fail[(method, path)]` can be an HTTP status, an exception,
    or ('after', exception) to raise AFTER the request was processed (an ambiguous answer)."""

    def __init__(self, clock):
        self.clock = clock
        self.calls, self.fail = [], {}
        self.account = {"id": ACCOUNT_ID, "account_number": ACCOUNT_NUMBER, "status": "ACTIVE", "currency": "USD",
                        "buying_power": "5000.00", "cash": "5000.00", "equity": "5000.00", "trading_blocked": False,
                        "account_blocked": False, "trade_suspended_by_user": False}
        self.is_open, self.next_close = True, "2026-10-08T16:00:00-04:00"
        self.asset = {"id": "a1", "class": "us_equity", "exchange": "NASDAQ", "symbol": "AAPL", "status": "active",
                      "tradable": True, "fractionable": True}
        self.quote = {"t": None, "ap": 190.12, "as": 3, "bp": 190.08, "bs": 2, "ax": "V", "bx": "V", "c": ["R"], "z": "C"}
        self.quote_age = 5
        self.positions, self.orders = [], {}
        self.fill_on_submit = None

    # helpers -----------------------------------------------------------
    def stamp(self, seconds_ago=0):
        from datetime import datetime, timedelta
        moment = datetime.strptime(self.clock(), "%Y-%m-%dT%H:%M:%SZ") - timedelta(seconds=seconds_ago)
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"

    def order_json(self, order):
        return dict(order)

    def set_status(self, client_order_id, status, filled_qty=None, avg=None):
        order = next(o for o in self.orders.values() if o["client_order_id"] == client_order_id)
        order["status"] = status
        if filled_qty is not None:
            order["filled_qty"] = str(filled_qty)
            order["filled_avg_price"] = avg
        return order

    # the transport -------------------------------------------------------
    def __call__(self, url, headers, *, timeout, max_bytes, method="GET", body=None):
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        self.calls.append({"method": method, "url": url, "host": parts.netloc, "path": path, "headers": dict(headers)})
        key = (method, re.sub(r"/v2/orders/[0-9a-f-]+$", "/v2/orders/ID", path))
        rule = self.fail.get(key)
        after = None
        if isinstance(rule, tuple) and rule[0] == "after":
            after = rule[1]
        elif isinstance(rule, BaseException):
            raise rule
        elif isinstance(rule, int):
            return rule, {}, json.dumps({"code": 40310000, "message": "insufficient buying power " + ACCOUNT_NUMBER}).encode()
        if headers.get("APCA-API-KEY-ID") != KEY or headers.get("APCA-API-SECRET-KEY") != SECRET:
            return 401, {}, b'{"message":"unauthorized"}'
        status, payload = self.route(method, parts.netloc, path, query, body)
        if after is not None:
            raise after
        return status, {}, b"" if payload is None else json.dumps(payload).encode()

    def route(self, method, host, path, query, body):
        if host == "data.alpaca.markets":
            assert path == "/v2/stocks/AAPL/quotes/latest" and query == {"feed": ["iex"]}
            return 200, {"symbol": "AAPL", "quote": dict(self.quote, t=self.quote["t"] or self.stamp(self.quote_age))}
        assert host == "paper-api.alpaca.markets", host
        if path == "/v2/account":
            return 200, self.account
        if path == "/v2/clock":
            return 200, {"timestamp": self.stamp()[:19] + "-00:00", "is_open": self.is_open,
                         "next_open": "2026-10-09T09:30:00-04:00", "next_close": self.next_close}
        if path.startswith("/v2/assets/"):
            return (200, self.asset) if path.endswith("/" + self.asset["symbol"]) else (404, {"message": "not found"})
        if path == "/v2/positions":
            return 200, self.positions
        if path == "/v2/orders" and method == "GET":
            return 200, [o for o in self.orders.values() if o["status"] not in ("filled", "canceled", "expired", "rejected")]
        if path == "/v2/orders" and method == "POST":
            request = json.loads(body)
            if any(o["client_order_id"] == request["client_order_id"] for o in self.orders.values()):
                return 422, {"code": 40010001, "message": "client_order_id must be unique"}
            order = {"id": str(uuid.uuid4()), "client_order_id": request["client_order_id"], "symbol": request["symbol"],
                     "side": request["side"], "type": request["type"], "time_in_force": request["time_in_force"],
                     "qty": request["qty"], "filled_qty": "0", "filled_avg_price": None,
                     "limit_price": request["limit_price"], "extended_hours": request["extended_hours"],
                     "status": "accepted", "submitted_at": self.stamp(), "account_id": ACCOUNT_ID}
            if self.fill_on_submit:
                order.update(self.fill_on_submit)
            self.orders[order["id"]] = order
            return 200, order
        if path == "/v2/orders:by_client_order_id":
            wanted = query["client_order_id"][0]
            found = [o for o in self.orders.values() if o["client_order_id"] == wanted]
            return (200, found[0]) if found else (404, {"message": "order not found"})
        if method == "DELETE" and path.startswith("/v2/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            if order is None:
                return 404, None
            if order["status"] in ("filled", "canceled", "expired", "rejected"):
                return 422, {"code": 42210000, "message": "order is not cancelable"}
            order["status"] = "pending_cancel"
            return 204, None
        raise AssertionError(f"unexpected route {method} {path}")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = load_broker_config()
        self.clock = Clock()
        self.fake = FakeAlpaca(self.clock)

    def broker(self, config=None, environ=ENV, allow_network=True, transport=None):
        return PaperBroker(self.root, config=config or self.config, allow_network=allow_network,
                           transport=transport or self.fake, environ=environ, clock=self.clock)

    def prepare(self, broker=None, **overrides):
        settings = dict(symbol="AAPL", side="buy", qty="2", limit_price="190.10")
        settings.update(overrides)
        return (broker or self.broker()).prepare(**settings)

    def submit(self, intent, broker=None):
        return (broker or self.broker()).submit(intent["intent_id"], consent_phrase(intent["intent_id"]))

    def posts(self):
        return [c for c in self.fake.calls if c["method"] == "POST"]

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception

    def stored_bytes(self):
        return b"".join(p.read_bytes() for p in self.root.rglob("*") if p.is_file())

    def cli(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = broker_main(list(argv), root=self.root, transport=self.fake, environ=ENV, clock=self.clock)
        return code, output.getvalue(), json.loads(output.getvalue())


# ---------------------------------------------------------------- endpoints and credentials
class EndpointAndCredentialTests(Base):
    def test_only_fixed_paper_and_data_endpoints(self):
        for url in ("https://api.alpaca.markets/v2/orders", "http://paper-api.alpaca.markets/v2/orders",
                    "https://paper-api.alpaca.markets.evil.example/v2/orders", "https://evil.example/v2/orders",
                    "https://paper-api.alpaca.markets@evil.example/x"):
            with self.subTest(url=url):
                self.assertCode("endpoint_not_allowed", check_endpoint, url)
        broker = self.broker()
        self.prepare(broker)
        hosts = {c["host"] for c in self.fake.calls}
        self.assertEqual(hosts, {"paper-api.alpaca.markets", "data.alpaca.markets"})
        source = (ROOT / "vicekrack/trading/broker/client.py").read_text(encoding="utf-8")
        self.assertNotIn('"https://api.alpaca.markets', source)

    def test_endpoint_overrides_are_rejected(self):
        for name, value in (("APCA_API_BASE_URL", "https://api.alpaca.markets"),
                            ("ALPACA_PAPER_BASE_URL", "https://example.com")):
            with self.subTest(name=name):
                self.assertCode("endpoint_override_rejected", self.broker(environ=dict(ENV, **{name: value})).check)
        self.broker(environ=dict(ENV, APCA_API_BASE_URL="https://paper-api.alpaca.markets/")).check()
        self.assertNotIn("http", json.dumps(self.config))                     # the config has no endpoint setting

    def test_redirects_and_network_opt_in(self):
        self.assertCode("network_not_allowed", self.broker(allow_network=False).check)
        self.fake.fail[("GET", "/v2/account")] = 302
        self.assertCode("broker_malformed_response", self.broker().check)
        self.assertEqual(self.fake.calls[-1]["host"], "paper-api.alpaca.markets")

    def test_separate_paper_credentials(self):
        step40 = {"APCA_API_KEY_ID": KEY, "APCA_API_SECRET_KEY": SECRET}
        self.assertCode("broker_credentials_missing", self.broker(environ=step40).check)
        self.assertCode("broker_credentials_invalid",
                        self.broker(environ={**ENV, "ALPACA_PAPER_API_SECRET_KEY": "a b"}).check)
        self.assertEqual(self.fake.calls, [])
        self.assertCode("broker_auth_failed", self.broker(environ={**ENV, "ALPACA_PAPER_API_KEY_ID": "PKWRONG"}).check)

    def test_check_is_sanitized(self):
        check = self.broker().check()
        self.assertEqual((check["trading_allowed"], check["market_open"], check["endpoint"]),
                         (True, True, "https://paper-api.alpaca.markets"))
        self.assertTrue(check["account_fingerprint"].startswith("pacct-"))
        stored = self.stored_bytes()
        for secret in (KEY, SECRET, ACCOUNT_ID, ACCOUNT_NUMBER, "5000"):
            self.assertNotIn(secret.encode(), stored)


# ---------------------------------------------------------------- preparation
class PrepareTests(Base):
    def test_static_order_rules(self):
        cases = ((dict(qty="1.5"), "invalid_quantity"), (dict(qty="0"), "invalid_quantity"),
                 (dict(qty="101"), "invalid_quantity"), (dict(limit_price="190.123"), "invalid_limit_price"),
                 (dict(limit_price="1e2"), "invalid_limit_price"), (dict(limit_price="-1"), "invalid_limit_price"),
                 (dict(limit_price="0.5000"), "limit_price_too_low"), (dict(qty="6"), "order_notional_too_large"),
                 (dict(symbol="aapl"), "invalid_symbol"), (dict(side="short"), "invalid_side"),
                 (dict(time_in_force="gtc"), "invalid_time_in_force"))
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                self.assertCode(code, self.prepare, **overrides)
        self.assertEqual(self.fake.calls, [])

    def test_prepared_intent_is_a_concrete_immutable_proposal(self):
        intent = self.prepare()
        self.assertEqual(intent["order"], {"symbol": "AAPL", "side": "buy", "qty": 2, "type": "limit", "time_in_force": "day",
                                           "limit_price": "190.1", "extended_hours": False})
        self.assertEqual(intent["exposure"]["max_notional"], "380.2")
        self.assertEqual((intent["source"], intent["real_money"], intent["simulated"]),
                         ({"kind": "manual_command"}, False, False))
        self.assertTrue(all(c["passed"] for c in intent["preview_checks"]), intent["preview_checks"])
        self.assertEqual(self.posts(), [])                                  # preparing never sends an order
        path = self.root / f"runtime/trading/broker-paper/intents/{intent['intent_id']}/intent.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["order"]["qty"] = 3
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertCode("broker_record_corrupt", BrokerPaperStore(self.root).load_intent, intent["intent_id"])

    def test_preview_shows_problems_but_submission_decides(self):
        self.fake.is_open = False
        intent = self.prepare()
        self.assertIn({"check": "market_regular_session_open", "passed": False, "reason": "market_closed"},
                      intent["preview_checks"])


# ---------------------------------------------------------------- submission and gates
class SubmissionTests(Base):
    def test_consent_and_single_submission(self):
        intent = self.prepare()
        self.assertCode("consent_required", self.broker().submit, intent["intent_id"], "yes")
        self.assertCode("consent_required", self.broker().submit, intent["intent_id"], consent_phrase(intent["intent_id"], "cancel"))
        result = self.submit(intent)
        self.assertEqual((result["outcome"], result["state"]), ("accepted", "accepted"))
        sent = self.fake.orders[next(iter(self.fake.orders))]
        self.assertEqual((sent["client_order_id"], sent["qty"], sent["limit_price"], sent["type"], sent["extended_hours"]),
                         (intent["client_order_id"], "2", "190.1", "limit", False))
        self.assertCode("already_submitted", self.submit, intent)
        self.assertEqual(len(self.posts()), 1)

    def gate(self, change, reason, overrides=None, **prepare):
        intent = self.prepare(**prepare)
        change()
        result = self.submit(intent, self.broker(config=overrides))
        self.assertTrue(result["blocked"], result)
        self.assertIn(reason, result["reasons"])
        self.assertEqual(self.posts(), [])
        self.assertFalse((self.root / f"runtime/trading/broker-paper/intents/{intent['intent_id']}/submission.json").exists())
        return intent

    def test_stale_or_missing_market_inputs_block(self):
        def stale():
            self.fake.quote_age = 120
        self.gate(stale, "quote_stale")

    def test_quote_unavailable_blocks(self):
        def gone():
            self.fake.fail[("GET", "/v2/stocks/AAPL/quotes/latest")] = 403
        self.gate(gone, "quote_unavailable")

    def test_quote_zero_bid_blocks(self):
        def zero():
            self.fake.quote["bp"] = 0
        self.gate(zero, "quote_unusable")

    def test_market_closed_and_near_close_and_clock_skew(self):
        def closed():
            self.fake.is_open = False
        self.gate(closed, "market_closed")
        self.fake.is_open = True

        def near():
            self.fake.next_close = "2026-10-08T10:02:00-04:00"
        self.gate(near, "too_close_to_close", qty="1")
        self.fake.next_close = "2026-10-08T16:00:00-04:00"
        intent = self.prepare(qty="3")
        skewed = Clock("2026-10-08T14:01:00Z")
        broker = PaperBroker(self.root, config=self.config, allow_network=True, transport=self.fake, environ=ENV, clock=skewed)
        self.fake.clock = Clock(NOW)
        result = broker.submit(intent["intent_id"], consent_phrase(intent["intent_id"]))
        self.assertIn("clock_skew", result["reasons"])

    def test_risk_gates(self):
        def spread():
            self.fake.quote.update(ap=192.0, bp=188.0)
        self.gate(spread, "spread_too_wide")
        self.fake.quote.update(ap=190.12, bp=190.08)
        self.gate(lambda: None, "limit_outside_band", limit_price="191.50", qty="1")
        self.gate(lambda: self.fake.account.update(buying_power="100"), "insufficient_buying_power", qty="1")
        self.fake.account["buying_power"] = "5000.00"
        self.gate(lambda: self.fake.positions.append({"symbol": "AAPL", "side": "long", "qty": "9", "qty_available": "9",
                                                      "market_value": "1711.08"}), "position_limit_exceeded", qty="3")
        self.fake.positions.clear()
        self.gate(lambda: self.fake.positions.append({"symbol": "AAPL", "side": "short", "qty": "-1", "qty_available": "-1",
                                                      "market_value": "-190.1"}), "short_position_exists", qty="4")
        self.fake.positions.clear()
        self.gate(lambda: self.fake.asset.update(tradable=False), "asset_not_eligible", qty="5")
        self.fake.asset.update(tradable=True, exchange="OTC")
        intent = self.prepare(limit_price="190.11")
        self.assertIn("asset_not_eligible", self.submit(intent)["reasons"])
        self.fake.asset["exchange"] = "NASDAQ"
        self.gate(lambda: self.fake.account.update(trading_blocked=True), "account_not_tradable", limit_price="190.09")
        self.fake.account["trading_blocked"] = False

    def test_pending_buy_orders_reserve_position_capacity(self):
        for n in range(2):
            self.fake.orders[f"00000000-0000-0000-0000-00000000000{n}"] = {
                "id": f"00000000-0000-0000-0000-00000000000{n}", "client_order_id": f"other-{n}", "symbol": "AAPL",
                "side": "buy", "type": "limit", "time_in_force": "day", "qty": "4", "filled_qty": "0",
                "limit_price": "190", "status": "new", "extended_hours": False}
        result = self.submit(self.prepare(qty="5"))                      # 2 x 4 x 190 + 5 x 190.1 > 2000
        self.assertIn("position_limit_exceeded", result["reasons"])
        config = deepcopy(self.config)
        config["limits"]["max_open_orders"] = 2
        self.assertIn("too_many_open_orders", self.submit(self.prepare(qty="1"), self.broker(config=config))["reasons"])

    def test_sell_only_reduces_an_existing_long(self):
        self.assertIn("not_enough_long_shares", self.submit(self.prepare(side="sell", limit_price="190.08"))["reasons"])
        self.fake.positions.append({"symbol": "AAPL", "side": "long", "qty": "3", "qty_available": "3", "market_value": "570"})
        self.assertEqual(self.submit(self.prepare(side="sell", qty="3", limit_price="190.07"))["outcome"], "accepted")

    def test_kill_switch_daily_limit_and_expiry(self):
        intent = self.prepare()
        BrokerPaperStore(self.root).set_kill_switch(True, NOW)
        calls = len(self.fake.calls)
        result = self.submit(intent)
        self.assertEqual(result["reasons"], ["kill_switch_engaged"])
        self.assertEqual(len(self.fake.calls), calls)                       # blocked before any network call
        BrokerPaperStore(self.root).set_kill_switch(False, NOW)
        self.clock.advance(self.config["limits"]["intent_ttl_seconds"] + 1)
        self.fake.clock = self.clock
        self.assertIn("intent_expired", self.submit(intent)["reasons"])
        config = deepcopy(self.config)
        config["limits"]["max_submissions_per_day"] = 1
        self.assertEqual(self.submit(self.prepare(limit_price="190.11"), self.broker(config=config))["outcome"], "accepted")
        self.assertIn("daily_limit_reached", self.submit(self.prepare(limit_price="190.09"), self.broker(config=config))["reasons"])

    def test_account_must_be_the_same_paper_account(self):
        intent = self.prepare()
        self.fake.account["id"] = "99999999-2222-3333-4444-555555555555"
        self.assertIn("account_changed", self.submit(intent)["reasons"])

    def test_broker_rejection_is_definitive(self):
        intent = self.prepare()
        self.fake.fail[("POST", "/v2/orders")] = 403
        result = self.submit(intent)
        self.assertEqual((result["outcome"], result["http_status"], result["broker_code"]), ("rejected", 403, 40310000))
        self.assertEqual(self.broker().show(intent["intent_id"])["state"], "rejected")
        self.assertNotIn(ACCOUNT_NUMBER.encode(), self.stored_bytes())       # the error message was not kept


# ---------------------------------------------------------------- uncertainty, crash recovery and reconciliation
class Crash(BaseException):
    pass


class UncertaintyTests(Base):
    def test_timeout_after_the_broker_accepted_is_unknown_then_reconciled(self):
        from vicekrack.trading.errors import TradingError as TE
        intent = self.prepare()
        self.fake.fail[("POST", "/v2/orders")] = ("after", TE("provider_timeout", "timeout"))
        result = self.submit(intent)
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(self.broker().show(intent["intent_id"])["state"], "unknown")
        other = self.prepare(limit_price="190.11")
        self.assertIn("reconciliation_required", self.submit(other)["reasons"])     # no new exposure while unknown
        self.assertCode("reconciliation_required", self.broker().cancel, intent["intent_id"],
                        consent_phrase(intent["intent_id"], "cancel"))
        refreshed = self.broker().refresh(intent["intent_id"])
        self.assertEqual((refreshed["found"], refreshed["state"], refreshed["reconciled"]), (True, "accepted", True))
        self.assertEqual(len(self.posts()), 1)                                        # never resubmitted
        del self.fake.fail[("POST", "/v2/orders")]
        self.assertEqual(self.submit(other)["outcome"], "accepted")

    def test_server_error_is_unknown_and_absence_needs_settling(self):
        intent = self.prepare()
        self.fake.fail[("POST", "/v2/orders")] = 503
        self.assertEqual(self.submit(intent)["outcome"], "unknown")
        self.assertEqual(self.broker().refresh(intent["intent_id"])["state"], "unknown")   # too soon to conclude
        self.clock.advance(self.config["limits"]["not_found_settle_seconds"])
        self.fake.clock = self.clock
        refreshed = self.broker().refresh(intent["intent_id"])
        self.assertEqual((refreshed["state"], refreshed["reconciled"], refreshed["terminal"]), ("not_placed", True, True))
        self.assertCode("already_submitted", self.submit, intent)                         # prepare a new intent instead

    def test_crash_after_recording_the_attempt(self):
        intent = self.prepare()
        with patch.object(AlpacaPaperClient, "submit", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.submit(intent)
        folder = self.root / f"runtime/trading/broker-paper/intents/{intent['intent_id']}"
        self.assertTrue((folder / "submission.json").exists())
        self.assertFalse((folder / "outcome.json").exists())
        summary = self.broker().show(intent["intent_id"])
        self.assertEqual((summary["state"], summary["outcome"]), ("unknown", "no_answer_recorded"))
        self.assertCode("already_submitted", self.submit, intent)
        self.assertIn("reconciliation_required", self.submit(self.prepare(limit_price="190.11"))["reasons"])
        self.assertEqual(self.posts(), [])

    def test_mismatching_broker_order_blocks(self):
        intent = self.prepare()
        self.submit(intent)
        self.fake.set_status(intent["client_order_id"], "new")["qty"] = "20"
        self.assertEqual(self.broker().refresh(intent["intent_id"])["state"], "mismatch")
        self.assertIn("reconciliation_required", self.submit(self.prepare(limit_price="190.11"))["reasons"])

    def test_unknown_or_stale_broker_orders_block(self):
        self.fake.orders["00000000-0000-0000-0000-0000000000aa"] = {
            "id": "00000000-0000-0000-0000-0000000000aa", "client_order_id": "vk-" + "a" * 24, "symbol": "MSFT",
            "side": "buy", "type": "limit", "time_in_force": "day", "qty": "1", "filled_qty": "0", "limit_price": "10",
            "status": "new", "extended_hours": False}
        self.assertIn("unreconciled_broker_order", self.submit(self.prepare())["reasons"])
        del self.fake.orders["00000000-0000-0000-0000-0000000000aa"]
        first = self.prepare(limit_price="190.11")
        self.submit(first)
        self.fake.set_status(first["client_order_id"], "filled", 2, "190.1")          # filled on the broker side
        second = self.prepare(limit_price="190.09")
        self.assertIn("local_state_stale", self.submit(second)["reasons"])
        self.broker().refresh(first["intent_id"])
        self.assertEqual(self.submit(second)["outcome"], "accepted")

    def test_concurrent_and_duplicate_submission(self):
        intent = self.prepare()
        lock = BrokerPaperStore(self.root).lock()
        try:
            self.assertCode("broker_busy", self.submit, intent)
            self.assertCode("broker_busy", self.broker().refresh, intent["intent_id"])
        finally:
            lock.release()
        results = []

        def go():
            try:
                results.append(self.submit(intent)["outcome"])
            except TradingError as error:
                results.append(error.code)
        threads = [threading.Thread(target=go) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("accepted"), 1, results)
        self.assertTrue(set(results) <= {"accepted", "broker_busy", "already_submitted"}, results)
        self.assertEqual(len(self.posts()), 1)


# ---------------------------------------------------------------- order lifecycle and cancellation
class LifecycleTests(Base):
    def submitted(self):
        intent = self.prepare()
        self.submit(intent)
        return intent

    def test_partial_then_full_fill(self):
        intent = self.submitted()
        self.fake.set_status(intent["client_order_id"], "partially_filled", 1, "190.09")
        refreshed = self.broker().refresh(intent["intent_id"])
        self.assertEqual((refreshed["state"], refreshed["filled_qty"], refreshed["terminal"]), ("partially_filled", "1", False))
        self.fake.set_status(intent["client_order_id"], "filled", 2, "190.095")
        refreshed = self.broker().refresh(intent["intent_id"])
        self.assertEqual((refreshed["state"], refreshed["filled_qty"], refreshed["filled_avg_price"]), ("filled", "2", "190.095"))
        self.assertCode("order_already_final", self.broker().cancel, intent["intent_id"],
                        consent_phrase(intent["intent_id"], "cancel"))

    def test_expired_and_rejected_states(self):
        intent = self.submitted()
        self.fake.set_status(intent["client_order_id"], "expired")
        self.assertEqual(self.broker().refresh(intent["intent_id"])["state"], "expired")
        other = self.prepare(limit_price="190.11")
        self.submit(other)
        self.fake.set_status(other["client_order_id"], "rejected")
        self.assertEqual(self.broker().refresh(other["intent_id"])["state"], "rejected")

    def test_cancel_request_is_not_confirmation_and_fills_can_race(self):
        intent = self.submitted()
        self.assertCode("consent_required", self.broker().cancel, intent["intent_id"], "cancel")
        result = self.broker().cancel(intent["intent_id"], consent_phrase(intent["intent_id"], "cancel"))
        self.assertEqual((result["cancel_request"], result["confirmed_canceled"]), ("cancel_requested", False))
        self.assertEqual(self.broker().show(intent["intent_id"])["state"], "cancel_requested")
        self.fake.set_status(intent["client_order_id"], "filled", 2, "190.1")         # the fill won the race
        self.assertEqual(self.broker().refresh(intent["intent_id"])["state"], "filled")

    def test_cancel_with_partial_fill_and_not_cancelable(self):
        intent = self.submitted()
        self.fake.set_status(intent["client_order_id"], "partially_filled", 1, "190.1")
        self.broker().refresh(intent["intent_id"])
        self.broker().cancel(intent["intent_id"], consent_phrase(intent["intent_id"], "cancel"))
        self.fake.set_status(intent["client_order_id"], "canceled")
        refreshed = self.broker().refresh(intent["intent_id"])
        self.assertEqual((refreshed["state"], refreshed["filled_qty"]), ("canceled", "1"))
        other = self.prepare(limit_price="190.11")
        self.submit(other)
        self.broker().refresh(other["intent_id"])
        self.fake.set_status(other["client_order_id"], "filled", 2, "190.1")
        result = self.broker().cancel(other["intent_id"], consent_phrase(other["intent_id"], "cancel"))
        self.assertEqual(result["cancel_request"], "not_cancelable")

    def test_kill_switch_allows_status_and_cancel(self):
        intent = self.submitted()
        BrokerPaperStore(self.root).set_kill_switch(True, NOW)
        self.assertEqual(self.broker().refresh(intent["intent_id"])["state"], "accepted")
        self.assertEqual(self.broker().cancel(intent["intent_id"], consent_phrase(intent["intent_id"], "cancel"))
                         ["cancel_request"], "cancel_requested")

    def test_ambiguous_cancel(self):
        from vicekrack.trading.errors import TradingError as TE
        intent = self.submitted()
        self.fake.fail[("DELETE", "/v2/orders/ID")] = TE("provider_timeout", "timeout")
        result = self.broker().cancel(intent["intent_id"], consent_phrase(intent["intent_id"], "cancel"))
        self.assertEqual(result["cancel_request"], "unknown")
        self.assertEqual(self.broker().show(intent["intent_id"])["state"], "accepted")      # nothing assumed


# ---------------------------------------------------------------- CLI, events, isolation and redaction
class CliAndIsolationTests(Base):
    def test_cli_workflow(self):
        code, _, check = self.cli("broker-paper-check", "--allow-network")
        self.assertEqual((code, check["trading_allowed"]), (0, True))
        code, _, prepared = self.cli("broker-paper-prepare", "--symbol", "AAPL", "--side", "buy", "--qty", "1",
                                     "--limit-price", "190.10", "--allow-network")
        intent_id = prepared["intent_id"]
        self.assertIn(f"--consent paper-execute:{intent_id}", prepared["to_submit"])
        code, _, submitted = self.cli("broker-paper-submit", intent_id, "--consent", f"paper-execute:{intent_id}",
                                      "--allow-network")
        self.assertEqual((code, submitted["outcome"]), (0, "accepted"))
        code, _, status = self.cli("broker-paper-status", intent_id, "--allow-network")
        self.assertEqual(status["state"], "accepted")
        code, _, cancel = self.cli("broker-paper-cancel", intent_id, "--consent", f"paper-cancel:{intent_id}", "--allow-network")
        self.assertFalse(cancel["confirmed_canceled"])
        code, _, listing = self.cli("broker-paper-list")
        self.assertEqual(listing["intents"][0]["state"], "cancel_requested")
        code, _, shown = self.cli("broker-paper-inspect", intent_id)
        self.assertEqual([e["event_type"] for e in shown["events"]],
                         ["prepared", "submission_started", "submission_accepted", "status_observed", "cancel_requested"])
        self.assertTrue(self.cli("broker-paper-kill-switch", "engage")[2]["broker_paper_kill_switch_engaged"])
        self.assertEqual(self.cli("broker-paper-submit", intent_id, "--consent", "x", "--allow-network")[2]["error"]["code"],
                         "consent_required")
        self.assertEqual(self.cli("broker-paper-check")[2]["error"]["code"], "network_not_allowed")

    def test_credentials_and_account_identifiers_never_leak(self):
        from vicekrack.trading.errors import TradingError as TE
        self.fake.fail[("POST", "/v2/orders")] = ("after", TE("provider_timeout", "timeout " + SECRET))
        texts = []
        for argv in (("broker-paper-check", "--allow-network"),):
            texts.append(self.cli(*argv)[1])
        intent = self.prepare()
        texts.append(json.dumps(self.submit(intent)))
        texts.append(json.dumps(self.broker().refresh(intent["intent_id"])))
        stored = self.stored_bytes()
        for secret in (KEY, SECRET, ACCOUNT_ID, ACCOUNT_NUMBER):
            self.assertNotIn(secret.encode(), stored)
            for text in texts:
                self.assertNotIn(secret, text)
        self.assertNotIn(b"APCA-API", stored)
        self.assertNotIn(b"5000.00", stored)                                  # balances are never persisted

    def test_kept_apart_from_simulation_and_local_paper_accounts(self):
        PaperAccount("broker-guard", root=self.root, clock=lambda: NOW).initialize()
        state = self.root / "runtime/trading/accounts/acct-broker-guard/state.json"
        before = state.read_bytes()
        intent = self.prepare()
        self.submit(intent)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.root / "runtime/trading").iterdir()), ["accounts", "broker-paper"])
        for path in (ROOT / "vicekrack/trading/broker").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("from ..state", "from ..simulation", "from ..signals", "from ..agents", "from ..session",
                              "from ..market", "PaperAccount", "threading", "while True", "sleep(", "websocket", "wss://",
                              "api.alpaca.markets/v2"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    if forbidden == "api.alpaca.markets/v2" and path.name == "client.py":
                        continue
                    self.assertNotIn(forbidden, source)

    def test_trading_code_never_imports_the_broker(self):
        for path in sorted((ROOT / "vicekrack/trading").rglob("*.py")):
            if "broker" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from ..broker", source, path)
            self.assertNotIn("trading.broker", source, path)

    def test_env_template_names_paper_keys(self):
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        self.assertIn("ALPACA_PAPER_API_KEY_ID=", lines)
        self.assertIn("ALPACA_PAPER_API_SECRET_KEY=", lines)


# ---------------------------------------------------------------- Living HQ (read-only)
class HQBrokerTests(Base):
    def get(self, target, method="GET"):
        from vicekrack.hq import api
        status, _, body = api.respond(method, target, {"host": "127.0.0.1:8765"}, port=8765, root=self.root)
        return status, json.loads(body)

    def test_status_view_is_read_only_and_sanitized(self):
        status, empty = self.get("/api/broker-paper")
        self.assertEqual((status, empty["intents"], empty["simulated"], empty["real_money"]), (200, [], False, False))
        intent = self.prepare()
        self.submit(intent)
        self.fake.set_status(intent["client_order_id"], "partially_filled", 1, "190.09")
        self.broker().refresh(intent["intent_id"])
        unknown = self.prepare(limit_price="190.11")
        self.fake.fail[("POST", "/v2/orders")] = 503
        self.submit(unknown)
        calls = len(self.fake.calls)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        status, index = self.get("/api/broker-paper")
        self.assertEqual((status, index["unreconciled"], index["open_orders"]), (200, 1, 1))
        states = {r["intent_id"]: r["state"] for r in index["intents"]}
        self.assertEqual(states, {intent["intent_id"]: "partially_filled", unknown["intent_id"]: "unknown"})
        status, detail = self.get(f"/api/broker-paper/intent?id={intent['intent_id']}")
        self.assertEqual((status, detail["filled_qty"], detail["filled_avg_price"]), (200, "1", "190.09"))
        self.assertTrue(any("broker-paper-cancel" in c for c in detail["commands"]))
        self.assertEqual([e["event_type"] for e in detail["events"]],
                         ["prepared", "submission_started", "submission_accepted", "status_observed"])
        self.assertEqual(self.get(f"/api/broker-paper/intent?id={intent['intent_id']}", method="POST")[0], 405)
        self.assertEqual(self.get("/api/broker-paper/intent?id=../x")[1]["error"]["code"], "invalid_intent_id")
        self.assertEqual(self.get("/api/broker-paper/intent?id=bpi-" + "0" * 24)[0], 404)
        self.assertEqual(self.get("/api/broker-paper?x=1")[0], 400)
        self.assertEqual(len(self.fake.calls), calls)                       # the HQ never contacts the broker
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        text = json.dumps(index) + json.dumps(detail)
        for secret in (KEY, SECRET, ACCOUNT_ID, ACCOUNT_NUMBER, "5000.00"):
            self.assertNotIn(secret, text)

    def test_corrupt_records_are_reported_not_shown(self):
        intent = self.prepare()
        self.submit(intent)
        path = self.root / f"runtime/trading/broker-paper/intents/{intent['intent_id']}/outcome.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["state"] = "rejected"
        document["client_order_id"] = "vk-" + "0" * 24
        path.write_text(json.dumps(document), encoding="utf-8")
        index = self.get("/api/broker-paper")[1]
        self.assertEqual((index["intents"], index["unreadable"][0]["code"]), ([], "broker_record_corrupt"))
        self.assertEqual(self.get(f"/api/broker-paper/intent?id={intent['intent_id']}")[0], 409)


@unittest.skipUnless(__import__("os").environ.get("RUN_LOCAL_BROWSER_TESTS") == "1",
                     "set RUN_LOCAL_BROWSER_TESTS=1 (needs Playwright + Chromium)")
class BrokerBrowserTests(Base):
    def test_paper_broker_view_in_a_real_browser(self):
        from playwright.sync_api import sync_playwright
        from vicekrack.hq.server import HQServer
        intent = self.prepare()
        self.submit(intent)
        self.fake.set_status(intent["client_order_id"], "partially_filled", 1, "190.09")
        self.broker().refresh(intent["intent_id"])
        server = HQServer(0, self.root)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width, height in ((1600, 900), (390, 844)):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    problems, writes = [], []
                    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
                    page.on("pageerror", lambda e: problems.append(str(e)))
                    page.on("request", lambda r: writes.append(r.url) if r.method != "GET" else None)
                    page.goto(url)
                    page.wait_for_selector(".bot[data-bot='creator']")
                    page.click("#btn-play")
                    page.click(".view-btn[data-view='broker']")
                    page.wait_for_selector(f"#broker-body section:has-text('{intent['intent_id']}')")
                    self.assertIn("NOT SIMULATION", page.inner_text("#broker-badge"))
                    text = page.inner_text("#broker-body")
                    for phrase in ("partially filled", "190.09", "this page cannot run them", "Worst-case cost"):
                        self.assertIn(phrase, text)
                    buttons = [b.lower() for b in page.locator("#broker-view button").all_inner_texts()]
                    self.assertFalse([b for b in buttons if any(w in b for w in ("submit", "cancel", "buy", "sell", "prepare"))],
                                     buttons)
                    self.assertTrue(page.is_hidden("#dock"))
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    self.assertEqual((problems, writes), ([], []))
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
