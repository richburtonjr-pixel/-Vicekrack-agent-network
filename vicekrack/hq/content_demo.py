"""Demo production for the Living HQ content results desk (Step 35). SYNTHETIC, never stored, never executed.

The brief, script and scene plan in `content_demo.json` were produced once, offline, with the
mock Creator and the Step 12 planner, and committed as a static fixture. Here they are only
loaded and re-validated with the same contract validators as real artifacts; nothing is
drafted, planned, rendered or checked at request time. The production matches the demo
house's production events (`demo.py`): its stages complete in that order, and the quality
event names this demo's synthetic Step 22 report (`needs_review`, because a production with
no media files cannot be measured).

There are no media files: the demo shows no video or posters and says so.
"""

import json
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from ..scene_plan import validate_scene_plan
from ..short_script import validate_short_script
from ..story_brief import validate_story_brief
from .demo import PRODUCTION, QUALITY_REPORT, START, _id, _stamp, demo_events

FIXTURE = Path(__file__).resolve().parent / "content_demo.json"
RECORD = _id("ver", "record")
SELECTION = _id("sel", "selection")


def _at(seconds):
    return _stamp(START + timedelta(seconds=seconds))


class DemoArtifacts:
    """Same shape as content.Artifacts, built from the validated fixture (no files)."""

    def __init__(self, brief, script, plan):
        self.brief, self.script, self.plan, self.manifest = brief, script, plan, None
        self.media = {}
        self.posters = [{"index": s["index"], "status": "not_produced", "reason": "demo_has_no_media_files",
                         "media_id": None} for s in plan["scenes"]]
        self.items = {
            "brief": {"status": "verified", "reason": None, "sha256": "1" * 64},
            "script": {"status": "verified", "reason": None, "sha256": "2" * 64},
            "validation": {"status": "verified", "reason": None, "draft": False, "claims_unverified": 0,
                           "claims_total": len(script["claims"])},
            "plan": {"status": "verified", "reason": None, "sha256": "3" * 64},
            "manifest": {"status": "not_produced", "reason": "demo_has_no_media_files"},
            "video": {"status": "not_produced", "reason": "demo_has_no_media_files"},
        }


def _state(events):
    """A production state shaped like Step 21's, with stage times taken from the demo events."""
    times = {}
    for event in events:
        name = event["component"].split(".")[-1]
        if event["component"].startswith("content.production.") and name in ("brief", "creator", "validate", "plan",
                                                                               "preview"):
            if event["event_type"] == "stage_started":
                times.setdefault(name, {})["started_at"] = event["recorded_at"]
            elif event["event_type"] == "stage_completed":
                times.setdefault(name, {})["finished_at"] = event["recorded_at"]
    stages = [{"name": name, "status": "completed", "attempts": 1, "error_code": None, "artifacts": {},
               "started_at": times[name]["started_at"], "finished_at": times[name]["finished_at"]}
              for name in ("brief", "creator", "validate", "plan", "preview")]
    return {"production_id": PRODUCTION["id"], "status": "completed", "created_at": _at(0),
            "updated_at": times["preview"]["finished_at"], "stages": stages, "trace": [{}] * 10,
            "config": {"profile": "demo", "record_id": RECORD, "selection_run_id": SELECTION,
                       "creator": {"adapter": "mock", "model": None, "paid": False}, "narration": None,
                       "allow_draft_preview": False}}


def _report(state, checked_at):
    unavailable = {"status": "unavailable", "reasons": ["media_probe_unavailable"], "details": {}}
    checks = [
        {"check_id": "state", "status": "pass", "reasons": [], "details": {"status": "completed", "completed_stages": 5,
                                                                          "creator": "mock"}},
        {"check_id": "artifacts", "status": "pass", "reasons": [], "details": {"brief": True, "script": True, "plan": True}},
        {"check_id": "provenance", "status": "pass", "reasons": [], "details": {"claims": 1, "sources": 2}},
        {"check_id": "evidence_freshness", "status": "pass", "reasons": [], "details": {"max_age_days": 14}},
        {"check_id": "draft_restrictions", "status": "pass", "reasons": [], "details": {"draft": False}},
        {"check_id": "scene_timing", "status": "pass", "reasons": [], "details": {"scenes": 4, "duration_seconds": 15}},
        dict(unavailable, check_id="video"), dict(unavailable, check_id="audio"),
        {"check_id": "manifest_consistency", "status": "needs_review", "reasons": ["media_measurements_unavailable"],
         "details": {"posters_found": 0}},
        {"check_id": "history", "status": "pass", "reasons": [], "details": {"history_state": "produced"}},
    ]
    return {"report_id": QUALITY_REPORT, "production_id": state["production_id"], "checked_at": checked_at,
            "result": "needs_review", "reasons": ["media_probe_unavailable", "media_measurements_unavailable"],
            "checks": checks,
            "scope": {"technical_checks_only": True, "factual_accuracy_verified": False, "rights_cleared": False,
                      "publishable": False, "permission_to_publish": False},
            "notes": ["DEMO: synthetic report. Technical checks only; never a permission to publish."]}


@lru_cache(maxsize=1)
def _fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    validate_story_brief(data["brief"])
    validate_short_script(data["script"])
    validate_scene_plan(data["plan"])
    if data["plan"]["script"] != data["script"] or data["script"]["claims"] != data["brief"]["claims"]:
        raise ValueError("demo fixture chain")
    return data


def demo_inputs():
    """(view, state, artifacts, demo extras) for the demo house's timeline."""
    data = _fixture()
    events = demo_events()
    view = {"timeline_id": "demo", "origin": "demo", "kind": "hq_demo", "completeness": "complete", "issues": [],
            "time_basis": "synthetic", "started_at": _at(0), "run_id": None, "correlation_id": None, "events": events}
    state = _state(events)
    checked = next(e["recorded_at"] for e in events if e["component"] == "content.production.quality"
                   and e["event_type"] == "stage_completed")
    record = {"verified_at": _at(-3600), "flags": [],
              "summary": {"verified": 1, "corroborated": 0, "disputed": 0, "insufficient_evidence": 0, "rejected": 0},
              "claims": [{"claim_id": "k1", "text": data["brief"]["claims"][0]["text"], "status": "verified",
                          "rationale_codes": ["primary_first_hand"], "rationale": "DEMO: one first-hand official source.",
                          "primary_support": True, "independent_origins": 1}]}
    extras = {"record": record, "reports": [_report(state, checked)],
              "attempts": [{"timeline_id": "demo", "kind": "hq_demo", "started_at": _at(0), "completeness": "complete",
                            "outcome": "completed", "stages_run": ["brief", "creator", "validate", "plan", "preview"],
                            "stages_reused": [], "failures": []}]}
    return view, state, DemoArtifacts(data["brief"], data["script"], data["plan"]), extras
