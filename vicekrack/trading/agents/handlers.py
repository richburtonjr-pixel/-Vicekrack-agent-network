"""Deterministic local role handlers (Step 28). Pure rules: no I/O, clock, providers or accounts.

Each handler receives (evidence_slice, prior_handoffs) and returns an `agent_output`:
conclusion, summary, findings, reason_codes, limitations. It has no reference to the
controller, cannot start tasks, choose a successor or retry; its output schema has no
field for any of that.

Market Scout   bars closed by T, last bar, age = T - last bar close; stale when age >
               freshness_max_intervals x interval. Gaps inside the window. Session: whether
               the last closed bar lies inside the configured session window (no calendar).
               conclusion: data_available | insufficient_data (fewer than min_bars closed)
               | stale_data.
Trend Agent    Uses only indicator points published for the LAST closed bar.
               EMA: fast > slow -> uptrend, fast < slow -> downtrend, equal -> flat.
               RSI: >= overbought -> overbought_zone, <= oversold -> oversold_zone, else
               neutral_zone (inclusive bounds). Close vs VWAP: above / below / at.
               Volume: last volume >= multiple x volume average (at the same bar) -> elevated,
               else normal. Unready inputs -> undetermined, never guessed.
               conclusion: trend_assessed (EMA trend determined) | insufficient_indicators.
Strategy Agent Signals with expires_at_utc > T and not expired_when_detected are active;
               others are listed as expired, never as active. Conflicts with the trend
               handoff: active bullish signal while EMA trend is downtrend
               (signal_against_trend) or RSI is in the overbought zone
               (signal_in_overbought_zone). Several active signals on the same bar from
               different strategies are corroborating.
               conclusion: active_research_signal | conflicting_signals | no_active_signal.
Risk Review    Research review only (not order authorization; the paper risk engine is
               never called). Sufficient only when: data available and fresh, no gap in the
               recent window, trend assessed, an active non-conflicting research signal.
               conclusion: sufficient_for_future_paper_evaluation |
               insufficient_for_future_paper_evaluation.
"""

from ..market.bars import from_utc_text, zone
from ..money import parse

COMMON_LIMITATIONS = [
    "Deterministic rules on historical data at a simulated time; not current market data.",
    "Data is not verified as authentic, current or licensed.",
    "Research only: no order authorization, no account access.",
]


def _value(point):
    return None if not point or point["status"] != "ready" else parse(point["value"])


def _prior(prior, role):
    return next((h for h in prior if h["role"] == role), None)


def market_scout(evidence, prior):
    bars, gaps = evidence["window_bars"], evidence["window_gaps"]
    findings = {"closed_bars_total": evidence["closed_bars_total"], "window_bars": len(bars),
                "data_label": evidence["dataset"]["data_label"], "symbol": evidence["dataset"]["symbol"],
                "interval": evidence["interval"], "verification_authentic": evidence["verification"]["authentic"],
                "verification_current": evidence["verification"]["current"],
                "verification_licensed": evidence["verification"]["licensed"],
                "window_gaps": len(gaps), "window_missing_intervals": sum(g["missing_intervals"] for g in gaps),
                "last_bar_sequence": None, "last_bar_close_utc": None, "last_close": None, "age_seconds": None,
                "session_timezone": evidence["session"]["timezone"], "last_bar_in_session": None}
    reasons = [f"data_label_{evidence['dataset']['data_label']}", "data_not_verified"]
    limitations = list(COMMON_LIMITATIONS)
    if gaps:
        reasons.append("gaps_in_window")
        limitations.append("Missing intervals inside the window; no prices were filled in.")
    if not bars or evidence["closed_bars_total"] < evidence["min_bars"]:
        reasons.append("too_few_closed_bars")
        return {"conclusion": "insufficient_data", "findings": findings, "reason_codes": reasons, "limitations": limitations,
                "summary": f"Only {evidence['closed_bars_total']} closed bar(s) at the simulated time; "
                           f"at least {evidence['min_bars']} are required."}
    last = bars[-1]
    age = int((from_utc_text(evidence["sim_time_utc"]) - from_utc_text(last["available_at_utc"])).total_seconds())
    findings.update(last_bar_sequence=last["sequence"], last_bar_close_utc=last["available_at_utc"],
                    last_close=last["close"], age_seconds=age, last_bar_in_session=_in_session(last, evidence["session"]))
    if findings["last_bar_in_session"] is False:
        reasons.append("last_bar_outside_session")
    if age > evidence["freshness_max_intervals"] * evidence["interval_seconds"]:
        reasons.append("data_stale_at_sim_time")
        return {"conclusion": "stale_data", "findings": findings, "reason_codes": reasons, "limitations": limitations,
                "summary": f"The last closed bar closed {age} seconds before the simulated time, beyond the "
                           f"{evidence['freshness_max_intervals']}-interval freshness limit."}
    return {"conclusion": "data_available", "findings": findings, "reason_codes": reasons, "limitations": limitations,
            "summary": f"{evidence['closed_bars_total']} closed {evidence['interval']} bars for "
                       f"{evidence['dataset']['symbol']} by the simulated time; last close {last['close']} at "
                       f"{last['available_at_utc']} ({len(gaps)} gap(s) in the last {len(bars)} bars)."}


def _in_session(bar, session):
    tz = zone(session["timezone"])
    start = from_utc_text(bar["timestamp_utc"]).astimezone(tz)
    end = from_utc_text(bar["available_at_utc"]).astimezone(tz)
    begin = int(session["start"][:2]) * 60 + int(session["start"][3:])
    finish = int(session["end"][:2]) * 60 + int(session["end"][3:])
    return (start.date() == end.date() and start.hour * 60 + start.minute >= begin
            and end.hour * 60 + end.minute <= finish)


def trend_agent(evidence, prior):
    keys, points, last = evidence["keys"], evidence["indicators"], evidence["last_bar"]
    reasons, limitations = [], list(COMMON_LIMITATIONS) + ["Descriptive indicator reading, not a forecast."]
    findings = {"ema_trend": "undetermined", "rsi_zone": "undetermined", "vwap_position": "undetermined",
                "volume": "undetermined", "ema_fast": None, "ema_slow": None, "rsi": None, "vwap": None,
                "volume_average": None}

    def current(key):
        point = points.get(key) if key else None
        if point is None:
            reasons.append(f"missing_{key or 'vwap'}"[:60])
            return None
        if last is None or point["sequence"] != last["sequence"]:
            reasons.append("indicator_not_for_last_bar")
            return None
        if point["status"] != "ready":
            reasons.append(f"{key}_unavailable"[:60])
            return None
        return parse(point["value"])

    fast, slow = current(keys["ema_fast"]), current(keys["ema_slow"])
    if fast is not None and slow is not None:
        findings.update(ema_fast=points[keys["ema_fast"]]["value"], ema_slow=points[keys["ema_slow"]]["value"],
                        ema_trend="uptrend" if fast > slow else "downtrend" if fast < slow else "flat")
    rsi = current(keys["rsi"])
    if rsi is not None:
        thresholds = evidence["thresholds"]
        findings.update(rsi=points[keys["rsi"]]["value"],
                        rsi_zone="overbought_zone" if rsi >= parse(thresholds["rsi_overbought"])
                        else "oversold_zone" if rsi <= parse(thresholds["rsi_oversold"]) else "neutral_zone")
    if keys["vwap"] is not None:
        vwap = current(keys["vwap"])
        if vwap is not None:
            close = parse(last["close"])
            findings.update(vwap=points[keys["vwap"]]["value"],
                            vwap_position="above_vwap" if close > vwap else "below_vwap" if close < vwap else "at_vwap")
    else:
        reasons.append("vwap_not_applicable")
    average = current(keys["volume_sma"])
    if average is not None:
        findings["volume_average"] = points[keys["volume_sma"]]["value"]
        findings["volume"] = ("elevated" if parse(last["volume"]) >= parse(evidence["thresholds"]["volume_elevated_multiple"]) * average
                              else "normal")
    reasons = list(dict.fromkeys(reasons))[:20]
    if findings["ema_trend"] == "undetermined":
        return {"conclusion": "insufficient_indicators", "findings": findings, "reason_codes": reasons or ["ema_unavailable"],
                "limitations": limitations, "summary": "EMA trend could not be determined from ready indicators at the "
                                                       "simulated time; nothing was guessed."}
    return {"conclusion": "trend_assessed", "findings": findings, "reason_codes": reasons, "limitations": limitations,
            "summary": f"EMA trend {findings['ema_trend']}; RSI {findings['rsi_zone']}; close {findings['vwap_position']}; "
                       f"volume {findings['volume']} (values at the last closed bar)."}


def strategy_agent(evidence, prior):
    sim_time = evidence["sim_time_utc"]
    trend = _prior(prior, "trend_agent")
    active, expired = [], []
    for signal in evidence["signals"]:
        if signal["detected_at_sim_utc"] > sim_time:          # cannot happen; evidence ends at T
            continue
        if signal["expires_at_utc"] > sim_time and not signal["expired_when_detected"]:
            active.append(signal)
        else:
            expired.append(signal)
    reasons, conflicts = [], []
    findings = {"active_signals": [f"{s['strategy']['name']}:{s['signal_id']}" for s in active][:50],
                "expired_signals": len(expired), "latest_outcomes": [f"{name}:{e['outcome'] if e else 'none'}"
                                                                  for name, e in sorted(evidence["latest_evaluations"].items())],
                "corroborating": False, "conflicts": [], "supporting_values": [],
                "signals_truncated": evidence["signals_truncated"]}
    for signal in active:
        values = ",".join(f"{k}={v}" for k, v in sorted(signal["supporting_values"].items()) if v is not None)
        findings["supporting_values"].append(f"{signal['strategy']['name']}@{signal['bar']['sequence']}: {values}"[:200])
    trend_findings = trend["findings"] if trend and trend["status"] == "completed" else {}
    if active:
        if trend_findings.get("ema_trend") == "downtrend":
            conflicts.append("signal_against_trend")
        if trend_findings.get("rsi_zone") == "overbought_zone":
            conflicts.append("signal_in_overbought_zone")
        if not trend_findings or trend_findings.get("ema_trend") == "undetermined":
            reasons.append("trend_context_unavailable")
        bars = {}
        for signal in active:
            bars.setdefault(signal["bar"]["sequence"], set()).add(signal["strategy"]["name"])
        findings["corroborating"] = any(len(names) > 1 for names in bars.values())
        if findings["corroborating"]:
            reasons.append("corroborating_signals_same_bar")
    if expired:
        reasons.append("expired_signals_not_actionable")
    findings["conflicts"] = conflicts
    limitations = list(COMMON_LIMITATIONS) + ["Research signals are rule matches, not predictions; expired signals are history only."]
    if active and conflicts:
        return {"conclusion": "conflicting_signals", "findings": findings, "reason_codes": reasons + conflicts,
                "limitations": limitations, "summary": f"{len(active)} active research signal(s) conflict with the trend "
                                                       f"reading ({', '.join(conflicts)})."}
    if active:
        return {"conclusion": "active_research_signal", "findings": findings, "reason_codes": reasons or ["active_signal"],
                "limitations": limitations, "summary": f"{len(active)} active research signal(s) at the simulated time; "
                                                       f"{len(expired)} earlier signal(s) expired."}
    return {"conclusion": "no_active_signal", "findings": findings, "reason_codes": reasons or ["no_signals"],
            "limitations": limitations, "summary": f"No unexpired research signal at the simulated time "
                                                   f"({len(expired)} expired signal(s) listed as history only)."}


def risk_review(evidence, prior):
    scout, trend, strategy = (_prior(prior, r) for r in ("market_scout", "trend_agent", "strategy_agent"))
    blockers = []
    if not scout or scout["conclusion"] != "data_available":
        blockers.append("market_data_insufficient_or_stale")
    elif scout["findings"].get("window_gaps"):
        blockers.append("recent_gaps_in_data")
    if not trend or trend["conclusion"] != "trend_assessed":
        blockers.append("trend_not_assessed")
    if not strategy or strategy["conclusion"] == "no_active_signal":
        blockers.append("no_active_research_signal")
    elif strategy["conclusion"] == "conflicting_signals":
        blockers.append("conflicting_research_signals")
    findings = {"checks_failed": len(blockers), "blockers": blockers, "authorization_performed": False,
                "paper_risk_engine_called": False, "data_label": scout["findings"].get("data_label") if scout else None}
    limitations = list(COMMON_LIMITATIONS) + [
        "This review checks whether research inputs are complete; it is not a risk decision and authorizes nothing.",
        "A future paper evaluation would still need fresh data and every paper-account risk check."]
    if blockers:
        return {"conclusion": "insufficient_for_future_paper_evaluation", "findings": findings, "reason_codes": blockers,
                "limitations": limitations, "summary": "The research package is not sufficient for a future paper "
                                                       f"evaluation: {', '.join(blockers)}."}
    return {"conclusion": "sufficient_for_future_paper_evaluation", "findings": findings,
            "reason_codes": ["research_inputs_complete"], "limitations": limitations,
            "summary": "Research inputs are complete and consistent enough to be considered by a future paper "
                       "evaluation. No order was authorized."}


class Handler:
    """A role handler: deterministic `analyze(evidence_slice, prior_handoffs) -> agent_output`."""

    def __init__(self, role, function, version="1.0"):
        self.role, self.version, self._function = role, version, function

    @property
    def identity(self):
        return f"{self.role}@{self.version}"

    def analyze(self, evidence, prior):
        """The handler's output stamped with the agent_output contract version (1.0)."""
        output = self._function(evidence, prior)
        return {"version": "1.0", **output} if isinstance(output, dict) else output


def default_handlers():
    return [Handler("market_scout", market_scout), Handler("trend_agent", trend_agent),
            Handler("strategy_agent", strategy_agent), Handler("risk_review", risk_review)]
