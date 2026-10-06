"""Evidence package for the research-agent workflow (Step 28).

The controller, not the agents, builds this once per run. Every part comes from a
bounded replay that ENDS at the simulated time T (`sim_time_utc`), so only bars closed by
T, indicator values published by T and research signals detected by T can appear. The
dataset itself is never handed to an agent; bars after T are not counted, summarized or
otherwise exposed.

Slices (each agent receives only its own slice plus earlier handoffs):
- market_scout: provenance, verification, the last <= max_window_bars closed bars, gaps
  inside that window, the configured session and the freshness limit.
- trend_agent: the latest Step 26 point for each configured indicator, the thresholds and
  the last closed bar.
- strategy_agent: research signals detected by T (at most max_signals, newest kept), the
  latest evaluation of each strategy, and the strategy references.
- risk_review: thresholds only; it reviews the earlier handoffs.
"""

from ..contracts import sha256
from ..errors import TradingError
from ..indicators.engine import build_settings, calculate
from ..market.bars import INTERVAL_SECONDS, from_utc_text, missing_between
from ..market.replay import drive
from ..signals.engine import load_strategies, run_signals


class _LastView:
    """Replay consumer that keeps only what is visible at the final simulated time."""
    name = "evidence_bars"

    def __init__(self):
        self.window, self.visible, self.now = (), 0, None

    def on_step(self, view):
        self.window, self.visible, self.now = view.bars(), view.visible_count, view.now

    def final(self):
        return {}


def _plain(bar):
    return {k: (dict(v) if hasattr(v, "keys") else v) for k, v in bar.items()}


def build_evidence(dataset, *, sim_time, workflow_config, market_config, indicator_config, signal_config, created_at):
    """Return (evidence, hashes). Raises TradingError for a simulated time before the data."""
    limits, scout, trend = workflow_config["limits"], workflow_config["scout"], workflow_config["trend"]
    if sim_time is None:
        sim_time = dataset["last_available_utc"]
    try:
        moment = from_utc_text(sim_time)
    except (TypeError, ValueError):
        raise TradingError("invalid_sim_time", "--as-of must be a UTC time like 2026-01-15T15:35:00Z.") from None
    if moment <= from_utc_text(dataset["first_start_utc"]):
        raise TradingError("sim_time_before_data", "The simulated time is at or before the first bar starts.")
    window = {"end": sim_time}

    capture = _LastView()
    drive(dataset, [capture], config=market_config, **window)
    bars = [_plain(b) for b in capture.window][-limits["max_window_bars"]:]
    gaps = []
    for before, after in zip(bars, bars[1:]):
        missing = missing_between(from_utc_text(before["timestamp_utc"]), from_utc_text(after["timestamp_utc"]),
                                  dataset["interval"], dataset["timezone"], "evidence")
        if missing:
            gaps.append({"after_sequence": before["sequence"], "before_sequence": after["sequence"], "missing_intervals": missing})

    intraday = INTERVAL_SECONDS[dataset["interval"]] is not None
    settings = build_settings(dataset, config=indicator_config, ema=sorted({trend["ema_fast"], trend["ema_slow"]}),
                              rsi=[trend["rsi"]], volume_sma=[trend["volume_sma"]], vwap=intraday,
                              vwap_session=scout["session"] if intraday else None, gap_policy="reset")
    indicators = calculate(dataset, settings, market_config=market_config, indicator_config=indicator_config,
                           created_at=created_at, **window)
    latest = {s["key"]: (s["points"][-1] if s["points"] else None) for s in indicators["series"]}

    strategies = load_strategies(workflow_config["strategies"], signal_config)
    signal_run = run_signals(dataset, strategies, market_config=market_config, indicator_config=indicator_config,
                             signal_config=signal_config, created_at=created_at, **window)
    signals = signal_run["signals"][-limits["max_signals"]:]
    latest_evaluations = {e["strategy"]: (e["entries"][-1] if e["entries"] else None) for e in signal_run["evaluations"]}

    provenance = dict(signal_run["dataset"])
    evidence = {
        "sim_time_utc": sim_time,
        "market_scout": {
            "dataset": provenance, "verification": dict(dataset["verification"]), "closed_bars_total": capture.visible,
            "window_bars": bars, "window_gaps": gaps, "interval": dataset["interval"],
            "interval_seconds": INTERVAL_SECONDS[dataset["interval"]] or 86400, "session": dict(scout["session"]),
            "freshness_max_intervals": scout["freshness_max_intervals"], "min_bars": scout["min_bars"],
        },
        "trend_agent": {
            "indicators": latest, "keys": {"ema_fast": f"ema_{trend['ema_fast']}", "ema_slow": f"ema_{trend['ema_slow']}",
                                           "rsi": f"rsi_{trend['rsi']}", "volume_sma": f"volume_sma_{trend['volume_sma']}",
                                           "vwap": "vwap_session" if intraday else None},
            "thresholds": {k: trend[k] for k in ("rsi_overbought", "rsi_oversold", "volume_elevated_multiple")},
            "last_bar": bars[-1] if bars else None,
        },
        "strategy_agent": {
            "signals": signals, "signals_truncated": len(signal_run["signals"]) > len(signals),
            "latest_evaluations": latest_evaluations, "strategies": signal_run["strategies"],
        },
        "risk_review": {"min_bars": scout["min_bars"]},
    }
    hashes = {
        "indicator_settings_sha256": settings["settings_sha256"], "signal_run_results_sha256": signal_run["results_sha256"],
        "strategies": [{"name": s["name"], "config_sha256": s["config_sha256"]} for s in signal_run["strategies"]],
        "evidence_sha256": sha256(evidence),
    }
    return evidence, hashes
