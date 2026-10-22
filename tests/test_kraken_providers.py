"""Kraken public market data and read-only account connection. No network: every request is a fake."""

import base64
import hashlib
import hmac
import json
import unittest
from urllib.parse import parse_qs

from vicekrack.trading.errors import TradingError
from vicekrack.trading.providers import kraken, kraken_account
from vicekrack.trading.providers.kraken import KrakenPublicOHLCAdapter, interval_minutes, parse_ohlc
from vicekrack.trading.providers.kraken_account import READ_ENDPOINTS, KrakenReadOnlyClient, sign

NOW = 1_800_000_000  # a fixed clock, so "closed bar" checks are repeatable


def bar(opened, close="101.5", volume="12.25"):
    return [opened, "100.0", "102.0", "99.5", close, "100.8", volume, 42]


class FakeTransport:
    def __init__(self, status=200, body=None, raise_error=None):
        self.status, self.body, self.raise_error = status, body, raise_error
        self.calls = []

    def __call__(self, url, headers, *, timeout, max_bytes, method="GET", body=None):
        self.calls.append({"url": url, "headers": headers, "method": method, "body": body})
        if self.raise_error:
            raise self.raise_error
        return self.status, {}, json.dumps(self.body).encode("utf-8")


class KrakenPublicTests(unittest.TestCase):
    def test_interval_labels(self):
        self.assertEqual(interval_minutes("1h"), 60)
        with self.assertRaises(TradingError):
            interval_minutes("2h")

    def test_open_bar_is_dropped_and_closed_bars_kept(self):
        payload = {"error": [], "result": {"XXBTZUSD": [bar(NOW - 7200), bar(NOW - 3600), bar(NOW - 60)],
                                            "last": 1}}
        rows = parse_ohlc(payload, 60, NOW)
        from datetime import datetime, timezone
        expected = [datetime.fromtimestamp(NOW - t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                    for t in (7200, 3600)]
        self.assertEqual([r["timestamp"] for r in rows], expected)  # the bar opened 60 seconds ago is still open

    def test_prices_stay_exact_text(self):
        rows = parse_ohlc({"error": [], "result": {"P": [bar(NOW - 7200, close="0.12345678")], "last": 1}}, 60, NOW)
        self.assertEqual(rows[0]["close"], "0.12345678")

    def test_more_than_eight_decimals_is_refused(self):
        with self.assertRaises(TradingError):
            parse_ohlc({"error": [], "result": {"P": [bar(NOW - 7200, close="1.123456789")], "last": 1}}, 60, NOW)

    def test_kraken_error_is_a_fixed_code(self):
        with self.assertRaises(TradingError) as caught:
            parse_ohlc({"error": ["EQuery:Unknown asset pair"], "result": {}}, 60, NOW)
        self.assertEqual(caught.exception.code, "kraken_error")
        self.assertNotIn("Unknown", str(caught.exception))

    def test_adapter_needs_explicit_opt_in(self):
        with self.assertRaises(TradingError) as caught:
            KrakenPublicOHLCAdapter(pair="XBTUSD", interval="1h", allow_network=False)
        self.assertEqual(caught.exception.code, "network_not_allowed")

    def test_adapter_reads_only_the_fixed_public_endpoint(self):
        transport = FakeTransport(body={"error": [], "result": {"XXBTZUSD": [bar(NOW - 7200)], "last": 1}})
        rows, source, defaults = KrakenPublicOHLCAdapter(pair="XBTUSD", interval="1h", allow_network=True,
                                                         transport=transport, clock=lambda: NOW).read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(source["adapter"], "kraken_public_ohlc")
        self.assertEqual(defaults["symbol"], "XBTUSD")
        call = transport.calls[0]
        self.assertTrue(call["url"].startswith("https://api.kraken.com/0/public/OHLC?"))
        self.assertNotIn("API-Key", call["headers"])  # public data sends no credentials

    def test_bad_pair_and_status_are_refused(self):
        with self.assertRaises(TradingError):
            KrakenPublicOHLCAdapter(pair="xbt/usd", interval="1h", allow_network=True)
        with self.assertRaises(TradingError) as caught:
            KrakenPublicOHLCAdapter(pair="XBTUSD", interval="1h", allow_network=True,
                                    transport=FakeTransport(status=503), clock=lambda: NOW).read()
        self.assertEqual(caught.exception.code, "provider_refused")

    def test_unreachable_is_a_fixed_code(self):
        with self.assertRaises(TradingError) as caught:
            KrakenPublicOHLCAdapter(pair="XBTUSD", interval="1h", allow_network=True,
                                    transport=FakeTransport(raise_error=OSError("secret detail")),
                                    clock=lambda: NOW).read()
        self.assertEqual(caught.exception.code, "provider_unreachable")
        self.assertNotIn("secret detail", str(caught.exception))


class KrakenAccountTests(unittest.TestCase):
    SECRET = base64.b64encode(b"not-a-real-secret-for-tests-only").decode("ascii")

    def test_signature_matches_the_documented_method(self):
        nonce, postdata, path = 1700000000000000, "nonce=1700000000000000&asset=XBT", "/0/private/Balance"
        expected = base64.b64encode(hmac.new(base64.b64decode(self.SECRET),
                                             path.encode() + hashlib.sha256((str(nonce) + postdata).encode()).digest(),
                                             hashlib.sha512).digest()).decode()
        self.assertEqual(sign(path, nonce, postdata, self.SECRET), expected)

    def test_invalid_secret_is_refused(self):
        with self.assertRaises(TradingError) as caught:
            sign("/0/private/Balance", 1, "nonce=1", "not base64!!")
        self.assertEqual(caught.exception.code, "kraken_secret_invalid")

    def test_no_order_or_withdrawal_endpoint_is_allowed(self):
        for name in ("AddOrder", "CancelOrder", "CancelAll", "EditOrder", "Withdraw", "WithdrawStatus", "Transfer"):
            self.assertNotIn(name, READ_ENDPOINTS)

    def test_client_refuses_non_read_endpoints_before_any_request(self):
        transport = FakeTransport(body={"error": [], "result": {}})
        client = KrakenReadOnlyClient(api_key="k", api_secret=self.SECRET, transport=transport, clock=lambda: NOW)
        with self.assertRaises(TradingError) as caught:
            client.query("AddOrder", {"pair": "XBTUSD"})
        self.assertEqual(caught.exception.code, "endpoint_not_allowed")
        self.assertEqual(transport.calls, [])

    def test_read_request_is_post_to_the_private_query_path(self):
        transport = FakeTransport(body={"error": [], "result": {"ZUSD": "10.0"}})
        client = KrakenReadOnlyClient(api_key="the-key", api_secret=self.SECRET, transport=transport, clock=lambda: NOW)
        self.assertEqual(client.balance(), {"ZUSD": "10.0"})
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.kraken.com/0/private/Balance")
        self.assertEqual(call["headers"]["API-Key"], "the-key")
        self.assertNotIn(self.SECRET, json.dumps(call["headers"]))
        self.assertIn("nonce", parse_qs(call["body"].decode()))

    def test_nonce_strictly_increases(self):
        transport = FakeTransport(body={"error": [], "result": {}})
        client = KrakenReadOnlyClient(api_key="k", api_secret=self.SECRET, transport=transport, clock=lambda: NOW)
        client.balance()
        client.balance()
        first, second = (int(parse_qs(c["body"].decode())["nonce"][0]) for c in transport.calls)
        self.assertGreater(second, first)

    def test_kraken_error_shows_only_a_fixed_message(self):
        transport = FakeTransport(body={"error": ["EAPI:Invalid key"], "result": {}})
        client = KrakenReadOnlyClient(api_key="k", api_secret=self.SECRET, transport=transport, clock=lambda: NOW)
        with self.assertRaises(TradingError) as caught:
            client.balance()
        self.assertEqual(caught.exception.code, "kraken_error")
        self.assertNotIn("Invalid key", str(caught.exception))

    def test_missing_credentials_are_reported_without_values(self):
        with self.assertRaises(TradingError) as caught:
            KrakenReadOnlyClient.from_environment(environ={})
        self.assertEqual(caught.exception.code, "kraken_not_configured")

    def test_secret_is_hidden_from_repr(self):
        client = KrakenReadOnlyClient(api_key="k", api_secret=self.SECRET, transport=FakeTransport(), clock=lambda: NOW)
        self.assertNotIn(self.SECRET, repr(client))
        self.assertNotIn("k'", repr(client))


if __name__ == "__main__":
    unittest.main()
