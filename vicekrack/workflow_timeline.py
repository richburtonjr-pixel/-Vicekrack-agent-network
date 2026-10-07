"""Saved Step 5 workflow runs as reconstructed timelines (Step 33): read-only.

A saved run (`runtime/runs/<32 hex>.json`) is shown as `wfr-<run id>`. Since Step 7 every
saved run keeps `workflow_state.audit_trace`: each role attempt's start and finish with the
time the workflow itself recorded. Those become events with `recorded_at` set to that time,
truncated to whole seconds (`time_basis: source_recorded`):
  running -> stage_started; completed -> stage_completed;
  failed (error "interrupted") -> stage_interrupted; failed (other) -> stage_failed.
Legacy runs without a workflow state only list completed roles, without times
(`time_basis: none_saved`). A run that is not finished is `open`, and liveness is never
assumed: a role saved as running is shown `unknown`.

Reading uses `RunStore.read` (no lock file, no folder creation). Nothing is executed.
"""

import re

from .content_events import ROLES, WORKFLOW_COMPONENTS, correlation_for, workflow_run_ref
from .errors import NetworkError
from .events.contract import build_view, digest, event_id, validate_event
from .orchestrator import ROOT

RUN_REF = re.compile(r"^wfr-([0-9a-f]{32})$")
OUTCOMES = {"completed": "completed", "failed": "failed"}


def _second(stamp):
    """2026-10-07T19:56:34.123456Z -> 2026-10-07T19:56:34Z (the source's own time, truncated)."""
    return stamp[:19] + "Z" if isinstance(stamp, str) and len(stamp) >= 19 else None


def from_saved_run(state):
    reference = workflow_run_ref(state["run_id"])
    workflow = state.get("workflow_state")
    rows = workflow["audit_trace"] if workflow else []
    timeline_id = "rtl-" + digest({"source": reference, "updated_at": state["updated_at"], "audit": rows,
                                   "history": [r["agent"] for r in state["history"]]})[:24]
    correlation_id = correlation_for(reference)
    events = []

    def add(agent, event_type, status, recorded_at, reasons=(), attempt=None):
        sequence = len(events) + 1
        events.append(validate_event({
            "contract": "execution_event", "version": "1.0", "event_id": event_id(timeline_id, sequence),
            "origin": "reconstructed", "timeline_id": timeline_id, "correlation_id": correlation_id,
            "run_id": reference, "department": "content", "component": ROLES[agent], "stage": agent,
            "sequence": sequence, "event_type": event_type, "status": status, "sim_time_utc": None,
            "recorded_at": recorded_at, "reason_codes": list(reasons),
            "refs": [{"kind": "workflow_run", "id": reference}], "details": {"attempt": attempt} if attempt else {}}))

    if workflow:
        for row in rows:
            at = _second(row["timestamp"])
            if row["status"] == "running":
                add(row["agent"], "stage_started", "started", at, attempt=row["attempt"])
            elif row["status"] == "completed":
                add(row["agent"], "stage_completed", "completed", at, attempt=row["attempt"])
            elif row["error"] == "interrupted":
                add(row["agent"], "stage_interrupted", "interrupted", at, ["interrupted"], row["attempt"])
            else:
                add(row["agent"], "stage_failed", "failed", at, [row["error"] or "execution_failed"], row["attempt"])
        basis = "source_recorded"
    else:
        for row in state["history"]:
            add(row["agent"], "stage_started", "started", None)
            add(row["agent"], "stage_completed", "completed", None)
        basis = "none_saved"
    finished = state["status"] in OUTCOMES
    return build_view(timeline_id=timeline_id, origin="reconstructed", department="content",
                      kind="research_review_workflow", correlation_id=correlation_id, run_id=reference,
                      source={"kind": "workflow_run", "id": reference, "saved_at": _second(state["updated_at"]),
                              "saved_at_meaning": "When the saved run was last written. Role times come from its audit trail."},
                      time_basis=basis, started_at=_second(state["created_at"]), components=WORKFLOW_COMPONENTS,
                      events=events, completeness="complete" if finished else "open",
                      outcome=OUTCOMES.get(state["status"]), live=False,
                      issues=[] if workflow else ["legacy_run_without_times"])


def _store(root):
    from pathlib import Path

    from .persistence import RunStore
    directory = Path(ROOT if root is None else root) / "runtime/runs"
    if not directory.is_dir():                      # never create folders while reading
        return None
    return RunStore(directory)


def load_workflow_timeline(identifier, root=None):
    match = RUN_REF.match(str(identifier))
    if not match:
        raise NetworkError("invalid_timeline_id", "Saved workflow runs look like wfr- followed by 32 hex characters.")
    store = _store(root)
    if store is None:
        raise NetworkError("run_not_found", "Saved run does not exist.")
    return from_saved_run(store.read(match.group(1)))


def list_workflow_timelines(root=None):
    store, rows = _store(root), []
    if store is None:
        return rows
    for path in sorted(store.directory.glob("*.json")):
        reference = "wfr-" + path.stem
        try:
            view = load_workflow_timeline(reference, root)
            rows.append({"source_id": reference, "origin": "reconstructed", "department": "content",
                         "kind": view["kind"], "completeness": view["completeness"], "outcome": view["outcome"],
                         "event_count": view["event_count"], "readable": True})
        except (NetworkError, OSError, ValueError, KeyError, TypeError):
            rows.append({"source_id": reference, "origin": "reconstructed", "department": "content",
                         "kind": "research_review_workflow", "readable": False})
    return rows
