"""Indicator engine (Step 26): a Step 25 replay consumer over closed bars only.

`calculate` drives the bounded replay (`market.replay.drive`). At each simulated time the
IndicatorConsumer receives a ReplayView that already contains only closed bars, processes
the bars it has not seen yet in sequence order, and records one point per bar per
indicator. If a step ever exposes more new bars than the view window holds, the run stops
with `indicator_window_too_small` rather than skipping bars.

Gaps (missing intervals between consecutive bars, no market calendar):
- `reset` (default): EMA, RSI and volume SMA restart warm-up after the gap; VWAP is
  unavailable (`session_gap`) for the rest of that session and restarts next session.
- `continue`: calculations carry on as if bars were consecutive; affected values carry
  `gap_ignored` (EMA/RSI/SMA from the gap onward; VWAP for the rest of that session).
With no calendar, overnight breaks in intraday data are gaps, so under `reset` intraday
EMA/RSI/SMA restart every day.

VWAP sessions: a bar belongs to the session of its local date (session timezone) when its
start is at or after `start` and its close is at or before `end`. A new local date starts
a new session. Bars outside the window are `outside_session`.

This module never imports paper-account, risk, order or journal code.
"""

import re

from ..contracts import sha256, validate_schema
from ..errors import TradingError
from ..market.bars import INTERVAL_SECONDS, from_utc_text, missing_between, zone
from ..market.replay import drive
from ..money import fmt, parse
from .formulas import EMA, SessionVWAP, VolumeSMA, WilderRSI

NOTICE = ("Offline indicator values computed from closed historical bars on a simulation clock. Not a signal, not "
          "advice, no orders, no account access. Data is not verified as authentic, current or licensed.")
KINDS = {"ema": (EMA, 1), "rsi": (WilderRSI, 2), "volume_sma": (VolumeSMA, 1)}
HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
PASSTHROUGH = {"indicator_window_too_small", "indicator_too_many_points"}


def _minutes(text, name):
    if not isinstance(text, str) or not HHMM.match(text):
        raise TradingError("invalid_indicator_settings", f"VWAP session {name} must be HH:MM (24-hour, two digits).")
    return int(text[:2]) * 60 + int(text[3:])


def build_settings(dataset, *, config, ema=(), rsi=(), volume_sma=(), vwap=False, gap_policy=None,
                   vwap_session=None):
    """Validated, canonical settings for one calculation."""
    limits = config["limits"]
    specs = []
    for kind, periods in (("ema", ema), ("rsi", rsi), ("volume_sma", volume_sma)):
        minimum = KINDS[kind][1]
        for period in periods or ():
            if isinstance(period, bool) or not isinstance(period, int) or not minimum <= period <= limits["max_period"]:
                raise TradingError("invalid_indicator_settings",
                                   f"{kind} period must be a whole number from {minimum} to {limits['max_period']}.")
            specs.append({"key": f"{kind}_{period}", "kind": kind, "period": period})
    session = None
    if vwap:
        if INTERVAL_SECONDS[dataset["interval"]] is None:
            raise TradingError("invalid_indicator_settings", "Session VWAP needs intraday bars, not daily bars.")
        session = dict(vwap_session or config["defaults"]["vwap_session"])
        if set(session) != {"timezone", "start", "end"}:
            raise TradingError("invalid_indicator_settings", "VWAP session needs timezone, start and end.")
        zone(session["timezone"])
        if _minutes(session["start"], "start") >= _minutes(session["end"], "end"):
            raise TradingError("invalid_indicator_settings", "VWAP session start must be before its end (same day).")
        specs.append({"key": "vwap_session", "kind": "vwap", "period": None})
    if not specs:
        raise TradingError("invalid_indicator_settings", "Choose at least one indicator.")
    keys = [s["key"] for s in specs]
    if len(keys) != len(set(keys)):
        raise TradingError("invalid_indicator_settings", "Each indicator and period may appear only once.")
    if len(specs) > limits["max_indicators"]:
        raise TradingError("invalid_indicator_settings", "Too many indicators for config/indicators.json.")
    policy = gap_policy or config["defaults"]["gap_policy"]
    if policy not in ("reset", "continue"):
        raise TradingError("invalid_indicator_settings", "gap_policy must be reset or continue.")
    settings = {"indicators": specs, "gap_policy": policy, "vwap_session": session, "rounding": dict(config["rounding"])}
    settings["settings_sha256"] = sha256(settings)
    return settings


class IndicatorConsumer:
    name = "indicators"

    def __init__(self, dataset, settings, max_points):
        self.dataset, self.settings, self.max_points = dataset, settings, max_points
        self.interval, self.tz = dataset["interval"], dataset["timezone"]
        self.calcs, self.series = {}, {}
        self.vwap = None
        for spec in settings["indicators"]:
            if spec["kind"] == "vwap":
                self.vwap = SessionVWAP()
                session = settings["vwap_session"]
                self.session_zone = zone(session["timezone"])
                self.session_start, self.session_end = _minutes(session["start"], "start"), _minutes(session["end"], "end")
            else:
                self.calcs[spec["key"]] = KINDS[spec["kind"]][0](spec["period"])
            self.series[spec["key"]] = []
        self.last_sequence, self.previous_start = 0, None
        self.gap_ignored = False
        self.points = self.gaps = self.resets = self.sessions = self.future_attempts = 0

    # ------------------------------------------------------------------ replay hook
    def on_step(self, view):
        if view.visible_count <= self.last_sequence:
            return
        new = [bar for bar in view.bars() if bar["sequence"] > self.last_sequence]
        if not new or new[0]["sequence"] != self.last_sequence + 1:
            raise TradingError("indicator_window_too_small",
                               "More new bars arrived in one step than the replay window holds; use a smaller step.")
        for bar in new:
            if bar["available_at_utc"] > view.now:                      # defensive; the view never allows it
                raise TradingError("future_bar_leak", "A future bar reached the indicator engine.")
            self._process(bar, view.now)
            self.last_sequence = bar["sequence"]

    def final(self):
        return {"bars_processed": self.last_sequence}

    # ------------------------------------------------------------------ per bar
    def _session_of(self, start_utc, available_utc):
        local_start = from_utc_text(start_utc).astimezone(self.session_zone)
        local_end = from_utc_text(available_utc).astimezone(self.session_zone)
        start_minutes = local_start.hour * 60 + local_start.minute
        end_minutes = local_end.hour * 60 + local_end.minute
        inside = (local_end.date() == local_start.date() and start_minutes >= self.session_start
                  and end_minutes <= self.session_end)
        return local_start.date().isoformat(), inside

    def _process(self, bar, now):
        start = from_utc_text(bar["timestamp_utc"])
        gap = False
        if self.previous_start is not None:
            gap = missing_between(self.previous_start, start, self.interval, self.tz, "indicator") > 0
        self.previous_start = start
        policy = self.settings["gap_policy"]
        if gap:
            self.gaps += 1
            if policy == "reset":
                for calc in self.calcs.values():
                    calc.reset()
                self.resets += 1
            else:
                self.gap_ignored = True
        close, volume = parse(bar["close"]), parse(bar["volume"])
        results = {}
        for key, calc in self.calcs.items():
            value, reasons = calc.update(volume if calc.kind == "volume_sma" else close)
            if policy == "continue" and self.gap_ignored:
                reasons = reasons + ["gap_ignored"]
            results[key] = (value, reasons, calc.count)
        if self.vwap is not None:
            session, inside = self._session_of(bar["timestamp_utc"], bar["available_at_utc"])
            if gap and self.vwap.session == session:
                if policy == "reset":
                    self.vwap.broken = True
                else:
                    self.vwap.gap_ignored = True
            if not inside:
                results["vwap_session"] = (None, ["outside_session"], 0)
            else:
                if self.vwap.session != session:
                    self.vwap.start_session(session)
                    self.sessions += 1
                value, reasons = self.vwap.update(parse(bar["high"]), parse(bar["low"]), close, volume)
                results["vwap_session"] = (value, reasons, self.vwap.count)
        for key, (value, reasons, count) in results.items():
            self.points += 1
            if self.points > self.max_points:
                raise TradingError("indicator_too_many_points", "This calculation exceeds max_points in config/indicators.json.")
            self.series[key].append({
                "sequence": bar["sequence"], "timestamp": bar["timestamp"], "timestamp_utc": bar["timestamp_utc"],
                "available_at_utc": bar["available_at_utc"], "computed_at_sim_utc": now,
                "status": "ready" if value is not None else "unavailable",
                "value": fmt(value) if value is not None else None, "reason_codes": list(dict.fromkeys(reasons)),
                "bars_since_reset": count})


def calculate(dataset, settings, *, market_config, indicator_config, created_at, start=None, end=None,
              step_seconds=None):
    """Run the bounded replay with the indicator consumer and return a validated indicator_result."""
    consumer = IndicatorConsumer(dataset, settings, indicator_config["limits"]["max_points"])
    simulation, summary, _ = drive(dataset, [consumer], config=market_config, start=start, end=end,
                                   step_seconds=step_seconds, passthrough=PASSTHROUGH)
    series = [{"key": spec["key"], "kind": spec["kind"], "period": spec["period"], "points": consumer.series[spec["key"]]}
              for spec in settings["indicators"]]
    provenance = {"dataset_id": dataset["dataset_id"], "bars_sha256": dataset["bars_sha256"],
                  "source_file_sha256": dataset["source"]["file_sha256"], "symbol": dataset["symbol"],
                  "interval": dataset["interval"], "timezone": dataset["timezone"], "data_label": dataset["data_label"]}
    totals = {"bars_processed": len(series[0]["points"]),
              "gaps_detected": consumer.gaps, "warmup_resets": consumer.resets, "sessions": consumer.sessions,
              "future_access_attempts": summary["future_access_attempts"]}
    body = {"dataset": provenance, "settings": settings, "replay": simulation, "series": series, "summary": totals}
    result = {
        "contract": "indicator_result", "version": "1.0",
        "result_id": "ind-" + sha256({"dataset_id": dataset["dataset_id"], "bars": dataset["bars_sha256"],
                                      "settings": settings["settings_sha256"], "replay": simulation})[:24],
        "mode": "historical_replay", "account_access": False, "authorization_possible": False,
        **body, "results_sha256": sha256(body), "created_at": created_at, "notice": NOTICE,
    }
    validate_result(result)
    return result


def validate_result(result):
    validate_schema("indicator_result", result)
    body = {k: result[k] for k in ("dataset", "settings", "replay", "series", "summary")}
    if sha256(body) != result["results_sha256"]:
        raise TradingError("invalid_indicator_result", "Indicator result hash mismatch.")
    settings = result["settings"]
    if sha256({k: v for k, v in settings.items() if k != "settings_sha256"}) != settings["settings_sha256"]:
        raise TradingError("invalid_indicator_result", "Indicator settings hash mismatch.")
    if [s["key"] for s in result["series"]] != [s["key"] for s in settings["indicators"]]:
        raise TradingError("invalid_indicator_result", "Indicator series do not match settings.")
    for series in result["series"]:
        for point in series["points"]:
            if (point["status"] == "ready") != (point["value"] is not None):
                raise TradingError("invalid_indicator_result", "Ready points need a value; unavailable points have none.")
            if point["available_at_utc"] > point["computed_at_sim_utc"]:
                raise TradingError("invalid_indicator_result", "A value was computed before its bar closed.")
