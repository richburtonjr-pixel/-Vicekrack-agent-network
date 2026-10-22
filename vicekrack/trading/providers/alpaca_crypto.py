"""Alpaca historical CRYPTO bars (HQ Phase 3a): BTC/USD and ETH/USD, opt-in, historical only.

The crypto twin of `alpaca.py` (stocks). It opens a connection only when the caller passes
`allow_network=True` (the CLI's `--allow-network`), through the same bounded HTTPS transport,
and produces an ordinary immutable Step 25 dataset that research, sessions and the HQ read offline.

Official endpoint (read from Alpaca's API reference, October 2026):
  GET https://data.alpaca.markets/v1beta3/crypto/us/bars
  headers  APCA-API-KEY-ID, APCA-API-SECRET-KEY (authentication is required)
  query    symbols (comma-separated, e.g. BTC/USD), timeframe, start, end (both inclusive, RFC 3339),
           limit (1-10000, counts data points across all symbols), page_token, sort (asc | desc)
  200      {"bars": {"BTC/USD": [{"t", "o", "h", "l", "c", "v", "n", "vw"}]}, "next_page_token": str|null}
  errors   400 invalid parameter, 401 authentication, 403 forbidden, 429 rate limit, 500 server error
Bars include quote midpoint prices; a bar with no trades has volume 0 and prices from quotes.

How crypto differs from stocks here:
- Crypto trades 24/7, so the dataset timezone is UTC and the requested range is whole UTC days.
- Only intraday intervals (1m-1h). Daily crypto bars are refused until Alpaca's daily start time is
  confirmed; a wrong guess would misalign every bar.
- One pair per dataset. Symbols are stored without the slash (BTCUSD), because the slash is not
  allowed in ViceKrack symbols or file names; the provider pair (BTC/USD) is recorded in provenance.
- No feed or adjustment choice exists for crypto; provenance records feed `crypto_us`, adjustment `raw`.
- Prices are never rounded: more than 8 decimal places fails the download. Crypto VOLUME can carry
  more places than ViceKrack stores; those values are cut (never rounded up) to 8 places and the
  number of cut values is recorded as `volume_values_truncated`. Volume is never used for money.

Everything else matches Step 40: credentials only from APCA_API_KEY_ID / APCA_API_SECRET_KEY, never
stored; fixed host; bounded pages, bytes, bars and time; no redirects, retries or fallbacks; nothing
is saved unless the whole download validates.
"""

import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from urllib.parse import urlencode

from ..contracts import sha256, utc_now
from ..errors import TradingError
from ..market.adapters import MarketDataAdapter
from ..market.bars import available_at, from_utc_text, local_text, utc_text
from .alpaca import (ADAPTER, KEY_ENV, PAGE_TOKEN, PROVIDER, RETRIEVAL_MEANING, SECRET_ENV, STATUS_ERRORS, _date,
                     parse_bar_time, plain_decimal, urllib_transport)

ENDPOINT = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
TIMEZONE = "UTC"
FEED, ADJUSTMENT = "crypto_us", "raw"
TIMEFRAMES = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "1Hour"}
BAR_KEYS, OPTIONAL_BAR_KEYS = {"t", "o", "h", "l", "c", "v"}, {"n", "vw"}
EIGHT_PLACES = Decimal("0.00000001")


def is_crypto_symbol(symbol, config):
    """True when `symbol` names a configured crypto pair, as BTCUSD or BTC/USD."""
    pairs = config.get("alpaca_crypto", {}).get("pairs", {})
    return isinstance(symbol, str) and (symbol in pairs or symbol in pairs.values())


def _midnight_utc(day):
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def request_hash(pair, timeframe, requested, page_limit):
    return sha256({"endpoint": ENDPOINT, "symbols": pair, "timeframe": timeframe, "start": requested["start_utc"],
                   "end_before": requested["end_before_utc"], "limit": page_limit, "sort": "asc"})


def source_hash(provider):
    return sha256({"request": provider["request_sha256"], "responses": [r["sha256"] for r in provider["responses"]]})


def volume_decimal(text, where):
    """Volume as exact decimal text; more than 8 places is cut toward zero. Returns (text, was_cut)."""
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        raise TradingError("provider_malformed_response", f"{where}: a value is not a number.") from None
    if not value.is_finite() or value < 0:
        raise TradingError("provider_malformed_response", f"{where}: the volume is not a finite, non-negative number.")
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -8:
        return plain_decimal(str(value.quantize(EIGHT_PLACES, rounding=ROUND_DOWN)), where), True
    return plain_decimal(text, where), False


class AlpacaCryptoAdapter(MarketDataAdapter):
    """Step 25 adapter interface: `read()` -> (rows, source, defaults). Network only when allowed."""
    adapter = ADAPTER
    allowed_labels = ("historical",)

    def __init__(self, *, symbol, interval, start, end, allow_network, config,
                 transport=urllib_transport, clock=utc_now, environ=None):
        if not allow_network:
            raise TradingError("network_not_allowed",
                               "Fetching from Alpaca needs explicit opt-in: add --allow-network. Nothing was requested.")
        crypto = config.get("alpaca_crypto")
        if not crypto:
            raise TradingError("invalid_provider_config", "config/market-providers.json has no alpaca_crypto section.")
        self.limits = crypto["limits"]
        pairs = crypto["pairs"]
        stored = symbol if symbol in pairs else next((k for k, v in pairs.items() if v == symbol), None)
        if stored is None:
            raise TradingError("invalid_symbol", "Use a crypto pair from config/market-providers.json, like BTC/USD or ETH/USD.")
        if interval not in TIMEFRAMES:
            raise TradingError("interval_not_allowed",
                               "Crypto supports 1m, 5m, 15m, 30m or 1h. Daily crypto bars are not supported yet.")
        first, last = _date(start, "start"), _date(end, "end")
        today = from_utc_text(clock()).date()
        if first > last:
            raise TradingError("invalid_date_range", "--start must not be after --end.")
        if first < date.fromisoformat(crypto["earliest_date"]):
            raise TradingError("invalid_date_range", "--start is earlier than the provider history allowed in the config.")
        if last >= today:
            raise TradingError("range_not_historical",
                               "--end must be before today (UTC date): only completed historical days are fetched.")
        if (last - first).days + 1 > self.limits["max_range_days"][interval]:
            raise TradingError("range_too_long", f"The {interval} range is longer than max_range_days allows.")
        self.symbol, self.pair, self.interval, self.timeframe = stored, pairs[stored], interval, TIMEFRAMES[interval]
        self.requested = {"start_date": first.isoformat(), "end_date": last.isoformat(),
                          "start_utc": utc_text(_midnight_utc(first)),
                          "end_before_utc": utc_text(_midnight_utc(last + timedelta(days=1))), "timezone": TIMEZONE}
        self.transport, self.clock = transport, clock
        self.environ = os.environ if environ is None else environ

    # ------------------------------------------------------------------ network (bounded, no retries)
    def _credentials(self):
        key, secret = (str(self.environ.get(KEY_ENV, "")).strip(), str(self.environ.get(SECRET_ENV, "")).strip())
        if not key or not secret:
            raise TradingError("provider_credentials_missing",
                               f"Set {KEY_ENV} and {SECRET_ENV} in your environment (never in files or chat). "
                               "Nothing was requested.")
        if any(ord(ch) < 33 or ord(ch) > 126 for ch in key + secret):
            raise TradingError("provider_credentials_invalid",
                               f"{KEY_ENV} or {SECRET_ENV} contains spaces or control characters. Nothing was requested.")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json",
                "User-Agent": "ViceKrack-historical-bars/1.0"}

    def _url(self, page_token):
        query = {"symbols": self.pair, "timeframe": self.timeframe, "start": self.requested["start_utc"],
                 "end": utc_text(from_utc_text(self.requested["end_before_utc"]) - timedelta(seconds=1)),
                 "limit": self.limits["page_limit"], "sort": "asc"}
        if page_token is not None:
            query["page_token"] = page_token
        return ENDPOINT + "?" + urlencode(query)

    def _page(self, headers, page_token, number):
        status, response_headers, body = self.transport(self._url(page_token), headers,
                                                        timeout=self.limits["timeout_seconds"],
                                                        max_bytes=self.limits["max_response_bytes"])
        if status != 200:
            code, message = STATUS_ERRORS.get(status, (
                ("provider_unavailable", f"Alpaca had a server error (HTTP {int(status)}). Nothing was saved; it was not "
                                         "retried.") if 500 <= int(status) <= 599 else
                ("provider_unexpected_status", f"Alpaca answered with HTTP {int(status)}. Nothing was saved.")))
            if status == 429:
                reset = str(response_headers.get("x-ratelimit-reset", ""))
                if reset.isdigit() and len(reset) <= 12:
                    message += " The limit resets at " + utc_text(datetime.fromtimestamp(int(reset), timezone.utc)) + "."
            raise TradingError(code, message)
        if len(body) > self.limits["max_response_bytes"]:
            raise TradingError("provider_response_too_large", "An Alpaca response exceeded max_response_bytes. Nothing was saved.")
        if not body:
            raise TradingError("provider_malformed_response", "Alpaca returned an empty response. Nothing was saved.")
        try:
            document = json.loads(body.decode("utf-8"), parse_float=str, parse_int=str)
        except (UnicodeError, ValueError):
            raise TradingError("provider_malformed_response", f"Page {number}: the response is not valid JSON. "
                                                              "Nothing was saved.") from None
        grouped = document.get("bars") if isinstance(document, dict) else None
        if (not isinstance(grouped, (dict, type(None))) or (grouped and set(grouped) != {self.pair})
                or not isinstance(document.get("next_page_token"), (str, type(None)))):
            raise TradingError("provider_malformed_response", f"Page {number}: the response does not have the expected "
                                                              "shape or pair. Nothing was saved.")
        items = (grouped or {}).get(self.pair) or []
        if not isinstance(items, list):
            raise TradingError("provider_malformed_response", f"Page {number}: the bars are not a list. Nothing was saved.")
        token = document["next_page_token"]
        if token is not None and not PAGE_TOKEN.match(token):
            raise TradingError("provider_malformed_response", f"Page {number}: the page token is malformed.")
        return items, token, body

    def read(self):
        headers = self._credentials()
        rows, responses, seen_tokens, token, total_bytes, cut = [], [], set(), None, 0, 0
        start, end_before = from_utc_text(self.requested["start_utc"]), from_utc_text(self.requested["end_before_utc"])
        for number in range(1, self.limits["max_pages"] + 1):
            items, token, body = self._page(headers, token, number)
            total_bytes += len(body)
            if total_bytes > self.limits["max_total_bytes"]:
                raise TradingError("provider_response_too_large", "The download exceeded max_total_bytes. Nothing was saved.")
            responses.append({"page": number, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                              "bars": len(items)})
            for index, bar in enumerate(items, start=1):
                row, was_cut = self._row(bar, f"Page {number} bar {index}", start, end_before, len(rows) + 1)
                rows.append(row)
                cut += was_cut
                if len(rows) > self.limits["max_bars"]:
                    raise TradingError("provider_too_many_bars", "The download exceeded max_bars. Nothing was saved.")
            if token is None:
                break
            if token in seen_tokens:
                raise TradingError("provider_pagination_loop", "Alpaca repeated a page token. Nothing was saved.")
            seen_tokens.add(token)
        else:
            raise TradingError("provider_page_limit", "More pages remain than max_pages allows; the download is incomplete "
                                                      "and nothing was saved. Use a shorter range.")
        if not rows:
            raise TradingError("provider_no_bars", "Alpaca returned no bars for this pair and range. Nothing was saved.")
        retrieved_at = self.clock()
        retrieved = from_utc_text(retrieved_at)
        for row in rows:
            if row["_available"] > retrieved:
                raise TradingError("unfinished_bar", f"Bar {row['row']} had not closed at retrieval time. Nothing was saved.")
        first = from_utc_text(min(r["_start"] for r in rows))
        last = from_utc_text(max(r["_start"] for r in rows))
        last_available = available_at(last, self.interval, TIMEZONE)
        provider = {
            "name": PROVIDER, "endpoint": ENDPOINT, "historical_only": True, "feed": FEED, "adjustment": ADJUSTMENT,
            "asset_class": "crypto", "pair": self.pair, "timeframe": self.timeframe, "requested": dict(self.requested),
            "coverage": {"first_start_utc": utc_text(first), "last_start_utc": utc_text(last),
                         "last_available_utc": utc_text(last_available), "bars": len(rows),
                         "uncovered_before_first_seconds": int((first - start).total_seconds()),
                         "uncovered_after_last_seconds": int((end_before - last_available).total_seconds()),
                         "calendar": "none"},
            "retrieved_at": retrieved_at, "retrieval_meaning": RETRIEVAL_MEANING,
            "request_sha256": request_hash(self.pair, self.timeframe, self.requested, self.limits["page_limit"]),
            "page_limit": self.limits["page_limit"], "responses": responses, "credentials_recorded": False,
            "volume_values_truncated": cut,
        }
        source = {"adapter": ADAPTER, "name": f"alpaca-{FEED}-{ADJUSTMENT}",
                  "file_name": f"alpaca-{self.symbol}-{self.interval}.json",
                  "file_sha256": source_hash(provider), "file_bytes": total_bytes, "rows": len(rows), "provider": provider}
        defaults = {"symbol": self.symbol, "interval": self.interval, "timezone": TIMEZONE, "currency": "USD"}
        return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows], source, defaults

    def _row(self, bar, where, start, end_before, number):
        if not isinstance(bar, dict) or not BAR_KEYS <= set(bar) or set(bar) - BAR_KEYS - OPTIONAL_BAR_KEYS:
            raise TradingError("provider_malformed_response", f"{where}: the bar does not have the expected fields.")
        moment = parse_bar_time(bar["t"], where)
        if not start <= moment < end_before:
            raise TradingError("provider_bar_outside_request", f"{where}: the bar is outside the requested range.")
        values = {name: plain_decimal(bar[key], where) for key, name in
                  (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"))}
        values["volume"], was_cut = volume_decimal(bar["v"], where)
        return ({"row": number, "timestamp": local_text(moment, TIMEZONE), **values, "_start": utc_text(moment),
                 "_available": available_at(moment, self.interval, TIMEZONE)}, was_cut)


def validate_crypto_provider(dataset):
    """Consistency rules for crypto datasets fetched from Alpaca (called through alpaca.validate_provider)."""
    source = dataset["source"]
    provider = source["provider"]
    coverage, requested = provider["coverage"], provider["requested"]
    pair = provider.get("pair")
    first, last = from_utc_text(dataset["first_start_utc"]), from_utc_text(dataset["last_start_utc"])
    start, end_before = from_utc_text(requested["start_utc"]), from_utc_text(requested["end_before_utc"])
    checks = (
        provider.get("asset_class") == "crypto", isinstance(pair, str),
        isinstance(pair, str) and pair.replace("/", "") == dataset["symbol"],
        isinstance(provider.get("volume_values_truncated"), int),
        provider["feed"] == FEED, provider["adjustment"] == ADJUSTMENT,
        dataset["data_label"] == "historical", dataset["timezone"] == TIMEZONE, dataset["currency"] == "USD",
        requested["timezone"] == TIMEZONE, TIMEFRAMES.get(dataset["interval"]) == provider["timeframe"],
        requested["start_utc"] == utc_text(_midnight_utc(date.fromisoformat(requested["start_date"]))),
        requested["end_before_utc"] == utc_text(_midnight_utc(date.fromisoformat(requested["end_date"]) + timedelta(days=1))),
        start <= first and from_utc_text(dataset["last_available_utc"]) <= end_before,
        (coverage["first_start_utc"], coverage["last_start_utc"], coverage["last_available_utc"], coverage["bars"])
        == (dataset["first_start_utc"], dataset["last_start_utc"], dataset["last_available_utc"], dataset["bar_count"]),
        coverage["uncovered_before_first_seconds"] == int((first - start).total_seconds()),
        coverage["uncovered_after_last_seconds"]
        == int((end_before - from_utc_text(dataset["last_available_utc"])).total_seconds()),
        from_utc_text(dataset["last_available_utc"]) <= from_utc_text(provider["retrieved_at"]),
        sum(r["bars"] for r in provider["responses"]) == dataset["bar_count"] == source["rows"],
        [r["page"] for r in provider["responses"]] == list(range(1, len(provider["responses"]) + 1)),
        sum(r["bytes"] for r in provider["responses"]) == source["file_bytes"],
        isinstance(pair, str)
        and provider["request_sha256"] == request_hash(pair, provider["timeframe"], requested, provider["page_limit"]),
        source["file_sha256"] == source_hash(provider),
        last <= from_utc_text(dataset["last_available_utc"]),
    )
    if not all(checks):
        raise TradingError("dataset_invalid", "The Alpaca crypto dataset provenance does not match its bars.")
