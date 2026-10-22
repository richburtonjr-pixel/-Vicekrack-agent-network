"""Kraken public market data (first slice): no account, no API key, no trading.

This module reads Kraken's public OHLC endpoint and turns it into the same row shape the
market-data adapters use. It is opt-in: the caller must pass `allow_network=True`.

Official endpoint:
  GET https://api.kraken.com/0/public/OHLC?pair=<PAIR>&interval=<minutes>
  200 {"error": [], "result": {"<PAIR>": [[time, open, high, low, close, vwap, volume, count], ...],
                                "last": <time>}}
  Prices and volumes are strings, so they are kept as exact text (never binary floats).
  The newest bar may still be open; it is dropped so only closed bars are returned.

Rules:
- No credentials are sent or read here. Public data only.
- Only the fixed host and path above are used; redirects are refused; response size is capped.
- Errors carry fixed codes and messages, never response bodies.
- Nothing here places, changes or cancels orders. No order endpoint exists in this file.
"""

import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from ..errors import TradingError
from ..market.adapters import MarketDataAdapter
from .alpaca import urllib_transport

ADAPTER = "kraken_public_ohlc"
HOST = "https://api.kraken.com"
OHLC_PATH = "/0/public/OHLC"
TIMEOUT_SECONDS = 20
MAX_BYTES = 2_000_000
# Interval label -> minutes, as Kraken expects them.
INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
PAIR = re.compile(r"^[A-Z0-9]{3,12}$")


def interval_minutes(label):
    if label not in INTERVALS:
        raise TradingError("interval_not_allowed", "Use one of 1m, 5m, 15m, 30m, 1h, 4h, 1d.")
    return INTERVALS[label]


def _decimal(text, where):
    if not isinstance(text, str) or not re.match(r"^[0-9]+(\.[0-9]+)?$", text):
        raise TradingError("kraken_data_invalid", f"{where}: a price or volume is not a plain number.")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise TradingError("kraken_data_invalid", f"{where}: a price or volume is not a number.") from None
    if "." in text and len(text.split(".")[1]) > 8:
        raise TradingError("kraken_data_invalid", f"{where}: more than 8 decimal places; nothing was rounded.")
    return text


def parse_ohlc(payload, minutes, now_epoch):
    """Turn a decoded OHLC response into rows. Returns rows (oldest first) with only closed bars."""
    if not isinstance(payload, dict):
        raise TradingError("kraken_data_invalid", "Kraken returned an unexpected response.")
    if payload.get("error"):
        raise TradingError("kraken_error", "Kraken refused the public market-data request.")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise TradingError("kraken_data_invalid", "Kraken returned no market data.")
    series_keys = [k for k in result if k != "last"]
    if len(series_keys) != 1:
        raise TradingError("kraken_data_invalid", "Kraken returned an unexpected number of series.")
    bars = result[series_keys[0]]
    if not isinstance(bars, list):
        raise TradingError("kraken_data_invalid", "Kraken returned malformed bars.")
    width = minutes * 60
    rows = []
    for number, bar in enumerate(bars, start=1):
        if not isinstance(bar, list) or len(bar) < 8 or not isinstance(bar[0], int):
            raise TradingError("kraken_data_invalid", f"Bar {number} is malformed.")
        opened_at = bar[0]
        if opened_at + width > now_epoch:  # still open: not a closed bar
            continue
        moment = datetime.fromtimestamp(opened_at, tz=timezone.utc)
        rows.append({
            "row": len(rows) + 1,
            "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "open": _decimal(bar[1], f"Bar {number} open"),
            "high": _decimal(bar[2], f"Bar {number} high"),
            "low": _decimal(bar[3], f"Bar {number} low"),
            "close": _decimal(bar[4], f"Bar {number} close"),
            "volume": _decimal(bar[6], f"Bar {number} volume"),
        })
    return rows


class KrakenPublicOHLCAdapter(MarketDataAdapter):
    """`read()` -> (rows, source, defaults), the same contract as the other market-data adapters."""
    adapter = ADAPTER
    allowed_labels = ("live_public",)

    def __init__(self, *, pair, interval, allow_network, transport=urllib_transport, clock=time.time):
        if not allow_network:
            raise TradingError("network_not_allowed",
                               "Fetching Kraken market data needs explicit opt-in. Nothing was requested.")
        if not isinstance(pair, str) or not PAIR.match(pair):
            raise TradingError("invalid_pair", "Use a Kraken pair name in capitals, like XBTUSD or ETHUSD.")
        self.pair = pair
        self.interval = interval
        self.minutes = interval_minutes(interval)
        self.transport = transport
        self.clock = clock

    def read(self):
        query = urlencode({"pair": self.pair, "interval": self.minutes})
        try:
            status, _, body = self.transport(f"{HOST}{OHLC_PATH}?{query}", {"Accept": "application/json"},
                                             timeout=TIMEOUT_SECONDS, max_bytes=MAX_BYTES)
        except TradingError:
            raise
        except Exception:
            raise TradingError("provider_unreachable", "Kraken could not be reached. Nothing was saved.") from None
        if status != 200:
            raise TradingError("provider_refused", f"Kraken answered with status {status}. Nothing was saved.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise TradingError("kraken_data_invalid", "Kraken returned unreadable data.") from None
        now = int(self.clock())
        rows = parse_ohlc(payload, self.minutes, now)
        if not rows:
            raise TradingError("kraken_no_closed_bars", "Kraken returned no closed bars for that request.")
        source = {"adapter": self.adapter, "name": f"kraken-{self.pair}-{self.interval}", "rows": len(rows),
                  "retrieved_at_epoch": now}
        defaults = {"symbol": self.pair, "interval": self.interval, "timezone": "UTC", "currency": "USD"}
        return rows, source, defaults
