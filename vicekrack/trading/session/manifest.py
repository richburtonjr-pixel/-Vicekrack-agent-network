"""Trading session manifest (Step 39): contract `trading_session_manifest` 1.0.

The manifest is the fifth stage's artifact. It correlates, by validated IDs and hashes
only, the session's stage artifacts, the Step 31 timelines its attempts recorded, and the
saved results the Living HQ already knows how to show:

  research rooms     the recorded research timeline of the completed attempt, otherwise the
                     research run's reconstructed timeline (rar-...)
  simulator station  the recorded simulation timeline, otherwise the reconstructed one (srun-...)
  analytics desk     the same simulation timeline (the Step 34 desk finds the analytics
                     report by content, never by filename)

Time is kept in four labelled domains: historical data, historical research (the exact
simulated as-of time), simulated execution (the replay window) and wall clock (when this
computer ran each attempt). Stage timelines are listed separately in their own order with
their own timestamps; the manifest never merges them into one continuous history.
"""

from ..contracts import sha256, validate_schema
from ..errors import TradingError

CONTINUITY = ("Each stage keeps its own record and timestamps. Research and simulation timelines exist only for "
              "attempts run with --record-events; dataset validation, analytics and this summary record no events. "
              "No continuous session-wide history is shown and gaps between stages are not filled in.")
NOTICE = ("Session summary of an OFFLINE, SIMULATED trading research session on stored historical data. Research "
          "conclusions are historical research as of the stated simulated time and were not used by the simulator. "
          "No broker, no live data, no orders. Not advice and not a prediction.")
LABELS = {
    "historical_data": "Historical market data: the stored dataset's bars (not live, not verified as authentic).",
    "historical_research": ("Historical research: research-agent conclusions computed only from bars closed by the "
                            "as-of time. Research only; never used by the simulator; authorizes nothing."),
    "simulated_execution": ("Simulated execution: the simulator's replay window in simulated market time. Decisions "
                            "use only bars closed by each simulated moment."),
    "wall_clock": ("Wall clock: real time on this computer when the session was created; each stage attempt lists its "
                   "own real start and end times."),
}


def relation(as_of, window):
    if as_of <= window["start_utc"]:
        return "before_simulation_start"
    if as_of < window["end_utc"]:
        return "during_simulation_window"
    return "at_or_after_simulation_end"


def _completed_attempt(stage):
    done = [a for a in stage["attempts"] if a["outcome"] in ("completed", "recovered")]
    return done[-1] if done else None


def _link(stage, reconstructed_id):
    attempt = _completed_attempt(stage)
    if attempt and attempt["events"]["timeline_id"] and attempt["events"]["outcome"] == "completed":
        return {"available": True, "timeline_id": attempt["events"]["timeline_id"], "origin": "recorded", "reason": None}
    return {"available": True, "timeline_id": reconstructed_id, "origin": "reconstructed",
            "reason": "no_complete_recorded_timeline"}


def build_manifest(record, checkpoint, documents, *, created_at):
    check, research, sim, report = (documents["dataset_validation"], documents["research_analysis"],
                                    documents["simulation"], documents["performance_analytics"])
    stages = []
    for stage in checkpoint["stages"][:4]:
        timelines = [{"attempt": a["attempt"], "timeline_id": a["events"]["timeline_id"], "origin": "recorded",
                      "outcome": a["events"]["outcome"]} for a in stage["attempts"] if a["events"]["timeline_id"]]
        stages.append({"stage": stage["stage"], "position": stage["position"],
                       "time_domain": {"dataset_validation": "historical_data", "research_analysis": "historical_research",
                                       "simulation": "simulated_execution",
                                       "performance_analytics": "derived_from_simulation"}[stage["stage"]],
                       "attempts": stage["attempts"], "artifact": stage["artifact"], "timelines": timelines,
                       "reconstructable_id": {"research_analysis": research["run_id"],
                                              "simulation": sim["run_id"]}.get(stage["stage"])})
    window = sim["replay"]
    sim_link = _link(checkpoint["stages"][2], sim["run_id"])
    body = {
        "session_id": record["session_id"], "correlation_id": record["correlation_id"], "simulated": True,
        "research_used_by_simulation": False, "authorization_possible": False, "dataset": record["dataset"],
        "inputs": {name: item["sha256"] for name, item in record["inputs"].items()}, "versions": record["versions"],
        "time_domains": {
            "historical_data": {"first_start_utc": check["first_start_utc"],
                                "last_available_utc": check["last_available_utc"], "label": LABELS["historical_data"]},
            "historical_research": {"as_of_utc": research["sim_time_utc"],
                                    "rule": record["inputs"]["session_config"]["document"]["research"]["as_of"],
                                    "relation_to_simulation": relation(research["sim_time_utc"], window),
                                    "used_by_simulation": False, "label": LABELS["historical_research"]},
            "simulated_execution": {"start_utc": window["start_utc"], "end_utc": window["end_utc"],
                                    "step_seconds": window["step_seconds"],
                                    "last_bar_close_utc": sim["summary"]["last_bar_close_utc"],
                                    "label": LABELS["simulated_execution"]},
            "wall_clock": {"session_created_at": record["created_at"], "label": LABELS["wall_clock"]},
        },
        "stages": stages,
        "links": {"research_rooms": _link(checkpoint["stages"][1], research["run_id"]), "simulator_station": sim_link,
                  "analytics_desk": dict(sim_link)},
        "results": {"research_verdict": research["final"]["verdict"], "research_run_id": research["run_id"],
                    "simulation_run_id": sim["run_id"], "analytics_report_id": report["report_id"],
                    "fills": sim["summary"]["fills"], "orders_created": sim["summary"]["orders_created"],
                    "orders_rejected": sim["summary"]["orders_rejected"], "ending_equity": sim["summary"]["ending_equity"],
                    "net_return": report["account"]["net_return"], "closed_trades": report["closed_trades"]["count"],
                    "kill_switch_engaged": sim["kill_switch"]["engaged"]},
        "continuity": {"kind": "separate_stage_timelines", "explanation": CONTINUITY},
    }
    results_sha = sha256(body)
    manifest = {"contract": "trading_session_manifest", "version": "1.0",
                "manifest_id": "tsm-" + sha256({"session": record["session_id"], "results": results_sha})[:24],
                **body, "results_sha256": results_sha, "created_at": created_at, "notice": NOTICE}
    return validate_manifest(manifest)


def validate_manifest(manifest):
    validate_schema("trading_session_manifest", manifest)
    body = {k: v for k, v in manifest.items()
            if k not in ("contract", "version", "manifest_id", "results_sha256", "created_at", "notice")}
    if (sha256(body) != manifest["results_sha256"]
            or manifest["manifest_id"] != "tsm-" + sha256({"session": manifest["session_id"],
                                                           "results": manifest["results_sha256"]})[:24]):
        raise TradingError("invalid_trading_session_manifest", "Session manifest hash mismatch.")
    if [s["stage"] for s in manifest["stages"]] != ["dataset_validation", "research_analysis", "simulation",
                                                     "performance_analytics"]:
        raise TradingError("invalid_trading_session_manifest", "The manifest does not list the fixed stages in order.")
    return manifest
