"""Verifier agent (Trading PR 3): a second opinion that does its own math.

The Strategy agent says "breakout-3 fired on bar 120". The Verifier does not trust that. It
takes the raw price bars (not the indicator engine's numbers) and checks for itself:

  1. fresh data      the last bar closed recently (`max_bar_age_seconds`)
  2. no holes        no missing bars in the last `gap_check_bars`
  3. fresh signal    the claimed signal is on the LAST closed bar
  4. same answer     at least one claimed signal is recomputed from raw closes/highs and matches:
                       ema-cross-F-S : its own EMA(F) crossed above its own EMA(S) on that bar
                       breakout-N    : close > highest high of the N bars before, and the bar
                                       before had not already broken out
                     Rules it can't recompute independently (VWAP reclaim) don't count.
  5. no spike        the last bar didn't move more than `max_last_bar_move_pct` percent

Any failed check is a veto. Plain deterministic code, exact decimals, no AI.
"""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

CLAIM = re.compile(r"^([a-z0-9-]+)@([0-9]+):")


def _ctx(context):
    context.prec = 50


def _time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def ema_series(values, period):
    """EMA seeded with the simple average of the first `period` values; None until then."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    with localcontext() as context:
        _ctx(context)
        alpha = Decimal(2) / Decimal(period + 1)
        current = sum(values[:period]) / Decimal(period)
        out[period - 1] = current
        for index in range(period, len(values)):
            current = alpha * values[index] + (1 - alpha) * current
            out[index] = current
    return out


def _ema_cross_confirmed(closes, fast, slow):
    f, s = ema_series(closes, fast), ema_series(closes, slow)
    if len(closes) < slow + 1 or None in (f[-1], s[-1], f[-2], s[-2]):
        return False
    return f[-2] <= s[-2] and f[-1] > s[-1]


def _breakout_confirmed(highs, closes, lookback):
    if len(closes) < lookback + 2:
        return False
    level = max(highs[-1 - lookback:-1])
    previous_level = max(highs[-2 - lookback:-2])
    return closes[-1] > level and not closes[-2] > previous_level


def parse_claims(strategy_findings):
    claims = []
    for text in (strategy_findings or {}).get("supporting_values", []):
        match = CLAIM.match(text)
        if match:
            claims.append((match.group(1), int(match.group(2))))
    return claims


def verify(bars, interval_minutes, strategy_findings, signal_config, now, config):
    """Return the Verifier's signature: {"agent", "decision": approve|veto, "reasons", "summary"}."""
    reasons, confirmed = [], []
    width = timedelta(minutes=interval_minutes)
    last_close_at = _time(bars[-1]["start_utc"]) + width
    if (_time(now) - last_close_at).total_seconds() > config["max_bar_age_seconds"]:
        reasons.append("data_too_old")
    window = bars[-config["gap_check_bars"]:]
    for before, after in zip(window, window[1:]):
        if _time(after["start_utc"]) - _time(before["start_utc"]) != width:
            reasons.append("missing_bars")
            break
    with localcontext() as context:
        _ctx(context)
        closes = [Decimal(b["close"]) for b in bars]
        highs = [Decimal(b["high"]) for b in bars]
        last_open = Decimal(bars[-1]["open"])
        move = abs(closes[-1] - last_open) / last_open * 100 if last_open else Decimal(0)
    if move > Decimal(config["max_last_bar_move_pct"]):
        reasons.append("last_bar_spike")
    claims = parse_claims(strategy_findings)
    if not claims:
        reasons.append("no_claim_to_check")
    for name, sequence in claims:
        if sequence != len(bars):
            reasons.append("signal_not_on_last_bar")
            continue
        definition = signal_config["strategies"].get(name)
        if definition is None:
            continue
        params = definition["params"]
        if definition["strategy"] == "ema_crossover" and _ema_cross_confirmed(closes, params["fast"], params["slow"]):
            confirmed.append(name)
        elif definition["strategy"] == "breakout" and params.get("volume_filter") is None \
                and _breakout_confirmed(highs, closes, params["lookback"]):
            confirmed.append(name)
    if claims and not confirmed:
        reasons.append("signal_not_independently_confirmed")
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        return {"agent": "verifier", "decision": "veto", "reasons": reasons,
                "summary": "Rechecked from raw prices and disagreed: " + ", ".join(reasons) + "."}
    return {"agent": "verifier", "decision": "approve", "reasons": ["recomputed_" + n for n in confirmed],
            "summary": f"Rechecked from raw prices: {', '.join(confirmed)} confirmed on the last closed bar; "
                       f"data fresh, no missing bars, no spike."}
