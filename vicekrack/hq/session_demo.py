"""Demo trading session for the Living HQ (Step 39). SYNTHETIC, never stored, never executed.

It describes how a session summary looks, using the demo house's own synthetic records:
the research rooms' failed demo workflow is attempt 1 of the research stage (failed), an
unrecorded attempt 2 completed it, and the simulator station and analytics desk show the
Step 34 demo run and report (`results_demo.py`). All three links open the `demo` timeline.
Times and IDs are synthetic; nothing here runs a stage.
"""

from .demo import DATASET, RUN, _id
from .results_demo import REPORT, demo_inputs, demo_report
from ..trading.session.manifest import CONTINUITY, LABELS

SESSION_CREATED = "2026-01-06T09:00:00Z"
AS_OF = "2026-01-05T15:30:00Z"
DEMO_ROW = {"session_id": "demo", "readable": True, "status": "completed", "live": False, "created_at": SESSION_CREATED,
            "dataset_id": DATASET["id"], "symbol": "DEMO", "interval": "5m", "data_label": "synthetic",
            "stages_completed": 5, "origin": "demo"}
NOTICE = ("DEMO DATA: a synthetic session summary, not a saved session. It shows how a Step 39 session is presented; "
          "nothing here starts, resumes or changes a session, and nothing here trades.")


def _attempt(number, minute, outcome="completed", reason="first_run", error=None):
    start = f"2026-01-06T09:{minute:02d}:00Z"
    end = f"2026-01-06T09:{minute:02d}:30Z"
    return {"attempt": number, "reason": reason, "outcome": outcome, "started_at": start, "ended_at": end,
            "error_code": error, "timeline_id": None, "timeline_outcome": None}


def _stage(name, position, label, domain, kind, record, attempts):
    return {"stage": name, "position": position, "label": label, "time_domain": domain, "status": "completed",
            "verification": "demo", "record_kind": kind, "record_id": record, "attempts": attempts}


def demo_session():
    _, run = demo_inputs()
    report = demo_report()
    window = run["replay"]
    demo_link = {"available": True, "timeline_id": "demo", "origin": "demo", "reason": None}
    return {
        "contract": "hq_session", "version": "1.1", "origin": "demo", "session_id": "demo", "read_only": True,
        "simulated": True, "status": "completed", "live": False, "summary_source": "demo",
        "manifest_id": _id("tsm", "session-manifest"), "integrity": {"status": "demo", "problem": None},
        "dataset": {"dataset_id": DATASET["id"], "symbol": "DEMO", "interval": "5m", "data_label": "synthetic"},
        "time_domains": {
            "historical_data": {"first_start_utc": window["start_utc"], "last_available_utc": AS_OF,
                                "label": LABELS["historical_data"]},
            "historical_research": {"as_of_utc": AS_OF, "relation_to_simulation": "at_or_after_simulation_end",
                                    "used_by_simulation": False, "label": LABELS["historical_research"]},
            "simulated_execution": {"start_utc": window["start_utc"], "end_utc": window["end_utc"],
                                    "label": LABELS["simulated_execution"]},
            "wall_clock": {"session_created_at": SESSION_CREATED, "label": LABELS["wall_clock"]},
        },
        "stages": [
            _stage("dataset_validation", 1, "Validate dataset", "historical_data", "trading_session_dataset_check",
                   _id("tsdc", "dataset-check"), [_attempt(1, 0)]),
            _stage("research_analysis", 2, "Research-agent analysis", "historical_research", "research_agent_run",
                   _id("rar", "research-run"),
                   [_attempt(1, 1, "failed", error="research_workflow_failed"),
                    _attempt(2, 5, reason="retry_after_failure")]),
            _stage("simulation", 3, "Simulation", "simulated_execution", "simulation_run", RUN, [_attempt(1, 6)]),
            _stage("performance_analytics", 4, "Performance analytics", "derived_from_simulation",
                   "simulation_analytics_report", REPORT, [_attempt(1, 7)]),
            _stage("hq_summary", 5, "Living HQ session summary", "wall_clock", "trading_session_manifest",
                   _id("tsm", "session-manifest"), [_attempt(1, 8)]),
        ],
        "links": {
            "research_rooms": {**demo_link, "reason": "demo_shows_failed_attempt_1",
                               "label": "Research rooms (upstairs)"},
            "simulator_station": {**demo_link, "label": "Simulator station (operations)"},
            "analytics_desk": {**demo_link, "label": "Analytics desk (trading results)"},
        },
        "results": {"research_verdict": "sufficient_for_future_paper_evaluation", "fills": run["summary"]["fills"],
                    "orders_created": run["summary"]["orders_created"],
                    "orders_rejected": run["summary"]["orders_rejected"],
                    "ending_equity": run["summary"]["ending_equity"], "net_return": report["account"]["net_return"],
                    "closed_trades": report["closed_trades"]["count"], "kill_switch_engaged": False},
        "continuity": CONTINUITY,
        "commands": ["python -m vicekrack trading-session-list", "python -m vicekrack trading-session-inspect SESSION_ID"],
        "notice": NOTICE,
        "data_source": {"adapter": "synthetic_fixture", "data_label": "synthetic", "source_name": "synthetic-demo",
                        "verified_as_authentic": False, "provider": None,
                        "coverage": {"first_start_utc": window["start_utc"], "last_available_utc": AS_OF, "bars": 12,
                                     "uncovered_before_first_seconds": None, "uncovered_after_last_seconds": None,
                                     "gap_count": 0, "missing_intervals": 0, "calendar": "none"}},
        "data_source_problem": None,
    }
