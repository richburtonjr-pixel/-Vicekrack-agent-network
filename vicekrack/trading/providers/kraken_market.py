"""Whole-market Kraken public data (Trading PR 4): every coin Kraken sells in US dollars.

Public endpoints only (no account, no keys, no orders):
  GET https://api.kraken.com/0/public/AssetPairs   which pairs exist     (cached 12 hours)
  GET https://api.kraken.com/0/public/Ticker       every pair's prices   (one request for the whole market)
  GET https://api.kraken.com/0/public/OHLC         one coin's price bars (charts)

ViceKrack names a coin by what the Kraken app shows plus "USD": BTCUSD, DOGEUSD, SOLUSD.
Kraken's own odd names (XBT for bitcoin, XDG for dogecoin) are translated here.

Prices are exact strings. For trading they are rounded to 8 decimal places (the practice
account's precision); coins priced under $0.000001 can't be represented well and are view-only.
"""

import json
import os
import re
import tempfile
import time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from ..contracts import ROOT
from ..errors import TradingError
from .alpaca import urllib_transport

HOST = "https://api.kraken.com"
PATHS = {"pairs": "/0/public/AssetPairs", "ticker": "/0/public/Ticker", "ohlc": "/0/public/OHLC"}
TIMEOUT = 20
MAX_BYTES = {"pairs": 4_000_000, "ticker": 6_000_000, "ohlc": 2_000_000}
CATALOG_MAX_AGE = 12 * 3600
MIN_TRADEABLE_PRICE = Decimal("0.000001")
EIGHT = Decimal("0.00000001")
RENAME = {"XBT": "BTC", "XDG": "DOGE"}
SYMBOL = re.compile(r"^[A-Z0-9]{1,12}USD$")
NUMBER = re.compile(r"^[0-9]+(\.[0-9]+)?$")
NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP", "DOGE": "Dogecoin", "ADA": "Cardano",
    "AVAX": "Avalanche", "LINK": "Chainlink", "LTC": "Litecoin", "DOT": "Polkadot", "BCH": "Bitcoin Cash",
    "XLM": "Stellar", "UNI": "Uniswap", "ATOM": "Cosmos", "NEAR": "NEAR Protocol", "SHIB": "Shiba Inu",
    "PEPE": "Pepe", "TRX": "TRON", "TON": "Toncoin", "SUI": "Sui", "APT": "Aptos", "ARB": "Arbitrum",
    "OP": "Optimism", "MATIC": "Polygon", "POL": "Polygon", "FIL": "Filecoin", "ETC": "Ethereum Classic",
    "ALGO": "Algorand", "AAVE": "Aave", "MKR": "Maker", "INJ": "Injective", "RNDR": "Render", "RENDER": "Render",
    "GRT": "The Graph", "SAND": "The Sandbox", "MANA": "Decentraland", "APE": "ApeCoin", "BONK": "Bonk",
    "WIF": "dogwifhat", "FET": "Fetch.ai", "HBAR": "Hedera", "ICP": "Internet Computer", "XMR": "Monero",
    "XTZ": "Tezos", "EOS": "EOS", "FLOKI": "Floki", "JUP": "Jupiter", "TIA": "Celestia", "SEI": "Sei",
    "USDT": "Tether", "USDC": "USD Coin", "DAI": "Dai", "KSM": "Kusama", "CRV": "Curve", "COMP": "Compound",
}


def to_price(text):
    """Exact price string -> 8-decimal string for the practice account (None if too small or invalid)."""
    try:
        if not isinstance(text, str) or not NUMBER.match(text):
            return None
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value < MIN_TRADEABLE_PRICE:
        return None
    rounded = format(value.quantize(EIGHT, rounding=ROUND_HALF_EVEN), "f").rstrip("0").rstrip(".")
    return rounded


def _get(kind, query, transport):
    url = HOST + PATHS[kind] + (("?" + urlencode(query)) if query else "")
    try:
        status, _, body = transport(url, {"Accept": "application/json"}, timeout=TIMEOUT, max_bytes=MAX_BYTES[kind])
    except TradingError:
        raise TradingError("provider_unreachable", "Kraken could not be reached.") from None
    except Exception:
        raise TradingError("provider_unreachable", "Kraken could not be reached.") from None
    if status != 200 or len(body) > MAX_BYTES[kind]:
        raise TradingError("provider_refused", "Kraken did not return market data.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise TradingError("kraken_data_invalid", "Kraken returned unreadable data.") from None
    if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("result"), dict):
        raise TradingError("kraken_error", "Kraken refused the public market-data request.")
    return payload["result"]


def parse_pairs(result):
    """AssetPairs result -> {symbol: {pair, altname, base, name, ordermin}} for online USD pairs."""
    coins = {}
    for key, info in sorted(result.items()):
        if not isinstance(info, dict) or key.endswith(".d"):
            continue
        wsname = info.get("wsname") or ""
        if info.get("quote") not in ("ZUSD", "USD") or not wsname.endswith("/USD"):
            continue
        if info.get("status", "online") != "online":
            continue
        base = RENAME.get(wsname[:-4], wsname[:-4])
        symbol = base + "USD"
        altname = info.get("altname") or key
        if not SYMBOL.match(symbol) or not re.match(r"^[A-Z0-9]{3,16}$", altname) or symbol in coins:
            continue
        coins[symbol] = {"symbol": symbol, "pair": altname, "key": key, "base": base, "name": NAMES.get(base, base),
                         "ordermin": info.get("ordermin")}
    return coins


class Catalog:
    """Which USD coins exist, cached in runtime/trading/market/kraken-usd.json for 12 hours."""

    def __init__(self, root=None, transport=urllib_transport, clock=time.time):
        self.path = Path(root if root is not None else ROOT) / "runtime/trading/market/kraken-usd.json"
        self.transport, self.clock = transport, clock
        self._coins = None

    def coins(self):
        if self._coins is not None:
            return self._coins
        cached = None
        try:
            cached = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            cached = None
        if cached and self.clock() - cached.get("saved_epoch", 0) < CATALOG_MAX_AGE and cached.get("coins"):
            self._coins = cached["coins"]
            return self._coins
        try:
            coins = parse_pairs(_get("pairs", None, self.transport))
            if not coins:
                raise TradingError("kraken_data_invalid", "Kraken listed no USD coins.")
        except TradingError:
            if cached and cached.get("coins"):
                self._coins = cached["coins"]          # an older list beats no list
                return self._coins
            raise
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False) as out:
            json.dump({"saved_epoch": int(self.clock()), "coins": coins}, out)
            temporary = out.name
        os.replace(temporary, self.path)
        self._coins = coins
        return coins

    def get(self, symbol):
        coin = self.coins().get(symbol)
        if coin is None:
            raise TradingError("unknown_coin", "Kraken doesn't sell that coin in US dollars.")
        return coin


def tickers(catalog, transport=urllib_transport, symbols=None):
    """Prices for every USD coin (or just `symbols`). -> {symbol: {...}} with exact strings."""
    coins = catalog.coins()
    by_key = {}
    for coin in coins.values():
        by_key[coin["key"]] = coin
        by_key[coin["pair"]] = coin
    query = None
    if symbols is not None:
        wanted = [coins[s]["pair"] for s in symbols if s in coins]
        if not wanted:
            return {}
        query = {"pair": ",".join(sorted(set(wanted)))}
    result = _get("ticker", query, transport)
    out = {}
    for key, row in result.items():
        coin = by_key.get(key)
        if coin is None or not isinstance(row, dict):
            continue
        try:
            last, ask, bid = row["c"][0], row["a"][0], row["b"][0]
            opened, high, low, volume, vwap = row["o"], row["h"][1], row["l"][1], row["v"][1], row["p"][1]
            if not all(isinstance(v, str) and NUMBER.match(v) for v in (last, ask, bid, opened, high, low, volume, vwap)):
                continue
            last_d, open_d = Decimal(last), Decimal(opened)
            change = None if open_d == 0 else format(((last_d - open_d) / open_d * 100).quantize(Decimal("0.01")), "f")
            volume_usd = format((Decimal(volume) * Decimal(vwap)).quantize(Decimal("1")), "f")
        except (KeyError, IndexError, TypeError, InvalidOperation):
            continue
        out[coin["symbol"]] = {"symbol": coin["symbol"], "base": coin["base"], "name": coin["name"], "pair": coin["pair"],
                               "last": last, "ask": ask, "bid": bid, "open_today": opened, "change_today_pct": change,
                               "high_24h": high, "low_24h": low, "volume_24h_usd": volume_usd,
                               "tradeable": to_price(last) is not None and to_price(ask) is not None
                               and to_price(bid) is not None}
    return out


RANGES = {"1H": (1, 60), "1D": (5, 288), "1W": (60, 168), "1M": (240, 180), "1Y": (1440, 365)}


def chart(catalog, symbol, span, transport=urllib_transport):
    """Closing prices for a chart: [[epoch, "close"], ...]. For drawing only, never for money."""
    if span not in RANGES:
        raise TradingError("invalid_range", "Pick 1H, 1D, 1W, 1M or 1Y.")
    minutes, count = RANGES[span]
    coin = catalog.get(symbol)
    result = _get("ohlc", {"pair": coin["pair"], "interval": minutes}, transport)
    series = [v for k, v in result.items() if k != "last"]
    if len(series) != 1 or not isinstance(series[0], list):
        raise TradingError("kraken_data_invalid", "Kraken returned no chart data.")
    points = []
    for bar in series[0][-count:]:
        if isinstance(bar, list) and len(bar) >= 5 and isinstance(bar[0], int) and isinstance(bar[4], str) \
                and NUMBER.match(bar[4]):
            points.append([bar[0], bar[4]])
    return points
