"""Shared fake Kraken for trading tests: AssetPairs, Ticker and 1-minute OHLC from seeded price walks.

Coins (all quoted in USD): BTC (Kraken calls it XBT), ETH, SOL, DOGE (XDG), plus a stablecoin (USDT), a thin coin
(TINY, almost no volume), a coin too cheap to trade (DUST), a euro pair and a dark-pool pair that must be ignored.
`FakeKraken(index)` serves the market as of minute `index` (that minute's bar is still open).
"""

import json
import math
import random
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

BASE_EPOCH = 1794024000          # aligned to a whole minute


def walk(seed, minutes=1500, start=100.0, drift=0.0, sigma=0.0015):
    rng = random.Random(seed)
    prices = [start]
    for _ in range(minutes - 1):
        prices.append(prices[-1] * (1 + drift + rng.gauss(0, sigma)))
    return prices


def streak(seed, minutes=1500, start=150.0, begin=600, length=40, step=0.004):
    """A random walk with a steady climb of `length` minutes starting at minute `begin`."""
    rng = random.Random(seed)
    prices = [start]
    for k in range(1, minutes):
        move = step if begin <= k < begin + length else rng.gauss(0, 0.0015)
        prices.append(prices[-1] * (1 + move))
    return prices


COINS = {
    # key: (altname, wsname, series, volume per minute)
    "XXBTZUSD": ("XBTUSD", "XBT/USD", walk(7, start=90000.0), 5.0),
    "XETHZUSD": ("ETHUSD", "ETH/USD", walk(11, start=3000.0), 50.0),
    "SOLUSD": ("SOLUSD", "SOL/USD", streak(13), 2000.0),
    "XDGUSD": ("XDGUSD", "XDG/USD", walk(17, start=0.2), 2_000_000.0),
    "USDTZUSD": ("USDTZUSD", "USDT/USD", [1.0] * 1500, 1_000_000.0),
    "TINYUSD": ("TINYUSD", "TINY/USD", streak(19, start=2.0), 1.0),
    "DUSTUSD": ("DUSTUSD", "DUST/USD", walk(23, start=0.0000002), 1e12),
}
OTHER_PAIRS = {
    "XXBTZEUR": {"altname": "XBTEUR", "wsname": "XBT/EUR", "quote": "ZEUR", "status": "online"},
    "XXBTZUSD.d": {"altname": "XBTUSD.d", "wsname": "XBT/USD", "quote": "ZUSD", "status": "online"},
}


def text(value):
    return f"{value:.10f}".rstrip("0").rstrip(".") if value < 0.001 else f"{value:.8f}".rstrip("0").rstrip(".")


def now_text(index, second=5):
    return datetime.fromtimestamp(BASE_EPOCH + 60 * index + second, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(index, second=5):
    return BASE_EPOCH + 60 * index + second


class FakeKraken:
    def __init__(self, index, coins=None, fail=(), scale=None):
        self.index, self.coins = index, coins or COINS
        self.fail, self.scale, self.calls = set(fail), scale or {}, []

    def price(self, key, k):
        return self.coins[key][2][k] * self.scale.get(key, 1.0)

    def __call__(self, url, headers, *, timeout, max_bytes, method="GET", body=None):
        assert method == "GET" and body is None and url.startswith("https://api.kraken.com/0/public/")
        parts = urlsplit(url)
        endpoint = parts.path.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        if endpoint in self.fail:
            raise OSError("down")
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if endpoint == "AssetPairs":
            result = {key: {"altname": alt, "wsname": ws, "base": ws.split("/")[0], "quote": "ZUSD", "status": "online",
                            "ordermin": "0.0001"} for key, (alt, ws, _, _) in self.coins.items()}
            result.update(OTHER_PAIRS)
        elif endpoint == "Ticker":
            wanted = set(query["pair"].split(",")) if "pair" in query else None
            result = {}
            for key, (alt, ws, series, volume) in self.coins.items():
                if wanted is not None and alt not in wanted and key not in wanted:
                    continue
                k = self.index
                last = self.price(key, k)
                window = [self.price(key, j) for j in range(max(0, k - 1440), k + 1)]
                result[key] = {"c": [text(last), "1"], "a": [text(last * 1.0005), "1", "1"], "b": [text(last * 0.9995), "1", "1"],
                               "o": text(self.price(key, max(0, k - 600))), "h": [text(max(window)), text(max(window))],
                               "l": [text(min(window)), text(min(window))], "v": [str(volume * 600), str(volume * 1440)],
                               "p": [text(last), text(last)]}
            result.update({"XXBTZEUR": {"c": ["1", "1"]}})
        elif endpoint == "OHLC":
            pair = query["pair"]
            key = next(k for k, v in self.coins.items() if v[0] == pair)
            minutes = int(query["interval"])
            rows = []
            for k in range(max(1, self.index - 300), self.index + 1):
                if minutes != 1 and k % minutes:
                    continue
                o, c = self.price(key, k - 1), self.price(key, k)
                rows.append([BASE_EPOCH + 60 * k, text(o), text(max(o, c) * 1.0004), text(min(o, c) * 0.9996), text(c), text(c),
                             f"{self.coins[key][3]:.8f}", 3])
            result = {key: rows, "last": rows[-1][0]}
        else:
            raise AssertionError(endpoint)
        return 200, {}, json.dumps({"error": [], "result": result}).encode()


assert math.isfinite(COINS["SOLUSD"][2][-1])
