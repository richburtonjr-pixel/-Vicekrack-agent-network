"""Alpaca historical stock bars (Step 40): an opt-in, provider-neutral market-data adapter.

This is the ONLY module in ViceKrack that opens a market-data network connection, and it
does so only when the caller passes `allow_network=True` (the CLI's `--allow-network`).
Research agents, the simulator, analytics and sessions never import it; they read the
immutable dataset it produces through the Step 25 store, like any other dataset.

Official endpoint (read from Alpaca's API reference, October 2026):
  GET https://data.alpaca.markets/v2/stocks/{symbol}/bars
  headers  APCA-API-KEY-ID, APCA-API-SECRET-KEY (Trading API keys)
  query    timeframe, start, end (both inclusive, RFC 3339), limit (1-10000), adjustment
           (raw | split | dividend | all ...), feed (sip | iex | ...), sort, page_token
  200      {"bars": [{"t", "o", "h", "l", "c", "v", "n", "vw"}], "symbol", "next_page_token"}
  errors   400 invalid parameter, 401 authentication, 403 forbidden (for example a feed or
           range the subscription does not cover), 429 rate limit (X-RateLimit-* headers)
Bar `t` is the bar START (confirmed by Alpaca staff; daily bars start at midnight New York).

Rules enforced here:
- Credentials come only from APCA_API_KEY_ID / APCA_API_SECRET_KEY in the environment.
  They are sent only as those two headers to the fixed HTTPS host and are never stored,
  logged, hashed or echoed. Errors carry fixed codes and messages, never response bodies.
- Feed and adjustment are explicit choices (no default is assumed free or available).
  One dataset = one feed + one adjustment; no fallback to another feed, no retries.
- Historical only: the requested range must end before today (New York date) at retrieval
  time, and any bar not yet closed at retrieval is rejected (`unfinished_bar`).
- Bounded: range length per interval, pages, bytes per response and in total, bars,
  timeout; redirects are refused. Exceeding any bound fails the whole download.
- Prices are kept as exact decimals from the response text (JSON numbers are never
  converted through binary floats); more than 8 decimal places is rejected, never rounded.
- Nothing is published until every page has arrived and the Step 25 validators accept the
  whole dataset; a failed or interrupted download leaves nothing behind.
"""

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from ..contracts import ROOT, sha256, utc_now, validate_schema
from ..errors import TradingError
from ..market.adapters import MarketDataAdapter
from ..market.bars import available_at, from_utc_text, local_text, utc_text

PROVIDER = "alpaca"
ADAPTER = "alpaca_historical"
HOST = "data.alpaca.markets"
ENDPOINT = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
KEY_ENV, SECRET_ENV = "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"
TIMEZONE = "America/New_York"
NEW_YORK = ZoneInfo(TIMEZONE)
TIMEFRAMES = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "1Hour", "1d": "1Day"}
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAGE_TOKEN = re.compile(r"^[A-Za-z0-9+/=_.-]{1,512}$")
RFC3339 = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$")
BAR_KEYS, OPTIONAL_BAR_KEYS = {"t", "o", "h", "l", "c", "v"}, {"n", "vw"}
RETRIEVAL_MEANING = ("When this computer finished downloading these historical bars (wall clock). It is not a market "
                     "time and not a quote time; every bar's own start and close times are its market times.")
STATUS_ERRORS = {
    400: ("provider_bad_request", "Alpaca rejected the request parameters (HTTP 400). Nothing was saved."),
    401: ("provider_auth_failed", "Alpaca did not accept the credentials (HTTP 401). Check APCA_API_KEY_ID and "
                                  "APCA_API_SECRET_KEY. Nothing was saved."),
    403: ("provider_not_entitled", "Alpaca refused this request (HTTP 403): the account may not be entitled to this "
                                   "feed or range. Nothing was saved and no other feed was tried."),
    404: ("provider_bad_request", "Alpaca did not recognise this request (HTTP 404). Nothing was saved."),
    422: ("provider_bad_request", "Alpaca rejected the request parameters (HTTP 422). Nothing was saved."),
    429: ("provider_rate_limited", "Alpaca's rate limit was reached (HTTP 429). Nothing was saved; it was not retried."),
}


def load_provider_config(path="config/market-providers.json", root=ROOT):
    target = (Path(root) / path).resolve()
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        validate_schema("market_providers_config", config)
    except (TradingError, OSError, ValueError, UnicodeError):
        raise TradingError("invalid_provider_config", "config/market-providers.json is invalid.") from None
    return config


def _date(text, name):
    if not isinstance(text, str) or not DATE.match(text):
        raise TradingError("invalid_date_range", f"--{name} must be a date like 2026-01-15.")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise TradingError("invalid_date_range", f"--{name} is not a real date.") from None


def _midnight(day):
    return datetime(day.year, day.month, day.day, tzinfo=NEW_YORK).astimezone(timezone.utc)


def plain_decimal(text, where):
    """Exact decimal text (no exponent, no binary float), at most 8 places; never rounded."""
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        raise TradingError("provider_malformed_response", f"{where}: a value is not a number.") from None
    if not value.is_finite():
        raise TradingError("provider_malformed_response", f"{where}: a value is not a finite number.")
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -8:
        raise TradingError("provider_precision_exceeded",
                           f"{where}: a value has more than 8 decimal places; it was not rounded and nothing was saved.")
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def parse_bar_time(text, where):
    match = RFC3339.match(text) if isinstance(text, str) else None
    if not match:
        raise TradingError("provider_malformed_response", f"{where}: the bar time is not an RFC 3339 UTC time.")
    if match.group(2) and int(match.group(2)) != 0:
        raise TradingError("provider_malformed_response", f"{where}: the bar time has a sub-second part.")
    try:
        return datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc)
    except ValueError:
        raise TradingError("provider_malformed_response", f"{where}: the bar time is not a real time.") from None


def request_hash(symbol, timeframe, requested, feed, adjustment, page_limit):
    return sha256({"endpoint": ENDPOINT, "symbol": symbol, "timeframe": timeframe, "start": requested["start_utc"],
                   "end_before": requested["end_before_utc"], "feed": feed, "adjustment": adjustment,
                   "limit": page_limit, "sort": "asc"})


def source_hash(provider):
    """The dataset's `file_sha256`: the request plus every raw response, in page order."""
    return sha256({"request": provider["request_sha256"], "responses": [r["sha256"] for r in provider["responses"]]})


def urllib_transport(url, headers, *, timeout, max_bytes, method="GET", body=None):
    """One HTTPS request (GET by default; Step 41's paper broker also sends POST and DELETE) with certificate
    verification, no redirects and a byte cap. Only https:// URLs are accepted.
    Returns (status, lower-case headers, body bytes). Never raises with response or header text."""
    import socket
    import ssl
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    if not isinstance(url, str) or not url.startswith("https://") or method not in ("GET", "POST", "DELETE"):
        raise TradingError("provider_unreachable", "Only HTTPS GET, POST or DELETE requests are allowed.")
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, headers=headers, method=method, data=body)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        try:
            body = error.read(4096)
        except OSError:
            body = b""
        return error.code, {k.lower(): v for k, v in (error.headers or {}).items()}, body
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise TradingError("provider_timeout", "Alpaca did not answer within the timeout. Nothing was saved.") from None
        raise TradingError("provider_unreachable", "Alpaca could not be reached. Nothing was saved.") from None
    except (socket.timeout, TimeoutError):
        raise TradingError("provider_timeout", "Alpaca did not answer within the timeout. Nothing was saved.") from None
    except (OSError, ValueError):
        raise TradingError("provider_unreachable", "Alpaca could not be reached. Nothing was saved.") from None


class AlpacaHistoricalAdapter(MarketDataAdapter):
    """Step 25 adapter interface: `read()` -> (rows, source, defaults). Network only when allowed."""
    adapter = ADAPTER
    allowed_labels = ("historical",)

    def __init__(self, *, symbol, interval, start, end, feed, adjustment, allow_network, config,
                 transport=urllib_transport, clock=utc_now, environ=None):
        if not allow_network:
            raise TradingError("network_not_allowed",
                               "Fetching from Alpaca needs explicit opt-in: add --allow-network. Nothing was requested.")
        alpaca = config["alpaca"]
        self.limits = alpaca["limits"]
        if not isinstance(symbol, str) or not SYMBOL.match(symbol):
            raise TradingError("invalid_symbol", "Use one US stock symbol in capitals, like AAPL or BRK.B.")
        if interval not in TIMEFRAMES:
            raise TradingError("interval_not_allowed", "Use one of 1m, 5m, 15m, 30m, 1h, 1d.")
        if feed not in alpaca["feeds"]:
            raise TradingError("invalid_feed", "Choose --feed explicitly: one of the feeds in config/market-providers.json.")
        if adjustment not in alpaca["adjustments"]:
            raise TradingError("invalid_adjustment", "Choose --adjustment explicitly: raw, split, dividend or all.")
        first, last = _date(start, "start"), _date(end, "end")
        today = datetime.strptime(clock(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(NEW_YORK).date()
        if first > last:
            raise TradingError("invalid_date_range", "--start must not be after --end.")
        if first < date.fromisoformat(alpaca["earliest_date"]):
            raise TradingError("invalid_date_range", "--start is earlier than the provider history allowed in the config.")
        if last >= today:
            raise TradingError("range_not_historical",
                               "--end must be before today (New York date): only completed historical days are fetched.")
        if (last - first).days + 1 > self.limits["max_range_days"][interval]:
            raise TradingError("range_too_long", f"The {interval} range is longer than max_range_days allows.")
        self.symbol, self.interval, self.feed, self.adjustment = symbol, interval, feed, adjustment
        self.timeframe = TIMEFRAMES[interval]
        self.requested = {"start_date": first.isoformat(), "end_date": last.isoformat(),
                          "start_utc": utc_text(_midnight(first)),
                          "end_before_utc": utc_text(_midnight(last + timedelta(days=1))), "timezone": TIMEZONE}
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
        query = {"timeframe": self.timeframe, "start": self.requested["start_utc"],
                 "end": utc_text(from_utc_text(self.requested["end_before_utc"]) - timedelta(seconds=1)),
                 "limit": self.limits["page_limit"], "adjustment": self.adjustment, "feed": self.feed, "sort": "asc"}
        if page_token is not None:
            query["page_token"] = page_token
        return ENDPOINT.format(symbol=quote(self.symbol, safe="")) + "?" + urlencode(query)

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
        if (not isinstance(document, dict) or document.get("symbol") != self.symbol
                or not isinstance(document.get("bars"), (list, type(None)))
                or not isinstance(document.get("next_page_token"), (str, type(None)))):
            raise TradingError("provider_malformed_response", f"Page {number}: the response does not have the expected "
                                                              "shape or symbol. Nothing was saved.")
        token = document["next_page_token"]
        if token is not None and not PAGE_TOKEN.match(token):
            raise TradingError("provider_malformed_response", f"Page {number}: the page token is malformed.")
        return document["bars"] or [], token, body

    def read(self):
        headers = self._credentials()
        rows, responses, seen_tokens, token, total_bytes = [], [], set(), None, 0
        start, end_before = from_utc_text(self.requested["start_utc"]), from_utc_text(self.requested["end_before_utc"])
        for number in range(1, self.limits["max_pages"] + 1):
            bars, token, body = self._page(headers, token, number)
            total_bytes += len(body)
            if total_bytes > self.limits["max_total_bytes"]:
                raise TradingError("provider_response_too_large", "The download exceeded max_total_bytes. Nothing was saved.")
            responses.append({"page": number, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                              "bars": len(bars)})
            for index, bar in enumerate(bars, start=1):
                rows.append(self._row(bar, f"Page {number} bar {index}", start, end_before, len(rows) + 1))
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
            raise TradingError("provider_no_bars", "Alpaca returned no bars for this symbol, range and feed. Nothing was saved.")
        retrieved_at = self.clock()
        retrieved = from_utc_text(retrieved_at)
        for row in rows:
            if row["_available"] > retrieved:
                raise TradingError("unfinished_bar", f"Bar {row['row']} had not closed at retrieval time. Nothing was saved.")
        first = from_utc_text(min(r["_start"] for r in rows))
        last = from_utc_text(max(r["_start"] for r in rows))
        last_available = available_at(last, self.interval, TIMEZONE)
        provider = {
            "name": PROVIDER, "endpoint": ENDPOINT, "historical_only": True, "feed": self.feed,
            "adjustment": self.adjustment, "timeframe": self.timeframe, "requested": dict(self.requested),
            "coverage": {"first_start_utc": utc_text(first), "last_start_utc": utc_text(last),
                         "last_available_utc": utc_text(last_available), "bars": len(rows),
                         "uncovered_before_first_seconds": int((first - start).total_seconds()),
                         "uncovered_after_last_seconds": int((end_before - last_available).total_seconds()),
                         "calendar": "none"},
            "retrieved_at": retrieved_at, "retrieval_meaning": RETRIEVAL_MEANING,
            "request_sha256": request_hash(self.symbol, self.timeframe, self.requested, self.feed, self.adjustment,
                                           self.limits["page_limit"]),
            "page_limit": self.limits["page_limit"], "responses": responses, "credentials_recorded": False,
        }
        source = {"adapter": ADAPTER, "name": f"alpaca-{self.feed}-{self.adjustment}",
                  "file_name": f"alpaca-{self.symbol.replace('.', '_')}-{self.interval}.json",
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
                  (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}
        return {"row": number, "timestamp": local_text(moment, TIMEZONE), **values, "_start": utc_text(moment),
                "_available": available_at(moment, self.interval, TIMEZONE)}


def validate_provider(dataset):
    """Extra consistency rules for datasets fetched from Alpaca (called by bars.validate_dataset)."""
    source = dataset["source"]
    provider = source.get("provider")
    if provider is None:
        raise TradingError("dataset_invalid", "An Alpaca dataset must record its provider provenance.")
    coverage, requested = provider["coverage"], provider["requested"]
    first, last = from_utc_text(dataset["first_start_utc"]), from_utc_text(dataset["last_start_utc"])
    start, end_before = from_utc_text(requested["start_utc"]), from_utc_text(requested["end_before_utc"])
    expected_request = request_hash(dataset["symbol"], provider["timeframe"], requested, provider["feed"],
                                    provider["adjustment"], provider["page_limit"])
    checks = (
        dataset["data_label"] == "historical", dataset["timezone"] == TIMEZONE, dataset["currency"] == "USD",
        TIMEFRAMES.get(dataset["interval"]) == provider["timeframe"],
        requested["start_utc"] == utc_text(_midnight(date.fromisoformat(requested["start_date"]))),
        requested["end_before_utc"] == utc_text(_midnight(date.fromisoformat(requested["end_date"]) + timedelta(days=1))),
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
        provider["request_sha256"] == expected_request, source["file_sha256"] == source_hash(provider),
        last <= from_utc_text(dataset["last_available_utc"]),
    )
    if not all(checks):
        raise TradingError("dataset_invalid", "The Alpaca dataset provenance does not match its bars.")
