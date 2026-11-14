"""The radar (Trading PR 4): the Market Scout's whole-market streak scan. Plain code, no AI.

Every minute the watch loop prices every Kraken USD coin in one request and adds it to the TAPE:
the last `history_minutes` of prices per coin (runtime/trading/watch/tape.json).

A coin is a STREAK CANDIDATE when, from the tape:
  - it trades enough: 24-hour volume of at least `min_volume_24h_usd` (thin coins jump around and
    their buy/sell gap eats you alive),
  - it isn't a stablecoin or a currency (`skip_bases`),
  - it is up at least `min_rise_5m_pct` over 5 minutes AND `min_rise_15m_pct` over 15 minutes,
  - and it rose in at least `min_up_minutes_of_last_5` of the last 5 one-minute steps (steady, not one spike).
Candidates are ranked by (5-minute rise + 15-minute rise); the top `max_candidates` go to the agents.
The first 15 minutes after the loop starts are a warm-up: there's no history yet, so no candidates.
"""

import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path


def update_tape(tape, tickers, now_epoch, history_minutes):
    """Add this minute's last prices; drop anything older than the history window."""
    cutoff = now_epoch - history_minutes * 60 - 30
    for symbol, row in tickers.items():
        tape.setdefault(symbol, []).append([now_epoch, row["last"]])
    for symbol in list(tape):
        tape[symbol] = [point for point in tape[symbol] if point[0] >= cutoff][-(history_minutes + 5):]
        if not tape[symbol]:
            del tape[symbol]
    return tape


def _price_at(points, epoch):
    """The last tape price at or before `epoch` (None if the tape doesn't reach back that far)."""
    best = None
    for at, price in points:
        if at <= epoch:
            best = price
        else:
            break
    return best


def _pct(new, old):
    try:
        old_d = Decimal(old)
        return None if old_d == 0 else (Decimal(new) - old_d) / old_d * 100
    except (InvalidOperation, TypeError):
        return None


def scan(tape, tickers, now_epoch, config):
    """-> (candidates, stats). Each candidate: symbol, rise_5m_pct, rise_15m_pct, up_minutes, volume."""
    skip = set(config["skip_bases"])
    min_volume = Decimal(config["min_volume_24h_usd"])
    rise5, rise15 = Decimal(config["min_rise_5m_pct"]), Decimal(config["min_rise_15m_pct"])
    stats = {"coins_priced": len(tickers), "liquid": 0, "with_history": 0, "candidates": 0}
    found = []
    for symbol, row in tickers.items():
        if row["base"] in skip or not row["tradeable"] or Decimal(row["volume_24h_usd"]) < min_volume:
            continue
        stats["liquid"] += 1
        points = tape.get(symbol, [])
        then5, then15 = _price_at(points, now_epoch - 300 + 30), _price_at(points, now_epoch - 900 + 30)
        if then5 is None or then15 is None or points[0][0] > now_epoch - 900 + 30:
            continue
        stats["with_history"] += 1
        last = row["last"]
        r5, r15 = _pct(last, then5), _pct(last, then15)
        recent = [p[1] for p in points if p[0] >= now_epoch - 330]
        ups = sum(1 for a, b in zip(recent, recent[1:]) if Decimal(b) > Decimal(a))
        if r5 is None or r15 is None or r5 < rise5 or r15 < rise15 or ups < config["min_up_minutes_of_last_5"]:
            continue
        found.append({"symbol": symbol, "base": row["base"], "rise_5m_pct": format(r5.quantize(Decimal("0.01")), "f"),
                      "rise_15m_pct": format(r15.quantize(Decimal("0.01")), "f"), "up_minutes_of_last_5": ups,
                      "volume_24h_usd": row["volume_24h_usd"], "_score": r5 + r15})
    found.sort(key=lambda c: (-c["_score"], c["symbol"]))
    for candidate in found:
        del candidate["_score"]
    stats["candidates"] = len(found)
    return found[: config["max_candidates"]], stats


def load_tape(path):
    try:
        tape = json.loads(Path(path).read_text(encoding="utf-8"))
        return tape if isinstance(tape, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def save_tape(path, tape):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as out:
        json.dump(tape, out, separators=(",", ":"))
        temporary = out.name
    os.replace(temporary, path)
