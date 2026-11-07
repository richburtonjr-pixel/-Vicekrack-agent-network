"""Shared fake market for trade desk tests: a seeded random walk served in Kraken's OHLC format."""

import json
import random
from datetime import datetime, timezone

BASE_EPOCH = 1794024000          # 2026-11-12T04:00:00Z, aligned to 5 minutes


def walk(seed=7, bars=1500, start=90000.0):
    rng = random.Random(seed)
    prices = [start]
    for _ in range(bars - 1):
        prices.append(prices[-1] * (1 + rng.gauss(0, 0.0015)))
    volumes = [1 + rng.random() * 2 for _ in range(bars)]
    return prices, volumes


def now_text(index):
    """The cycle time just after bar `index - 1` closed (bar index is still open)."""
    return datetime.fromtimestamp(BASE_EPOCH + 300 * index + 20, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SeriesKraken:
    """Serves bars 0..index (the last one still open) for both pairs; ETH follows its own walk."""

    def __init__(self, index, btc=None, eth=None, scale=None):
        self.index = index
        self.series = {"XBTUSD": btc or walk(7), "ETHUSD": eth or walk(11, start=3000.0)}
        self.scale = scale or {}

    def __call__(self, url, headers, *, timeout, max_bytes, method="GET", body=None):
        pair = "XBTUSD" if "XBTUSD" in url else "ETHUSD"
        prices, volumes = self.series[pair]
        factor = self.scale.get(pair, 1.0)
        rows = []
        for k in range(max(1, self.index - 200), self.index + 1):
            o, c = prices[k - 1] * factor, prices[k] * factor
            rows.append([BASE_EPOCH + 300 * k, f"{o:.2f}", f"{max(o, c) * 1.0004:.2f}", f"{min(o, c) * 0.9996:.2f}",
                         f"{c:.2f}", f"{c:.2f}", f"{volumes[k]:.8f}", 5])
        return 200, {}, json.dumps({"error": [], "result": {pair: rows, "last": rows[-1][0]}}).encode()
