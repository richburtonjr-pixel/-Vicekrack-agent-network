"""HQ Phase 3a: opt-in Alpaca historical CRYPTO bars (BTC/USD, ETH/USD). Every provider response here is
MOCKED: no request leaves this process. The fake credentials below are not Alpaca keys.
"""

import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from vicekrack.hq.sessions import session_document
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.market.bars import expand_bar
from vicekrack.trading.market.store import MarketStore, load_market_config
from vicekrack.trading.providers import alpaca, alpaca_crypto
from vicekrack.trading.providers.alpaca import load_provider_config
from vicekrack.trading.providers.alpaca_crypto import AlpacaCryptoAdapter
from vicekrack.trading.providers.cli import main as fetch_main
from vicekrack.trading.session.runner import SessionRunner

NOW = "2026-10-08T12:00:00Z"
KEY, SECRET = "PKFAKEKEYID0001", "fakeSecretValue0123456789"
ENV = {"APCA_API_KEY_ID": KEY, "APCA_API_SECRET_KEY": SECRET}
SATURDAY_NIGHT = datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc)      # crypto keeps trading over the weekend


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def bars(count, start=SATURDAY_NIGHT, minutes=5, base=60000.0):
    out = []
    for i in range(count):
        price = round(base + (i % 7) * 12.5 - (i % 3) * 4.25, 2)
        out.append({"t": stamp(start + timedelta(minutes=minutes * i)), "o": price, "h": round(price + 20, 2),
                    "l": round(price - 20, 2), "c": round(price + 3.5, 2), "v": round(0.25 + i / 100, 2), "n": 12, "vw": price})
    return out


def body(items, token=None, pair="BTC/USD", raw=None):
    if raw is not None:
        return raw
    return json.dumps({"bars": {pair: items} if items is not None else {}, "next_page_token": token}).encode()


def ok(items, token=None, **kwargs):
    return 200, {}, body(items, token, **kwargs)


class FakeAlpaca:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers, *, timeout, max_bytes):
        self.calls.append({"url": url, "headers": dict(headers)})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = load_provider_config()
        self.market_config, self.market_sha = load_market_config()

    def adapter(self, transport, **overrides):
        settings = dict(symbol="BTC/USD", interval="5m", start="2026-09-05", end="2026-09-06", allow_network=True,
                        config=self.config, transport=transport, clock=lambda: NOW, environ=ENV)
        settings.update(overrides)
        return AlpacaCryptoAdapter(**settings)

    def fetch(self, transport, **overrides):
        return MarketStore(self.root, clock=lambda: NOW).import_dataset(
            self.adapter(transport, **overrides), config=self.market_config, config_sha256=self.market_sha, label="historical")

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception

    def cli(self, *argv, transport=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = fetch_main(list(argv), root=self.root, transport=transport, clock=lambda: NOW, environ=ENV)
        return code, output.getvalue(), json.loads(output.getvalue())

    def stored(self):
        folder = self.root / "runtime/trading/market/datasets"
        return sorted(folder.iterdir()) if folder.is_dir() else []


class RequestTests(Base):
    def test_needs_explicit_opt_in_and_credentials(self):
        fake = FakeAlpaca()
        self.assertCode("network_not_allowed", self.adapter, fake, allow_network=False)
        adapter = self.adapter(fake, environ={})
        self.assertCode("provider_credentials_missing", adapter.read)
        self.assertEqual(fake.calls, [])

    def test_only_configured_pairs_and_intraday_intervals(self):
        fake = FakeAlpaca()
        for symbol in ("DOGE/USD", "btc/usd", "BTC/EUR", "AAPL", ""):
            with self.subTest(symbol=symbol):
                self.assertCode("invalid_symbol", self.adapter, fake, symbol=symbol)
        error = self.assertCode("interval_not_allowed", self.adapter, fake, interval="1d")
        self.assertIn("Daily crypto bars are not supported yet", error.message)
        self.assertEqual(self.adapter(fake, symbol="ETHUSD").pair, "ETH/USD")

    def test_range_must_be_completed_utc_days_and_bounded(self):
        fake = FakeAlpaca()
        self.assertCode("range_not_historical", self.adapter, fake, end="2026-10-08")
        self.assertCode("invalid_date_range", self.adapter, fake, start="2026-09-07", end="2026-09-06")
        self.assertCode("invalid_date_range", self.adapter, fake, start="2020-12-31")
        self.assertCode("range_too_long", self.adapter, fake, interval="1m", start="2026-09-01", end="2026-09-30")

    def test_request_shape_and_headers(self):
        fake = FakeAlpaca(ok(bars(3)))
        self.fetch(fake, interval="5m")
        call = fake.calls[0]
        parts = urlsplit(call["url"])
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", "https://data.alpaca.markets/v1beta3/crypto/us/bars")
        query = parse_qs(parts.query)
        self.assertEqual(query["symbols"], ["BTC/USD"])
        self.assertEqual(query["timeframe"], ["5Min"])
        self.assertEqual(query["start"], ["2026-09-05T00:00:00Z"])
        self.assertEqual(query["end"], ["2026-09-06T23:59:59Z"])
        self.assertNotIn("feed", query)
        self.assertNotIn(KEY, call["url"])
        self.assertEqual((call["headers"]["APCA-API-KEY-ID"], call["headers"]["APCA-API-SECRET-KEY"]), (KEY, SECRET))


class DatasetTests(Base):
    def test_weekend_bars_are_continuous_in_utc(self):
        dataset = self.fetch(FakeAlpaca(ok(bars(60))))
        self.assertEqual((dataset["symbol"], dataset["timezone"], dataset["currency"]), ("BTCUSD", "UTC", "USD"))
        self.assertEqual(dataset["bar_count"], 60)
        self.assertEqual(dataset["gaps"]["gap_count"], 0, "24/7 data over a weekend night has no gaps")
        provider = dataset["source"]["provider"]
        self.assertEqual((provider["asset_class"], provider["pair"], provider["feed"], provider["adjustment"]),
                         ("crypto", "BTC/USD", "crypto_us", "raw"))
        self.assertEqual(provider["requested"]["timezone"], "UTC")
        self.assertEqual(provider["volume_values_truncated"], 0)
        bar = expand_bar(dataset, 0)
        self.assertEqual(bar["timestamp"], "2026-09-05T22:00:00+00:00")
        self.assertEqual(bar["volume"], "0.25")

    def test_pagination_across_pages(self):
        items = bars(5)
        fake = FakeAlpaca(ok(items[:2], "dG9rZW4x"), ok(items[2:]))
        dataset = self.fetch(fake)
        self.assertEqual(dataset["bar_count"], 5)
        self.assertEqual(parse_qs(urlsplit(fake.calls[1]["url"]).query)["page_token"], ["dG9rZW4x"])
        self.assertEqual([r["page"] for r in dataset["source"]["provider"]["responses"]], [1, 2])

    def test_prices_are_never_rounded(self):
        raw = body(bars(2)).replace(b'"o": 60000.0', b'"o": 60000.123456789')
        self.assertCode("provider_precision_exceeded", self.fetch, FakeAlpaca((200, {}, raw)))
        self.assertEqual(self.stored(), [])

    def test_extra_volume_places_are_cut_and_counted(self):
        raw = body(bars(3)).replace(b'"v": 0.25,', b'"v": 0.123456789012,')
        dataset = self.fetch(FakeAlpaca((200, {}, raw)))
        self.assertEqual(dataset["bars"][0]["volume"], "0.12345678")
        self.assertEqual(dataset["source"]["provider"]["volume_values_truncated"], 1)

    def test_malformed_or_unexpected_responses_save_nothing(self):
        cases = (
            (ok(bars(2), pair="ETH/USD"), "provider_malformed_response"),
            ((200, {}, b'{"bars": [], "next_page_token": null}'), "provider_malformed_response"),
            ((200, {}, b"not json"), "provider_malformed_response"),
            (ok(None), "provider_no_bars"),
            (ok([{**bars(1)[0], "t": "2026-09-09T00:00:00Z"}]), "provider_bar_outside_request"),
            (ok([{**bars(1)[0], "x": 1}]), "provider_malformed_response"),
            ((401, {}, b""), "provider_auth_failed"),
            ((429, {"x-ratelimit-reset": "1791460800"}, b""), "provider_rate_limited"),
            ((503, {}, b""), "provider_unavailable"),
        )
        for response, code in cases:
            with self.subTest(code=code):
                self.assertCode(code, self.fetch, FakeAlpaca(response))
        self.assertEqual(self.stored(), [])

    def test_unfinished_bars_are_refused(self):
        late = [{**bars(1)[0], "t": "2026-09-06T23:55:00Z"}]
        adapter = self.adapter(FakeAlpaca(ok(late)))
        adapter.clock = iter(["2026-09-06T23:58:00Z"]).__next__
        self.assertCode("unfinished_bar", adapter.read)

    def test_eth_dataset(self):
        dataset = self.fetch(FakeAlpaca(ok(bars(4, base=3000.0), pair="ETH/USD")), symbol="ETH/USD", interval="5m")
        self.assertEqual((dataset["symbol"], dataset["source"]["provider"]["pair"]), ("ETHUSD", "ETH/USD"))


class IntegrityTests(Base):
    def test_crypto_provenance_is_tamper_checked(self):
        dataset = self.fetch(FakeAlpaca(ok(bars(4))))
        path = self.root / f"runtime/trading/market/datasets/{dataset['dataset_id']}.json"
        original = path.read_text(encoding="utf-8")
        for change in (lambda d: d["source"]["provider"].update(pair="ETH/USD"),
                       lambda d: d["source"]["provider"].pop("asset_class"),
                       lambda d: d["source"]["provider"].update(feed="iex"),
                       lambda d: d["source"]["provider"]["requested"].update(timezone="America/New_York"),
                       lambda d: d["source"]["provider"].update(endpoint="https://data.alpaca.markets/v2/stocks/{symbol}/bars"),
                       lambda d: d["source"]["provider"].pop("volume_values_truncated"),
                       lambda d: d["source"]["provider"]["responses"][0].update(sha256="0" * 64)):
            document = json.loads(original)
            change(document)
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.subTest(change=change):
                self.assertCode("dataset_corrupt", MarketStore(self.root).load, dataset["dataset_id"])
        path.write_text(original, encoding="utf-8")
        self.assertEqual(MarketStore(self.root).load(dataset["dataset_id"])["symbol"], "BTCUSD")

    def test_stock_datasets_cannot_claim_crypto_fields(self):
        from test_market_provider import FakeAlpaca as StockFake, bars as stock_bars, ok as stock_ok
        from vicekrack.trading.providers.alpaca import AlpacaHistoricalAdapter
        adapter = AlpacaHistoricalAdapter(symbol="AAPL", interval="5m", start="2026-09-01", end="2026-09-02", feed="iex",
                                          adjustment="raw", allow_network=True, config=self.config,
                                          transport=StockFake(stock_ok(stock_bars(3))), clock=lambda: NOW, environ=ENV)
        dataset = MarketStore(self.root, clock=lambda: NOW).import_dataset(
            adapter, config=self.market_config, config_sha256=self.market_sha, label="historical")
        path = self.root / f"runtime/trading/market/datasets/{dataset['dataset_id']}.json"
        original = path.read_text(encoding="utf-8")
        for change in (lambda d: d["source"]["provider"].update(pair="BTC/USD"),
                       lambda d: d["source"]["provider"].update(feed="crypto_us")):
            document = json.loads(original)
            change(document)
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.subTest(change=change):
                self.assertCode("dataset_corrupt", MarketStore(self.root).load, dataset["dataset_id"])

    def test_credentials_never_stored_or_printed(self):
        code, text, out = self.cli("market-fetch", "--provider", "alpaca", "--symbol", "BTC/USD", "--interval", "5m",
                                   "--start", "2026-09-05", "--end", "2026-09-06", "--allow-network",
                                   transport=FakeAlpaca(ok(bars(3))))
        self.assertEqual(code, 0, out)
        stored = b"".join(p.read_bytes() for p in (self.root / "runtime").rglob("*") if p.is_file())
        for secret in (KEY, SECRET):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret.encode(), stored)


class CommandTests(Base):
    ARGS = ("market-fetch", "--provider", "alpaca", "--symbol", "BTC/USD", "--interval", "5m",
            "--start", "2026-09-05", "--end", "2026-09-06")

    def test_crypto_fetch_needs_no_feed_or_adjustment(self):
        code, _text, out = self.cli(*self.ARGS, "--allow-network", transport=FakeAlpaca(ok(bars(3))))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["imported"])

    def test_crypto_refuses_stock_only_options(self):
        fake = FakeAlpaca()
        code, _text, out = self.cli(*self.ARGS, "--allow-network", "--feed", "iex", transport=fake)
        self.assertEqual((code, out["error"]["code"]), (1, "crypto_options_not_allowed"))
        self.assertEqual(fake.calls, [])

    def test_stocks_still_need_feed_and_adjustment(self):
        fake = FakeAlpaca()
        code, _text, out = self.cli("market-fetch", "--provider", "alpaca", "--symbol", "AAPL", "--interval", "5m",
                                    "--start", "2026-09-01", "--end", "2026-09-02", "--allow-network", transport=fake)
        self.assertEqual((code, out["error"]["code"]), (1, "invalid_feed"))
        self.assertEqual(fake.calls, [])

    def test_no_network_without_opt_in(self):
        fake = FakeAlpaca()
        code, _text, out = self.cli(*self.ARGS, transport=fake)
        self.assertEqual((code, out["error"]["code"]), (1, "network_not_allowed"))
        self.assertEqual(fake.calls, [])


class SessionAndHQTests(Base):
    def test_crypto_dataset_runs_a_research_session_offline(self):
        dataset = self.fetch(FakeAlpaca(ok(bars(60))))
        with patch.object(alpaca, "urllib_transport", side_effect=AssertionError("network used by a session")), \
                patch.object(alpaca_crypto, "urllib_transport", side_effect=AssertionError("network used by a session")):
            out = SessionRunner(self.root, clock=lambda: NOW).start(dataset["dataset_id"])
        self.assertEqual(out["status"], "completed", out)
        source = session_document(out["session_id"], self.root)["data_source"]
        self.assertEqual((source["adapter"], source["provider"]["feed"], source["provider"]["adjustment"]),
                         ("alpaca_historical", "crypto_us", "raw"))
        self.assertEqual(source["coverage"]["gap_count"], 0)


class IsolationTests(unittest.TestCase):
    def test_crypto_module_has_one_fixed_endpoint_and_no_order_code(self):
        source = (ROOT / "vicekrack/trading/providers/alpaca_crypto.py").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"https://[^\s\"']+", source)),
                         {"https://data.alpaca.markets/v1beta3/crypto/us/bars"})
        for forbidden in ("urllib.request", "import socket", "/v2/orders", "/v2/positions", "/v2/account", "paper-api",
                          "stream", "websocket", "while True", "threading", "sleep(", "PaperAccount"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
