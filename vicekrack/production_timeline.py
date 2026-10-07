"""Content production timelines (Step 31): read-only reconstruction from saved production state.

Reads `runtime/productions/<prod-id>/state.json` through `ProductionStore.read` (no lock,
no writes) and turns its bounded stage trace into execution events for the content
department. Trace entries carry the times the pipeline itself recorded, so
`recorded_at` is the source's own time (`time_basis: source_recorded`); nothing is
invented.

Mapping: started -> stage_started; completed -> stage_completed; failed -> stage_failed;
uncertain or interrupted -> stage_interrupted (state unknown). The trace keeps only its
newest 100 entries, so a full trace is reported `partial` (`trace_may_be_truncated`). A
production that has not finished is `open`, and its liveness is NOT checked: a stage saved
as running is shown `unknown`, never `working`. This module never imports trading code.
"""

from .errors import NetworkError
from .events.contract import build_view, digest, event_id, fold, validate_event
from .production import MAX_TRACE, STAGES, ProductionStore

COMPONENTS = tuple(f"content.production.{name}" for name in STAGES)
MAPPING = {"started": ("stage_started", "started"), "completed": ("stage_completed", "completed"),
           "failed": ("stage_failed", "failed"), "uncertain": ("stage_interrupted", "uncertain"),
           "interrupted": ("stage_interrupted", "interrupted")}
EXPECTED = {"pending": {"idle"}, "running": {"working"}, "completed": {"completed"}, "failed": {"failed", "unknown"},
            "uncertain": {"unknown", "failed"}}
OUTCOMES = {"completed": "completed", "failed": "failed", "uncertain": "failed"}


def from_production(state):
    production_id = state["production_id"]
    timeline_id = "rtl-" + digest({"source": production_id, "updated_at": state["updated_at"], "trace": state["trace"]})[:24]
    correlation_id = "cor-" + digest({"run": production_id})[:24]
    events = []
    for entry in state["trace"]:
        event_type, status = MAPPING[entry["event"]]
        sequence = len(events) + 1
        events.append(validate_event({
            "contract": "execution_event", "version": "1.0", "event_id": event_id(timeline_id, sequence),
            "origin": "reconstructed", "timeline_id": timeline_id, "correlation_id": correlation_id, "run_id": production_id,
            "department": "content", "component": f"content.production.{entry['stage']}", "stage": entry["stage"],
            "sequence": sequence, "event_type": event_type, "status": status, "sim_time_utc": None,
            "recorded_at": entry["at"], "reason_codes": [entry["error_code"]] if entry["error_code"] else [],
            "refs": [{"kind": "production", "id": production_id}], "details": {}}))
    issues = ["trace_may_be_truncated"] if len(state["trace"]) >= MAX_TRACE else []
    # Cross-check the trace against the saved stage statuses.
    folded, _, _ = fold(events, COMPONENTS)
    for stage in state["stages"]:
        if folded[f"content.production.{stage['name']}"] not in EXPECTED.get(stage["status"], set()):
            issues.append("trace_disagrees_with_state")
            break
    finished = state["status"] in OUTCOMES
    return build_view(timeline_id=timeline_id, origin="reconstructed", department="content", kind="content_production",
                      correlation_id=correlation_id, run_id=production_id,
                      source={"kind": "production", "id": production_id, "saved_at": state["updated_at"],
                              "saved_at_meaning": "When the production state was last saved. Event times come from its trace."},
                      time_basis="source_recorded", started_at=state["created_at"], components=COMPONENTS, events=events,
                      completeness="complete" if finished else "open", outcome=OUTCOMES.get(state["status"]), live=False,
                      issues=issues)


def load_production_timeline(production_id, root=None):
    return from_production(ProductionStore(root).read(production_id))


def list_production_timelines(root=None):
    store, rows = ProductionStore(root), []
    for production_id in store.list_ids():
        try:
            view = load_production_timeline(production_id, root)
            rows.append({"source_id": production_id, "origin": "reconstructed", "department": "content",
                         "kind": view["kind"], "completeness": view["completeness"], "outcome": view["outcome"],
                         "event_count": view["event_count"], "readable": True})
        except (NetworkError, OSError, ValueError, KeyError, TypeError):
            rows.append({"source_id": production_id, "origin": "reconstructed", "department": "content", "readable": False})
    return rows
