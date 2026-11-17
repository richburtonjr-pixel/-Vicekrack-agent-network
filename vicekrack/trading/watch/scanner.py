"""The radar: the Market Scout's whole-market scan. Plain code, no AI.

Every minute the watch loop prices every Kraken USD coin in one request and adds it to the TAPE:
the last `history_minutes` of prices (and today's traded volume) per coin (runtime/trading/watch/tape.json).

A coin must first pass the gates:
  - it trades enough: 24-hour volume of at least `min_volume_24h_usd` (thin coins jump around and
    their buy/sell gap eats you alive), and it isn't a stablecoin or a currency (`skip_bases`),
  - its buy/sell gap (spread) is at most `max_spread_pct`,
  - it is NOT already pumped: up less than `max_change_today_pct` since 00:00 UTC (buying a coin that
    already ran +40% today is chasing, not spotting it early).
Then it becomes a CANDIDATE in one of two ways:
  STREAK  up at least `min_rise_5m_pct` over 5 minutes AND `min_rise_15m_pct` over 15 minutes, rising in at
          least `min_up_minutes_of_last_5` of the last 5 one-minute steps (steady, not one spike).
  EARLY   a VOLUME SPIKE: the coin traded at least `min_volume_spike` times its normal 5-minute volume in the
          last 5 minutes, while the price is up at least `early_min_rise_5m_pct` over 5 minutes. Unusual
          buying often shows up in volume before the price move is obvious.
Candidates are ranked by an EARLY-MOMENTUM SCORE: 5-minute rise + half the 15-minute rise, plus a bonus for
the volume spike (capped) and for ACCELERATION (the last 5 minutes rose faster than the 10 before them).
Each candidate lists its `signals` so the agents and the app can show why it was picked.
The top `max_candidates` go to the agents. The first 15 minutes after the loop starts are a warm-up.
This finds coins that are moving; it cannot know which will keep going. The early-or-late scorecard
measures how often the picks came before the big move.
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
        point = [now_epoch, row["last"]]
        if row.get("volume_today_base") is not None:
            point.append(row["volume_today_base"])
        tape.setdefault(symbol, []).append(point)
    for symbol in list(tape):
        tape[symbol] = [point for point in tape[symbol] if point[0] >= cutoff][-(history_minutes + 5):]
        if not tape[symbol]:
            del tape[symbol]
    return tape


def _price_at(points, epoch, field=1):
    """The last tape value at or before `epoch` (None if the tape doesn't reach back that far)."""
    best = None
    for point in points:
        if point[0] <= epoch:
            best = point[field] if len(point) > field else None
        else:
            break
    return best


def _volume_spike(points, row, now_epoch):
    """Base volume traded in the last 5 minutes / the 24-hour average 5 minutes. None if unknown.

    Kraken's "volume today" resets at 00:00 UTC, so a drop means the day rolled over: no reading then."""
    then = _price_at(points, now_epoch - 300 + 30, field=2)
    now = row.get("volume_today_base")
    day = row.get("volume_24h_base")
    try:
        if then is None or now is None or day is None or Decimal(day) <= 0:
            return None
        recent = Decimal(now) - Decimal(then)
        if recent < 0:
            return None
        return recent / (Decimal(day) / 288)
    except (InvalidOperation, TypeError):
        return None


def _pct(new, old):
    try:
        old_d = Decimal(old)
        return None if old_d == 0 else (Decimal(new) - old_d) / old_d * 100
    except (InvalidOperation, TypeError):
        return None


def scan(tape, tickers, now_epoch, config):
    """-> (candidates, stats). Each candidate: symbol, base, rise_5m_pct, rise_15m_pct, up_minutes_of_last_5,
    volume_24h_usd, change_today_pct, spread_pct, volume_spike_x, accelerating, kind, signals, score."""
    skip = set(config["skip_bases"])
    min_volume = Decimal(config["min_volume_24h_usd"])
    rise5, rise15 = Decimal(config["min_rise_5m_pct"]), Decimal(config["min_rise_15m_pct"])
    max_spread = Decimal(config.get("max_spread_pct", "100"))
    max_today = Decimal(config.get("max_change_today_pct", "100000"))
    min_spike = Decimal(config.get("min_volume_spike", "1000000"))
    early5 = Decimal(config.get("early_min_rise_5m_pct", "0"))
    stats = {"coins_priced": len(tickers), "liquid": 0, "with_history": 0, "candidates": 0,
             "skipped_already_pumped": 0, "skipped_wide_spread": 0, "volume_spikes": 0}
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
        if r5 is None or r15 is None:
            continue
        ask, bid = Decimal(row["ask"]), Decimal(row["bid"])
        spread = (ask - bid) / ((ask + bid) / 2) * 100 if ask + bid > 0 else Decimal(100)
        today = Decimal(row["change_today_pct"]) if row.get("change_today_pct") is not None else None
        recent = [p[1] for p in points if p[0] >= now_epoch - 330]
        ups = sum(1 for a, b in zip(recent, recent[1:]) if Decimal(b) > Decimal(a))
        spike = _volume_spike(points, row, now_epoch)
        if spike is not None and spike >= min_spike:
            stats["volume_spikes"] += 1
        streak = r5 >= rise5 and r15 >= rise15 and ups >= config["min_up_minutes_of_last_5"]
        early = spike is not None and spike >= min_spike and r5 >= early5
        if not (streak or early):
            continue
        if spread > max_spread:
            stats["skipped_wide_spread"] += 1
            continue
        if today is not None and today >= max_today:
            stats["skipped_already_pumped"] += 1
            continue
        accelerating = r5 > (r15 - r5) / 2 and r5 > 0          # last 5 min faster than the 10 before, per 5 min
        signals = (["streak"] if streak else []) + (["volume_spike"] if early else []) + (["accelerating"] if accelerating else [])
        score = r5 + r15 / 2 + (min(spike, Decimal(10)) * Decimal("0.25") if spike is not None and spike >= min_spike else 0) \
            + (Decimal("0.5") if accelerating else 0)
        found.append({"symbol": symbol, "base": row["base"], "rise_5m_pct": format(r5.quantize(Decimal("0.01")), "f"),
                      "rise_15m_pct": format(r15.quantize(Decimal("0.01")), "f"), "up_minutes_of_last_5": ups,
                      "volume_24h_usd": row["volume_24h_usd"],
                      "change_today_pct": row.get("change_today_pct"),
                      "spread_pct": format(spread.quantize(Decimal("0.01")), "f"),
                      "volume_spike_x": None if spike is None else format(spike.quantize(Decimal("0.1")), "f"),
                      "accelerating": accelerating, "kind": "streak" if streak else "early", "signals": signals,
                      "score": format(score.quantize(Decimal("0.01")), "f"), "_score": score})
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
