"""Living HQ trading sessions (Step 39): a read-only selector and summary for Step 39 sessions.

`hq_session` 1.0 (schemas/hq-session.schema.json) describes one session: its five stages
with every attempt's real (wall-clock) start and end, its labelled time domains, its
results and three links into views the HQ already has:

  research rooms     the research timeline (the four upstairs rooms)
  simulator station  the simulation timeline (the Simulator operations station)
  analytics desk     the same simulation timeline in the Step 34 trading results desk

Links are checked when the summary is built: the timeline must load through the Step 31
loader and name the session's own research or simulation run. A link that cannot be
established is shown as unavailable with a reason, never guessed.

Sources: a completed session is summarized from its verified manifest; an incomplete one
from its verified checkpoint and completed artifacts only. A session whose files fail
validation is reported with its integrity problem and no results. Stage timelines stay
separate in their own order with their own timestamps; nothing is merged or filled in.

Nothing here takes the session lock, starts or resumes a session, runs a stage or writes.
"""

import json
import re
from functools import lru_cache

from jsonschema import Draft202012Validator

from ..errors import NetworkError
from ..events.contract import ROOT
from ..trading.session.manifest import CONTINUITY, LABELS, relation
from ..trading.session.view import MAX_SESSIONS, describe, list_sessions

SESSION = re.compile(r"^(demo|tss-[0-9a-f]{24})$")
STAGE_LABELS = {"dataset_validation": "Validate dataset", "research_analysis": "Research-agent analysis",
                "simulation": "Simulation", "performance_analytics": "Performance analytics",
                "hq_summary": "Living HQ session summary"}
TIME_DOMAIN = {"dataset_validation": "historical_data", "research_analysis": "historical_research",
               "simulation": "simulated_execution", "performance_analytics": "derived_from_simulation",
               "hq_summary": "wall_clock"}
LINK_LABELS = {"research_rooms": "Research rooms (upstairs)", "simulator_station": "Simulator station (operations)",
               "analytics_desk": "Analytics desk (trading results)"}
NOTICE = ("Read-only summary of an OFFLINE, SIMULATED trading research session. Research is historical research at a "
          "stated simulated as-of time and was not used by the simulator. Nothing here starts, resumes or changes a "
          "session, and nothing here trades.")


@lru_cache(maxsize=None)
def _validator():
    return Draft202012Validator(json.loads((ROOT / "schemas/hq-session.schema.json").read_text(encoding="utf-8")))


def check(document):
    if next(_validator().iter_errors(document), None) is not None:
        raise NetworkError("invalid_session_summary", "The session summary does not match its contract.")
    return document


def session_items(root=None):
    from .session_demo import DEMO_ROW
    items, total = list_sessions(root)
    return {"items": [DEMO_ROW] + items, "total": total + 1, "shown_limit": MAX_SESSIONS}


def _attempts(stage):
    return [{"attempt": a["attempt"], "reason": a["reason"], "outcome": a["outcome"], "started_at": a["started_at"],
             "ended_at": a["ended_at"], "error_code": a["error_code"], "timeline_id": a["events"]["timeline_id"],
             "timeline_outcome": a["events"]["outcome"]} for a in stage["attempts"]]


def _timeline_link(name, stage, expected_run, root):
    """The completed attempt's recorded timeline if it is complete and names this run; otherwise the
    run's reconstructed timeline. Every candidate is loaded and checked; nothing is assumed."""
    from ..events.cli import load_timeline
    if stage is None or stage["status"] != "completed" or expected_run is None:
        return {"available": False, "timeline_id": None, "origin": None, "reason": "stage_not_completed",
                "label": LINK_LABELS[name]}
    done = [a for a in stage["attempts"] if a["outcome"] in ("completed", "recovered")]
    candidates = []
    if done and done[-1]["events"]["timeline_id"] and done[-1]["events"]["outcome"] == "completed":
        candidates.append((done[-1]["events"]["timeline_id"], "recorded"))
    candidates.append((expected_run, "reconstructed"))
    reason = "timeline_unavailable"
    for timeline_id, origin in candidates:
        try:
            view = load_timeline(timeline_id, root)
        except NetworkError:
            continue
        if view.get("run_id") == expected_run and view.get("department") == "trading":
            return {"available": True, "timeline_id": timeline_id, "origin": origin,
                    "reason": None if origin == "recorded" else "no_complete_recorded_timeline",
                    "label": LINK_LABELS[name]}
        reason = "timeline_run_mismatch"
    return {"available": False, "timeline_id": None, "origin": None, "reason": reason, "label": LINK_LABELS[name]}


def session_document(session_id, root=None):
    if not SESSION.match(str(session_id)):
        raise NetworkError("invalid_session_id", "Use demo or a tss- session ID.")
    if session_id == "demo":
        from .session_demo import demo_session
        return check(demo_session())
    view = describe(session_id, root)
    documents, manifest = view["documents"], view["manifest"]
    stages = {s["stage"]: s for s in view["stages"]}
    check_doc, research = documents.get("dataset_validation"), documents.get("research_analysis")
    sim, report = documents.get("simulation"), documents.get("performance_analytics")
    window = sim["replay"] if sim else None
    domains = {
        "historical_data": None if check_doc is None else {
            "first_start_utc": check_doc["first_start_utc"], "last_available_utc": check_doc["last_available_utc"],
            "label": LABELS["historical_data"]},
        "historical_research": None if research is None else {
            "as_of_utc": research["sim_time_utc"],
            "relation_to_simulation": relation(research["sim_time_utc"], window) if window else None,
            "used_by_simulation": False, "label": LABELS["historical_research"]},
        "simulated_execution": None if window is None else {
            "start_utc": window["start_utc"], "end_utc": window["end_utc"], "label": LABELS["simulated_execution"]},
        "wall_clock": {"session_created_at": view["created_at"], "label": LABELS["wall_clock"]},
    }
    rows = []
    for stage in view["stages"]:
        artifact = stage["artifact"]
        rows.append({"stage": stage["stage"], "position": stage["position"], "label": STAGE_LABELS[stage["stage"]],
                     "time_domain": TIME_DOMAIN[stage["stage"]], "status": stage["status"],
                     "verification": stage["verification"],
                     "record_kind": artifact["record_kind"] if artifact else None,
                     "record_id": artifact["record_id"] if artifact else None, "attempts": _attempts(stage)})
    problem = view["integrity_problem"]
    research_id = research["run_id"] if research else None
    sim_id = sim["run_id"] if sim else None
    sim_link = _timeline_link("simulator_station", stages["simulation"], sim_id, root)
    desk = dict(sim_link, label=LINK_LABELS["analytics_desk"])
    if report is None:
        desk = {"available": False, "timeline_id": None, "origin": None, "reason": "stage_not_completed",
                "label": LINK_LABELS["analytics_desk"]}
    links = {"research_rooms": _timeline_link("research_rooms", stages["research_analysis"], research_id, root),
             "simulator_station": sim_link, "analytics_desk": desk}
    if problem is not None:                              # nothing from an unverified session is linked or shown
        links = {name: {"available": False, "timeline_id": None, "origin": None, "reason": "session_integrity_failed",
                        "label": LINK_LABELS[name]} for name in LINK_LABELS}
    results = None
    if problem is None and (research or sim):
        results = {"research_verdict": research["final"]["verdict"] if research else None,
                   "fills": sim["summary"]["fills"] if sim else None,
                   "orders_created": sim["summary"]["orders_created"] if sim else None,
                   "orders_rejected": sim["summary"]["orders_rejected"] if sim else None,
                   "ending_equity": sim["summary"]["ending_equity"] if sim else None,
                   "net_return": report["account"]["net_return"] if report else None,
                   "closed_trades": report["closed_trades"]["count"] if report else None,
                   "kill_switch_engaged": sim["kill_switch"]["engaged"] if sim else None}
    commands = [f"python -m vicekrack trading-session-inspect {session_id}"]
    if view["status"] in ("failed", "interrupted"):
        commands.append(f"python -m vicekrack trading-session-resume {session_id}")
    document = {
        "contract": "hq_session", "version": "1.0", "origin": "saved", "session_id": session_id, "read_only": True,
        "simulated": True, "status": view["status"], "live": view["live"],
        "summary_source": "manifest" if manifest is not None else "checkpoint",
        "manifest_id": manifest["manifest_id"] if manifest else None,
        "integrity": {"status": "failed" if problem else "verified", "problem": problem},
        "dataset": {k: view["dataset"][k] for k in ("dataset_id", "symbol", "interval", "data_label")},
        "time_domains": domains, "stages": rows,
        "links": links,
        "results": results, "continuity": CONTINUITY, "commands": commands, "notice": NOTICE,
    }
    return check(document)
