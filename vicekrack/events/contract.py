"""Execution-event contract and display-state rules (Step 31). Shared core: imports no content or trading code.

An event is controlled metadata only: fixed codes, IDs, counts, simulated time and the
time it was recorded. It never holds prompts, source text, credentials, environment
values, raw exception text or file paths (a strict schema plus `reject_secrets`).

Two origins, never mixed in one timeline:
  recorded       captured while a command actually ran; every event has `recorded_at`.
  reconstructed  rebuilt later from a saved record. `recorded_at` is only filled when the
                 saved record itself stored that moment (content production traces);
                 saved trading runs store no per-stage times, so it stays null.

Display states per component: idle, working, blocked, completed, failed, unknown.
Transition rules (anything else is rejected as `invalid_event_transition`):
  stage_started      idle | failed | unknown -> working   (failed/unknown: a resumed stage)
  stage_completed    working -> completed
  stage_failed       working -> failed
  stage_blocked      idle -> blocked                     (not run, e.g. an earlier stage failed)
  stage_interrupted  working -> unknown                  (outcome not known, e.g. interrupted)
  stage_reused       idle -> completed                   (Step 33: finished in an earlier attempt of the
                                                          same run; it is NOT executed again)
  order_decision     working -> working                  (only while the component is working)
  simulated_fill     working -> working
`completed` and `blocked` are terminal. A viewer shows `working` ONLY for a recorded
timeline whose writer still holds its lock (a live process). Otherwise a last-known
`working` is shown as `unknown`, and in a partial timeline every non-terminal state is
shown as `unknown`.
"""

import hashlib
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from ..errors import NetworkError
from ..persistence import reject_secrets

ROOT = Path(__file__).resolve().parents[2]
STATES = ("idle", "working", "blocked", "completed", "failed", "unknown")
TERMINAL = {"completed", "blocked"}
TRANSITIONS = {
    "stage_started": ({"idle", "failed", "unknown"}, "working"),
    "stage_completed": ({"working"}, "completed"),
    "stage_failed": ({"working"}, "failed"),
    "stage_blocked": ({"idle"}, "blocked"),
    "stage_interrupted": ({"working"}, "unknown"),
    "stage_reused": ({"idle"}, "completed"),
    "order_decision": ({"working"}, None),
    "simulated_fill": ({"working"}, None),
}
STATUSES = {
    "stage_started": {"started"}, "stage_completed": {"completed"}, "stage_failed": {"failed"},
    "stage_blocked": {"blocked"}, "stage_interrupted": {"interrupted", "uncertain"}, "stage_reused": {"completed"},
    "order_decision": {"accepted", "rejected", "pending_at_end_of_data"}, "simulated_fill": {"filled"},
}
NOTICE = ("Display data only. Recorded timelines were captured while a command ran; reconstructed timelines are "
          "rebuilt from saved records and carry no invented times. Nothing here runs agents, orders or productions.")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def event_id(timeline_id, sequence):
    return "evt-" + digest({"timeline_id": timeline_id, "sequence": sequence})[:24]


@lru_cache(maxsize=None)
def _validator(name):
    schema = json.loads((ROOT / f"schemas/{name}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def validate_event(event):
    """Schema, secret filter, type/status pairing, derived ID and origin rules."""
    try:
        reject_secrets(event)
    except NetworkError:
        raise NetworkError("invalid_event", "Event payloads may not contain credential-like fields or values.") from None
    if not isinstance(event, dict) or next(_validator("execution-event").iter_errors(event), None) is not None:
        raise NetworkError("invalid_event", "The event does not match the execution_event contract.")
    if event["status"] not in STATUSES[event["event_type"]]:
        raise NetworkError("invalid_event", "The event status does not belong to its event type.")
    if event["event_id"] != event_id(event["timeline_id"], event["sequence"]):
        raise NetworkError("invalid_event", "The event ID does not match its timeline and sequence.")
    if event["origin"] == "recorded" and (event["recorded_at"] is None or not event["timeline_id"].startswith("tl-")):
        raise NetworkError("invalid_event", "Recorded events need a recorded time and a recorded timeline.")
    if event["origin"] == "reconstructed" and not event["timeline_id"].startswith("rtl-"):
        raise NetworkError("invalid_event", "Reconstructed events belong to reconstructed timelines.")
    if not event["component"].startswith(event["department"] + "."):
        raise NetworkError("invalid_event", "A component belongs to its own department.")
    return event


def apply(states, event):
    """Apply one event's transition in place; raise invalid_event_transition if not allowed."""
    component = event["component"]
    if component not in states:
        raise NetworkError("invalid_event_transition", "The event names a component this timeline does not declare.")
    allowed, target = TRANSITIONS[event["event_type"]]
    if states[component] not in allowed:
        raise NetworkError("invalid_event_transition", "That transition is not allowed from the component's current state.")
    if target is not None:
        states[component] = target


def fold(events, components):
    """States after the events, the last sequence per component, and the first transition problem (or None)."""
    states = {name: "idle" for name in components}
    last = {name: None for name in components}
    for event in events:
        try:
            apply(states, event)
        except NetworkError:
            return states, last, "invalid_transition"
        last[event["component"]] = event["sequence"]
    return states, last, None


def display(states, last, *, live, degraded):
    rows = []
    for component, state in states.items():
        note = None
        if state == "working" and not live:
            state, note = "unknown", "not_live_last_known_working"
        if degraded and state not in TERMINAL:
            state, note = "unknown", note or "timeline_partial"
        rows.append({"component": component, "display_state": state, "last_sequence": last[component], "note": note})
    return rows


def build_view(*, timeline_id, origin, department, kind, correlation_id, run_id, source, time_basis, started_at,
               components, events, completeness, outcome, live, issues):
    """Assemble and validate an execution_timeline view (events must already be validated)."""
    states, last, problem = fold(events, components)
    issues = list(dict.fromkeys(list(issues) + ([problem] if problem else [])))
    if issues and completeness == "complete":
        completeness = "partial"
    degraded = completeness in ("partial",) or bool(issues)
    view = {"contract": "execution_timeline", "version": "1.0", "timeline_id": timeline_id, "origin": origin,
            "department": department, "kind": kind, "correlation_id": correlation_id, "run_id": run_id, "source": source,
            "time_basis": time_basis, "started_at": started_at, "completeness": completeness, "outcome": outcome,
            "live": bool(live), "issues": issues[:20], "event_count": len(events),
            "components": display(states, last, live=live, degraded=degraded), "events": events, "notice": NOTICE}
    if next(_validator("execution-timeline").iter_errors(view), None) is not None:
        raise NetworkError("invalid_timeline", "The timeline view does not match the execution_timeline contract.")
    return view
