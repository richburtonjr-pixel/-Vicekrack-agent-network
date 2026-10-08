"""Step 40: opt-in Alpaca historical bars. Every provider response here is MOCKED: no request
leaves this process (the one test of the real HTTPS transport blocks sockets itself).
No real credentials are used; the fake ones below are not Alpaca keys.
"""

import io
import json
import re
import shutil
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from vicekrack.hq.sessions import session_document
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.market.bars import expand_bar
from vicekrack.trading.market.store import MarketStore, load_market_config
from vicekrack.trading.providers import alpaca
from vicekrack.trading.providers.alpaca import AlpacaHistoricalAdapter, load_provider_config, urllib_transport
from vicekrack.trading.providers.cli import main as fetch_main
from vicekrack.trading.session.runner import SessionRunner

NOW = "2026-10-08T12:00:00Z"
KEY, SECRET = "PKFAKEKEYID0001", "fakeSecretValue0123456789"
ENV = {"APCA_API_KEY_ID": KEY, "APCA_API_SECRET_KEY": SECRET}
OPEN = datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc)          # 09:30 New York (EDT)


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def bars(count, start=OPEN, minutes=5, base=100.0):
    out = []
    for i in range(count):
        price = round(base + (i % 7) * 0.25 - (i % 3) * 0.1, 2)
        out.append({"t": stamp(start + timedelta(minutes=minutes * i)), "o": price, "h": round(price + 0.5, 2),
                    "l": round(price - 0.5, 2), "c": round(price + 0.1, 2), "v": 1000 + i, "n": 10, "vw": price})
    return out


def page(items, token=None, symbol="AAPL", raw=None):
    if raw is not None:
        return raw
    return json.dumps({"bars": items, "symbol": symbol, "next_page_token": token}).encode()


class FakeAlpaca:
    """Scripted responses: each item is (status, headers, body) or an exception to raise."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers, *, timeout, max_bytes):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout, "max_bytes": max_bytes})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def ok(items, token=None, **kwargs):
    return 200, {}, page(items, token, **kwargs)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = load_provider_config()
        self.market_config, self.market_sha = load_market_config()

    def adapter(self, transport, **overrides):
        settings = dict(symbol="AAPL", interval="5m", start="2026-09-01", end="2026-09-02", feed="iex", adjustment="raw",
                        allow_network=True, config=self.config, transport=transport, clock=lambda: NOW, environ=ENV)
        settings.update(overrides)
        return AlpacaHistoricalAdapter(**settings)

    def fetch(self, transport, **overrides):
        return MarketStore(self.root, clock=lambda: NOW).import_dataset(
            self.adapter(transport, **overrides), config=self.market_config, config_sha256=self.market_sha, label="historical")

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception

    def stored(self):
        folder = self.root / "runtime/trading/market/datasets"
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def cli(self, *argv, transport=None, environ=ENV, clock=lambda: NOW):
        output = io.StringIO()
        with redirect_stdout(output):
            code = fetch_main(list(argv), root=self.root, transport=transport, clock=clock, environ=environ)
        return code, output.getvalue(), json.loads(output.getvalue())


ARGS = ("market-fetch", "--provider", "alpaca", "--symbol", "AAPL", "--interval", "5m", "--start", "2026-09-01",
        "--end", "2026-09-02", "--feed", "iex", "--adjustment", "raw")


class OptInAndAuthenticationTests(Base):
    def test_network_needs_explicit_opt_in(self):
        fake = FakeAlpaca()
        code, _, out = self.cli(*ARGS, transport=fake)
        self.assertEqual((code, out["error"]["code"]), (1, "network_not_allowed"))
        self.assertEqual(fake.calls, [])

    def test_credentials_only_from_environment(self):
        fake = FakeAlpaca(ok(bars(3)))
        for environ, code in (({}, "provider_credentials_missing"), ({"APCA_API_KEY_ID": KEY}, "provider_credentials_missing"),
                              ({"APCA_API_KEY_ID": "bad key", "APCA_API_SECRET_KEY": SECRET}, "provider_credentials_invalid"),
                              ({"APCA_API_KEY_ID": KEY, "APCA_API_SECRET_KEY": "x\r\nInjected: 1"}, "provider_credentials_invalid")):
            self.assertCode(code, self.adapter(fake, environ=environ).read)
        self.assertEqual(fake.calls, [])
        dataset = self.fetch(fake)
        sent = fake.calls[0]
        self.assertEqual((sent["headers"]["APCA-API-KEY-ID"], sent["headers"]["APCA-API-SECRET-KEY"]), (KEY, SECRET))
        self.assertTrue(sent["url"].startswith("https://data.alpaca.markets/v2/stocks/AAPL/bars?"))
        self.assertNotIn(KEY, sent["url"])
        self.assertEqual(dataset["source"]["provider"]["credentials_recorded"], False)

    def test_authentication_failure_is_sanitized(self):
        fake = FakeAlpaca((401, {"www-authenticate": KEY}, json.dumps({"message": "invalid key " + KEY}).encode()))
        error = self.assertCode("provider_auth_failed", self.fetch, fake)
        self.assertNotIn(KEY, error.message)
        self.assertNotIn(SECRET, error.message)
        self.assertEqual(self.stored(), [])

    def test_request_parameters_are_explicit_and_bounded(self):
        fake = FakeAlpaca(ok(bars(2, start=datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc), minutes=60)))
        self.fetch(fake, interval="1h", feed="sip", adjustment="split", start="2026-09-01", end="2026-09-01")
        url = fake.calls[0]["url"]
        for part in ("timeframe=1Hour", "feed=sip", "adjustment=split", "sort=asc", "limit=10000",
                     "start=2026-09-01T04%3A00%3A00Z", "end=2026-09-02T03%3A59%3A59Z"):
            self.assertIn(part, url)
        self.assertEqual(fake.calls[0]["timeout"], self.config["alpaca"]["limits"]["timeout_seconds"])

    def test_invalid_choices_are_refused_before_any_request(self):
        fake = FakeAlpaca()
        for overrides, code in (({"feed": "boats"}, "invalid_feed"), ({"feed": None}, "invalid_feed"),
                                ({"adjustment": "spin-off"}, "invalid_adjustment"), ({"symbol": "aapl"}, "invalid_symbol"),
                                ({"symbol": "AAPL/../X"}, "invalid_symbol"), ({"interval": "2m"}, "interval_not_allowed"),
                                ({"start": "2026-09-03"}, "invalid_date_range"), ({"start": "2015-12-31"}, "invalid_date_range"),
                                ({"end": "2026-10-08"}, "range_not_historical"),
                                ({"start": "2026-07-01", "end": "2026-09-01"}, "range_too_long"),
                                ({"start": "2026-02-30"}, "invalid_date_range")):
            with self.subTest(overrides=overrides):
                self.assertCode(code, self.adapter, fake, **overrides)
        self.assertEqual(fake.calls, [])


class PaginationAndLimitTests(Base):
    def test_pages_are_followed_and_hashed(self):
        items = bars(9)
        fake = FakeAlpaca(ok(items[:3], "tok1"), ok(items[3:6], "tok2"), ok(items[6:]))
        dataset = self.fetch(fake)
        self.assertEqual(dataset["bar_count"], 9)
        self.assertIn("page_token=tok1", fake.calls[1]["url"])
        self.assertIn("page_token=tok2", fake.calls[2]["url"])
        responses = dataset["source"]["provider"]["responses"]
        self.assertEqual([(r["page"], r["bars"]) for r in responses], [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(len({r["sha256"] for r in responses}), 3)

    def test_page_limit_loop_and_sizes_publish_nothing(self):
        config = deepcopy(self.config)
        config["alpaca"]["limits"]["max_pages"] = 2
        items = bars(6)
        cases = [
            ("provider_page_limit", dict(config=config), [ok(items[:2], "a"), ok(items[2:4], "b")]),
            ("provider_pagination_loop", {}, [ok(items[:2], "a"), ok(items[2:4], "a")]),
            ("provider_malformed_response", {}, [ok(items[:2], "bad token!")]),
        ]
        small = deepcopy(self.config)
        small["alpaca"]["limits"].update(max_response_bytes=1000, max_bars=4)
        total = deepcopy(self.config)
        total["alpaca"]["limits"].update(max_total_bytes=1500)
        many = bars(18)
        cases += [("provider_response_too_large", dict(config=small), [(200, {}, b"x" * 1001)]),
                  ("provider_response_too_large", dict(config=total), [ok(many[:6], "a"), ok(many[6:12], "b"),
                                                                       ok(many[12:], "c")]),
                  ("provider_too_many_bars", dict(config=small), [ok(items[:5])])]
        for code, overrides, responses in cases:
            with self.subTest(code=code):
                self.assertCode(code, self.fetch, FakeAlpaca(*responses), **overrides)
        self.assertEqual(self.stored(), [])

    def test_transport_receives_the_byte_cap(self):
        fake = FakeAlpaca(ok(bars(2)))
        self.fetch(fake)
        self.assertEqual(fake.calls[0]["max_bytes"], self.config["alpaca"]["limits"]["max_response_bytes"])

    def test_no_bars_is_an_error_not_an_empty_dataset(self):
        self.assertCode("provider_no_bars", self.fetch, FakeAlpaca(ok(None)))
        self.assertCode("provider_no_bars", self.fetch, FakeAlpaca(ok([])))
        self.assertEqual(self.stored(), [])


class BarContentTests(Base):
    def test_duplicate_and_out_of_order_bars(self):
        items = bars(4)
        self.assertCode("duplicate_bar", self.fetch, FakeAlpaca(ok(items[:2], "a"), ok([items[1]] + items[2:])))
        self.assertCode("bars_out_of_order", self.fetch, FakeAlpaca(ok([items[0], items[2], items[1]])))
        self.assertEqual(self.stored(), [])

    def test_unfinished_and_out_of_range_bars(self):
        times = iter([NOW, "2026-09-01T13:32:00Z"])               # the clock jumps back: bar 1 not closed yet
        self.assertCode("unfinished_bar", self.fetch, FakeAlpaca(ok(bars(2))), clock=lambda: next(times))
        late = bars(1, start=datetime(2026, 9, 3, 13, 30, tzinfo=timezone.utc))
        self.assertCode("provider_bar_outside_request", self.fetch, FakeAlpaca(ok(late)))
        early = bars(1, start=datetime(2026, 9, 1, 3, 55, tzinfo=timezone.utc))
        self.assertCode("provider_bar_outside_request", self.fetch, FakeAlpaca(ok(early)))
        self.assertEqual(self.stored(), [])

    def test_timezone_conversion_across_daylight_saving(self):
        # 1h bars across the 2026-11-01 New York change (EDT -> EST): 08:00 local before, 07:00 local after.
        times = ["2026-10-30T12:00:00Z", "2026-10-30T13:00:00Z", "2026-11-02T13:00:00Z", "2026-11-02T14:00:00Z"]
        items = [dict(b, t=t) for b, t in zip(bars(4), times)]
        later = lambda: "2026-11-10T12:00:00Z"
        dataset = self.fetch(FakeAlpaca(ok(items)), interval="1h", start="2026-10-30", end="2026-11-02", clock=later)
        local = [expand_bar(dataset, i)["timestamp"] for i in range(4)]
        self.assertEqual(local, ["2026-10-30T08:00:00-04:00", "2026-10-30T09:00:00-04:00",
                                 "2026-11-02T08:00:00-05:00", "2026-11-02T09:00:00-05:00"])
        daily = [dict(b, t=t) for b, t in zip(bars(2), ["2026-10-30T04:00:00Z", "2026-11-02T05:00:00Z"])]
        dataset = self.fetch(FakeAlpaca(ok(daily)), interval="1d", start="2026-10-30", end="2026-11-02", clock=later)
        self.assertEqual(expand_bar(dataset, 1)["timestamp"], "2026-11-02T00:00:00-05:00")
        self.assertEqual(dataset["gaps"]["missing_intervals"], 2)              # the weekend: reported, not filled
        misaligned = [dict(bars(1)[0], t="2026-10-30T00:00:00Z")]
        self.assertCode("interval_misaligned", self.fetch, FakeAlpaca(ok(misaligned)), interval="1d",
                        start="2026-10-29", end="2026-10-30", clock=later)
        sub_second = [dict(bars(1)[0], t="2026-09-01T13:30:00.5Z")]
        self.assertCode("provider_malformed_response", self.fetch, FakeAlpaca(ok(sub_second)))
        nanos = [dict(bars(1)[0], t="2026-09-01T13:30:00.000000000Z")]
        self.assertEqual(self.fetch(FakeAlpaca(ok(nanos)), adjustment="split")["bars"][0]["start_utc"], "2026-09-01T13:30:00Z")

    def test_prices_keep_exact_decimal_text(self):
        body = (b'{"bars":[{"t":"2026-09-01T13:30:00Z","o":187.1,"h":187.35,"l":0.1,"c":187.2,"v":12345678901,'
                b'"n":1,"vw":187.123456789}],"symbol":"AAPL","next_page_token":null}')
        dataset = self.fetch(FakeAlpaca((200, {}, body)))
        bar = dataset["bars"][0]
        self.assertEqual((bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]),
                         ("187.1", "187.35", "0.1", "187.2", "12345678901"))
        exponent = body.replace(b'"o":187.1', b'"o":1.8710E2')
        self.assertEqual(self.fetch(FakeAlpaca((200, {}, exponent)), adjustment="all")["bars"][0]["open"], "187.1")
        nine = body.replace(b'"l":0.1', b'"l":0.123456789')
        self.assertCode("provider_precision_exceeded", self.fetch, FakeAlpaca((200, {}, nine)), adjustment="dividend")
        for bad in (b'"l":0', b'"l":-1', b'"l":"abc"'):
            with self.subTest(bad=bad):
                with self.assertRaises(TradingError):
                    self.fetch(FakeAlpaca((200, {}, body.replace(b'"l":0.1', bad))), adjustment="split")

    def test_gaps_are_reported_never_filled(self):
        items = bars(6)
        del items[2:4]                                            # two missing 5-minute bars
        dataset = self.fetch(FakeAlpaca(ok(items)))
        self.assertEqual((dataset["bar_count"], dataset["gaps"]["gap_count"], dataset["gaps"]["missing_intervals"]), (4, 1, 2))
        coverage = dataset["source"]["provider"]["coverage"]
        self.assertEqual(coverage["uncovered_before_first_seconds"], 9 * 3600 + 30 * 60)   # midnight -> 09:30
        self.assertEqual(coverage["calendar"], "none")

    def test_malformed_responses(self):
        good = bars(1)[0]
        for raw in (b"not json", b"[]", page([good], symbol="MSFT"), page([dict(good, x=1)]),
                    page([{k: v for k, v in good.items() if k != "v"}]), page([dict(good, t="2026-09-01 13:30:00")]),
                    json.dumps({"bars": "x", "symbol": "AAPL", "next_page_token": None}).encode(), b""):
            with self.subTest(raw=raw[:40]):
                self.assertCode("provider_malformed_response", self.fetch, FakeAlpaca((200, {}, raw)))
        self.assertEqual(self.stored(), [])


class AdjustmentAndFeedTests(Base):
    def test_adjustment_and_feed_are_part_of_identity_and_never_mixed(self):
        items = bars(3)
        raw = self.fetch(FakeAlpaca(ok(items)))
        split = self.fetch(FakeAlpaca(ok(items)), adjustment="split")
        sip = self.fetch(FakeAlpaca(ok(items)), feed="sip")
        self.assertEqual(len({raw["dataset_id"], split["dataset_id"], sip["dataset_id"]}), 3)
        self.assertEqual([d["source"]["provider"]["adjustment"] for d in (raw, split, sip)], ["raw", "split", "raw"])
        self.assertEqual([d["source"]["name"] for d in (raw, split, sip)], ["alpaca-iex-raw", "alpaca-iex-split", "alpaca-sip-raw"])
        self.assertCode("dataset_exists", self.fetch, FakeAlpaca(ok(items)))      # same request and bytes: immutable
        listed = MarketStore(self.root).list_datasets()
        self.assertEqual(sorted((d["provider"]["feed"], d["provider"]["adjustment"]) for d in listed),
                         [("iex", "raw"), ("iex", "split"), ("sip", "raw")])

    def test_entitlement_failure_does_not_fall_back(self):
        fake = FakeAlpaca((403, {}, b'{"message":"subscription does not permit querying recent SIP data"}'),
                          ok(bars(2)))
        error = self.assertCode("provider_not_entitled", self.fetch, fake, feed="sip")
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("no other feed was tried", error.message)
        self.assertNotIn("subscription does not permit", error.message)


class RateLimitAndFailureTests(Base):
    def test_rate_limit_is_reported_and_not_retried(self):
        fake = FakeAlpaca((429, {"x-ratelimit-reset": "1791460800", "x-ratelimit-remaining": "0"}, b"{}"), ok(bars(2)))
        error = self.assertCode("provider_rate_limited", self.fetch, fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("resets at 2026-10-08T12:00:00Z", error.message)
        fake = FakeAlpaca((429, {"x-ratelimit-reset": "soon; " + SECRET}, b"{}"))
        error = self.assertCode("provider_rate_limited", self.fetch, fake)
        self.assertNotIn(SECRET, error.message)

    def test_server_errors_and_redirects(self):
        self.assertCode("provider_unavailable", self.fetch, FakeAlpaca((503, {}, b"down")))
        self.assertCode("provider_unexpected_status", self.fetch, FakeAlpaca((302, {"location": "https://evil.example"}, b"")))
        self.assertCode("provider_bad_request", self.fetch, FakeAlpaca((400, {}, b'{"message":"bad"}')))

    def test_interrupted_downloads_leave_nothing(self):
        items = bars(4)
        for failure in (TradingError("provider_timeout", "Alpaca did not answer within the timeout."),
                        TradingError("provider_unreachable", "Alpaca could not be reached.")):
            with self.subTest(code=failure.code):
                self.assertCode(failure.code, self.fetch, FakeAlpaca(ok(items[:2], "a"), failure))
        with self.assertRaises(KeyboardInterrupt):
            self.fetch(FakeAlpaca(ok(items[:2], "a"), KeyboardInterrupt()))
        self.assertEqual(self.stored(), [])
        self.assertFalse(list((self.root / "runtime").rglob("*.tmp")))

    def test_real_transport_failure_is_sanitized_and_sends_nothing(self):
        blocked = OSError("blocked in tests: " + SECRET)
        with patch.object(socket, "create_connection", side_effect=blocked), \
                patch.object(socket.socket, "connect", side_effect=blocked):
            error = self.assertCode("provider_unreachable", urllib_transport,
                                    "https://data.alpaca.markets/v2/stocks/AAPL/bars?timeframe=1Day",
                                    {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}, timeout=1, max_bytes=100)
        self.assertNotIn(SECRET, error.message)
        self.assertNotIn(KEY, error.message)


class RedactionAndIntegrityTests(Base):
    def test_credentials_never_stored_or_printed(self):
        fake = FakeAlpaca(ok(bars(3)))
        with patch.dict("os.environ", ENV):
            code, text, out = self.cli(*ARGS, "--allow-network", transport=fake)
            self.assertEqual(code, 0, out)
            stored = b"".join(p.read_bytes() for p in (self.root / "runtime").rglob("*") if p.is_file())
            for secret in (KEY, SECRET):
                self.assertNotIn(secret, text)
                self.assertNotIn(secret.encode(), stored)
            self.assertNotIn(b"APCA-API", stored)
            # page tokens are used for the next request only and never stored, even if one equals a credential
            items = bars(2)
            fake = FakeAlpaca(ok(items[:1], KEY), ok(items[1:]))
            code, text, out = self.cli(*ARGS, "--allow-network", "--adjustment", "split", transport=fake)
            self.assertEqual(code, 0, out)
            self.assertNotIn(KEY, text)
            stored = b"".join(p.read_bytes() for p in (self.root / "runtime").rglob("*") if p.is_file())
            self.assertNotIn(KEY.encode(), stored)

    def test_stored_provenance_is_tamper_checked(self):
        dataset = self.fetch(FakeAlpaca(ok(bars(4))))
        path = self.root / f"runtime/trading/market/datasets/{dataset['dataset_id']}.json"
        original = path.read_text(encoding="utf-8")
        for change in (lambda d: d["source"]["provider"].update(feed="sip"),
                       lambda d: d["source"]["provider"]["coverage"].update(bars=5),
                       lambda d: d["source"]["provider"].update(retrieved_at="2026-09-01T00:00:00Z"),
                       lambda d: d["source"]["provider"]["responses"][0].update(sha256="0" * 64),
                       lambda d: d.update(data_label="delayed"),
                       lambda d: d["source"].pop("provider")):
            document = json.loads(original)
            change(document)
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.subTest(change=change):
                self.assertCode("dataset_corrupt", MarketStore(self.root).load, dataset["dataset_id"])
        path.write_text(original, encoding="utf-8")
        self.assertEqual(MarketStore(self.root).load(dataset["dataset_id"])["dataset_id"], dataset["dataset_id"])

    def test_other_adapters_cannot_carry_provider_provenance(self):
        from vicekrack.trading.market.store import make_adapter
        dataset = MarketStore(self.root, clock=lambda: NOW).import_dataset(
            make_adapter("synthetic", config=self.market_config, fixture="synth1-5m"), config=self.market_config,
            config_sha256=self.market_sha)
        path = self.root / f"runtime/trading/market/datasets/{dataset['dataset_id']}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        fetched = self.fetch(FakeAlpaca(ok(bars(2))))
        document["source"]["provider"] = fetched["source"]["provider"]
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertCode("dataset_corrupt", MarketStore(self.root).load, dataset["dataset_id"])


class SessionAndHQTests(Base):
    def test_fetched_dataset_runs_a_session_offline_and_shows_provenance(self):
        dataset = self.fetch(FakeAlpaca(ok(bars(40))))
        with patch.object(alpaca, "urllib_transport", side_effect=AssertionError("network used by a session")):
            out = SessionRunner(self.root, clock=lambda: NOW).start(dataset["dataset_id"])
        self.assertEqual(out["status"], "completed", out)
        document = session_document(out["session_id"], self.root)
        source = document["data_source"]
        self.assertEqual((source["adapter"], source["data_label"], source["provider"]["feed"],
                          source["provider"]["adjustment"], source["provider"]["retrieved_at"]),
                         ("alpaca_historical", "historical", "iex", "raw", NOW))
        self.assertEqual(source["coverage"]["bars"], 40)
        self.assertIsNone(document["data_source_problem"])
        synthetic = session_document("demo")
        self.assertIsNone(synthetic["data_source"]["provider"])

    def test_session_shows_a_dataset_that_no_longer_validates(self):
        dataset = self.fetch(FakeAlpaca(ok(bars(40))))
        out = SessionRunner(self.root, clock=lambda: NOW).start(dataset["dataset_id"])
        path = self.root / f"runtime/trading/market/datasets/{dataset['dataset_id']}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["source"]["provider"]["feed"] = "sip"
        path.write_text(json.dumps(document), encoding="utf-8")
        summary = session_document(out["session_id"], self.root)
        self.assertEqual((summary["data_source"], summary["data_source_problem"]), (None, "dataset_corrupt"))


class IsolationTests(unittest.TestCase):
    def test_network_code_lives_only_in_the_provider_module(self):
        for path in sorted((ROOT / "vicekrack/trading").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            relative = path.relative_to(ROOT).as_posix()
            for forbidden in ("urllib", "import socket", "http.client", "requests"):
                with self.subTest(file=relative, forbidden=forbidden):
                    if relative == "vicekrack/trading/providers/alpaca.py" and forbidden in ("urllib", "import socket"):
                        continue
                    self.assertNotIn(forbidden, source)
            if relative.startswith(("vicekrack/trading/agents", "vicekrack/trading/simulation",
                                    "vicekrack/trading/analytics", "vicekrack/trading/session",
                                    "vicekrack/trading/signals", "vicekrack/trading/indicators")):
                self.assertNotIn("from ..providers", source, relative)
                self.assertNotIn("trading.providers", source, relative)
        provider = (ROOT / "vicekrack/trading/providers/alpaca.py").read_text(encoding="utf-8")
        for forbidden in ("orders", "/v2/positions", "/v2/account", "paper-api", "stream", "websocket", "while True",
                          "threading", "sleep(", "PaperAccount"):
            self.assertNotIn(forbidden, provider)
        urls = set(re.findall(r"https://[^\s\"']+", provider))
        self.assertEqual(urls, {"https://data.alpaca.markets/v2/stocks/{symbol}/bars"})      # one fixed endpoint

    def test_documents_name_only_environment_variables(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("APCA_API_KEY_ID=\n", example)
        self.assertIn("APCA_API_SECRET_KEY=\n", example)


if __name__ == "__main__":
    unittest.main()
