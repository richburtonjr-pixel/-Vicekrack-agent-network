"""Rule-based research strategies (Step 27). Pure functions: no I/O, clock or accounts.

Each rule sees `history`: the most recent processed bars (oldest first, current last). A
history item holds the bar's exact prices, the Step 26 indicator points published for that
bar, `run` (consecutive bars since the last gap, including this one), and VWAP session
information. Comparisons use exact bar prices and the *published* indicator values
(rounded half-even to 8 places), so every decision can be re-checked from the record.

Every rule compares the current bar t with the previous bar t-1 and needs them to be
consecutive (no missing interval between them). A gap that falls inside a rule's required
window makes it `not_ready` with `gap_invalidated`. Missing or unavailable inputs make it
`not_ready`. A not-ready rule can never trigger.

VWAP reclaim     trigger: close_(t-1) <= VWAP_(t-1)  and  close_t > VWAP_t
                 (inclusive at/below before, strict above now). Both bars in the same
                 session and inside the session window, VWAP ready on both. The first bar
                 of a session is not ready (`no_prior_bar_in_session`).
EMA crossover    trigger: fast_(t-1) <= slow_(t-1)  and  fast_t > slow_t, with fast < slow
                 periods and both EMAs ready on both bars.
Breakout(N)      level_t = max(high_(t-N) .. high_(t-1)); trigger: close_t > level_t (strict)
                 and NOT close_(t-1) > level_(t-1). Needs N + 2 consecutive bars.
                 Optional volume filter: volume_t >= multiplier * volume_sma(M)_(t-1)
                 (inclusive). The average is Step 26's volume SMA at the previous bar, so it
                 never includes bar t. An unavailable or zero average makes it not ready.
"""

from ..money import fmt, parse


def _value(point):
    return None if point is None or point["status"] != "ready" else parse(point["value"])


def _unavailable(*points):
    """`input_unavailable` plus the indicators' own reasons (e.g. indicator_warming_up)."""
    reasons = ["input_unavailable"]
    for point in points:
        if point is not None and point["status"] != "ready":
            for code in point["reason_codes"]:
                if f"indicator_{code}" not in reasons:
                    reasons.append(f"indicator_{code}")
    return reasons[:10]


def _text(value):
    return None if value is None else fmt(value)


def _not_ready(reasons, values):
    return "not_ready", reasons, values, None


def _pair_ready(history, needed):
    """(ok, reasons) for needing `needed` consecutive bars ending at the current bar."""
    current = history[-1]
    if current["run"] >= needed:
        return True, []
    if current["total"] >= needed:
        return False, ["gap_invalidated"]
    return False, ["insufficient_history"]


def vwap_reclaim(params, history, keys):
    current = history[-1]
    previous = history[-2] if len(history) > 1 else None
    values = {"close": _text(current["close"]), "vwap": None, "previous_close": None, "previous_vwap": None}
    if not current["inside"]:
        return _not_ready(["outside_session"], values)
    vwap = _value(current["points"][keys["vwap"]])
    values["vwap"] = _text(vwap)
    if previous is not None:
        values["previous_close"] = _text(previous["close"])
    if previous is None or previous["session"] != current["session"] or not previous["inside"]:
        return _not_ready(["no_prior_bar_in_session"], values)
    if current["run"] < 2:
        return _not_ready(["gap_invalidated"], values)
    previous_vwap = _value(previous["points"][keys["vwap"]])
    values["previous_vwap"] = _text(previous_vwap)
    if vwap is None or previous_vwap is None:
        return _not_ready(_unavailable(previous["points"][keys["vwap"]], current["points"][keys["vwap"]]), values)
    was_at_or_below = previous["close"] <= previous_vwap
    above = current["close"] > vwap
    if was_at_or_below and above:
        return "triggered", ["reclaimed_vwap"], values, "vwap_reclaim"
    return "not_triggered", ["already_above_vwap" if above else "at_or_below_vwap"], values, None


def ema_crossover(params, history, keys):
    current = history[-1]
    values = {"fast": None, "slow": None, "previous_fast": None, "previous_slow": None}
    ok, reasons = _pair_ready(history, 2)
    fast, slow = _value(current["points"][keys["fast"]]), _value(current["points"][keys["slow"]])
    values.update(fast=_text(fast), slow=_text(slow))
    if not ok:
        return _not_ready(reasons, values)
    previous = history[-2]
    previous_fast, previous_slow = _value(previous["points"][keys["fast"]]), _value(previous["points"][keys["slow"]])
    values.update(previous_fast=_text(previous_fast), previous_slow=_text(previous_slow))
    if None in (fast, slow, previous_fast, previous_slow):
        return _not_ready(_unavailable(*(item["points"][keys[role]] for item in (previous, current)
                                         for role in ("fast", "slow"))), values)
    if previous_fast <= previous_slow and fast > slow:
        return "triggered", ["crossed_above"], values, "ema_cross_above"
    return "not_triggered", ["already_above" if fast > slow else "no_cross"], values, None


def breakout(params, history, keys):
    lookback = params["lookback"]
    current = history[-1]
    values = {"close": _text(current["close"]), "level": None, "previous_close": None, "previous_level": None}
    if params["volume_filter"] is not None:
        values.update(volume=_text(current["volume"]), volume_average=None, volume_threshold=None)
    ok, reasons = _pair_ready(history, lookback + 2)
    if not ok:
        return _not_ready(reasons, values)
    window = history[-(lookback + 2):]
    level = max(item["high"] for item in window[1:-1])
    previous_level = max(item["high"] for item in window[:-2])
    previous_close = window[-2]["close"]
    values.update(level=_text(level), previous_close=_text(previous_close), previous_level=_text(previous_level))
    broke, was_above = current["close"] > level, previous_close > previous_level
    if not broke:
        return "not_triggered", ["below_or_at_level"], values, None
    if was_above:
        return "not_triggered", ["already_above_level"], values, None
    volume_filter = params["volume_filter"]
    if volume_filter is not None:
        average = _value(window[-2]["points"][keys["volume"]])
        values["volume_average"] = _text(average)
        if average is None:
            return _not_ready(["volume_average_unavailable"] + _unavailable(window[-2]["points"][keys["volume"]])[1:], values)
        if average == 0:
            return _not_ready(["volume_average_zero"], values)
        threshold = parse(volume_filter["multiplier"]) * average
        values["volume_threshold"] = _text(threshold)
        if current["volume"] < threshold:
            return "not_triggered", ["volume_filter_failed"], values, None
    return "triggered", ["closed_above_level"] + (["volume_filter_passed"] if volume_filter else []), values, "breakout_above"


RULES = {"vwap_reclaim": vwap_reclaim, "ema_crossover": ema_crossover, "breakout": breakout}


def history_needed(definition):
    params = definition["params"]
    return params["lookback"] + 2 if definition["strategy"] == "breakout" else 2


def required_indicators(definition):
    """{role: (kind, period)} for the Step 26 indicators a strategy reads."""
    params = definition["params"]
    if definition["strategy"] == "vwap_reclaim":
        return {"vwap": ("vwap", None)}
    if definition["strategy"] == "ema_crossover":
        return {"fast": ("ema", params["fast"]), "slow": ("ema", params["slow"])}
    if params["volume_filter"] is not None:
        return {"volume": ("volume_sma", params["volume_filter"]["period"])}
    return {}
