"""Trading execution events (Step 31): instrumentation helper and read-only reconstruction.

`TradingEvents` wraps an optional sink for the research-agent controller and the
simulator. With no sink it does nothing. A sink failure becomes a TradingError with the
same fixed code, so trading commands fail explicitly and save nothing.

`from_agent_run` / `from_simulation_run` rebuild a timeline from a validated saved run.
They are reconstructions: saved trading runs store no per-stage wall-clock times, so every
`recorded_at` is null and only simulated times appear. Order is deterministic: research
stages in their fixed order; simulation events by simulated time, then decisions before
fill-time outcomes before end-of-data, then order record order. With coarse replay steps
that can differ from the original emission order. Nothing here runs a workflow or a
simulation, or touches accounts.
"""

from ..errors import NetworkError
from ..events.contract import build_view, digest, event_id, validate_event
from ..events.sink import NullSink
from .errors import TradingError

STAGES = ("market_scout", "trend_agent", "strategy_agent", "risk_review")
AGENT_CONTROLLER = "trading.research.controller"
AGENT_COMPONENTS = (AGENT_CONTROLLER,) + tuple(f"trading.research.{role}" for role in STAGES)
SIM_ENGINE = "trading.simulation.engine"
SIM_COMPONENTS = (SIM_ENGINE,)
EVENT_CODES = {"event_persistence_failed", "event_limit_reached", "event_duplicate", "invalid_event",
               "invalid_event_transition", "event_timeline_exists", "event_storage_full"}


def open_recorder(kind, root=None, correlation_id=None):
    """A persistent Recorder for one trading command; storage errors become TradingErrors.
    A Step 39 session passes its own correlation ID so its stage timelines can be grouped."""
    from ..events.sink import Recorder
    from ..events.store import EventStore
    components = AGENT_COMPONENTS if kind == "research_agent_workflow" else SIM_COMPONENTS
    try:
        return Recorder(department="trading", kind=kind, components=components, correlation_id=correlation_id,
                        store=EventStore(root))
    except NetworkError as error:
        raise TradingError(error.code, error.message) from None


def close_recorder(recorder, outcome):
    if recorder is None:
        return
    try:
        recorder.close(outcome)
    except NetworkError as error:
        raise TradingError(error.code, error.message) from None


class TradingEvents:
    def __init__(self, sink=None):
        self.sink = sink if sink is not None else NullSink()

    @property
    def enabled(self):
        return self.sink.enabled

    def emit(self, component, stage, event_type, status, **fields):
        if not self.sink.enabled:
            return
        try:
            self.sink.emit(component, stage, event_type, status, **fields)
        except NetworkError as error:
            raise TradingError(error.code, error.message) from None


def order_event_fields(order, entry):
    """Controlled metadata for one simulated order history entry."""
    source = order["source"]
    refs = [{"kind": "order", "id": order["order_id"]}]
    if source["signal_id"]:
        refs.append({"kind": "research_signal", "id": source["signal_id"]})
    return {"sim_time": entry["at_utc"], "reason_codes": entry["reason_codes"][:10], "refs": refs,
            "details": {"purpose": order["purpose"], "side": order["side"], "quantity": order["quantity"],
                        "strategy": source["strategy"], "rule": source["rule"],
                        "bar_sequence": source["decision_bar_sequence"]}}


def fill_event_fields(fill, order, sim_time):
    return {"sim_time": sim_time, "reason_codes": fill["reason_codes"][:10],
            "refs": [{"kind": "fill", "id": fill["fill_id"]}, {"kind": "order", "id": order["order_id"]}],
            "details": {"purpose": order["purpose"], "side": fill["side"], "quantity": fill["quantity"],
                        "bar_sequence": fill["bar_sequence"]}}


# ---------------------------------------------------------------- reconstruction
class _Builder:
    def __init__(self, timeline_id, correlation_id, run_id):
        self.timeline_id, self.correlation_id, self.run_id, self.events = timeline_id, correlation_id, run_id, []

    def add(self, component, stage, event_type, status, sim_time=None, reason_codes=(), refs=(), details=None):
        sequence = len(self.events) + 1
        self.events.append({"contract": "execution_event", "version": "1.0", "event_id": event_id(self.timeline_id, sequence),
                            "origin": "reconstructed", "timeline_id": self.timeline_id,
                            "correlation_id": self.correlation_id, "run_id": self.run_id, "department": "trading",
                            "component": component, "stage": stage, "sequence": sequence, "event_type": event_type,
                            "status": status, "sim_time_utc": sim_time, "recorded_at": None,
                            "reason_codes": list(reason_codes)[:10], "refs": list(refs), "details": dict(details or {})})
        try:
            validate_event(self.events[-1])
        except NetworkError as error:
            raise TradingError(error.code, error.message) from None


def _ids(run):
    return ("rtl-" + digest({"source": run["run_id"], "results": run["results_sha256"]})[:24],
            "cor-" + digest({"run": run["run_id"]})[:24])


def _source(kind, run):
    return {"kind": kind, "id": run["run_id"], "saved_at": run["created_at"],
            "saved_at_meaning": "When the saved result was created. Stage start and end times were not stored."}


def from_agent_run(run):
    timeline_id, correlation_id = _ids(run)
    build = _Builder(timeline_id, correlation_id, run["run_id"])
    sim = run["sim_time_utc"]
    dataset = [{"kind": "dataset", "id": run["dataset"]["dataset_id"]}]
    build.add(AGENT_CONTROLLER, "workflow", "stage_started", "started", None, refs=dataset)   # as recorded
    for stage in run["stages"]:
        component, details = f"trading.research.{stage['role']}", {"position": stage["position"]}
        if stage["status"] == "not_run":
            build.add(component, stage["role"], "stage_blocked", "blocked", sim, ["earlier_stage_failed"], details=details)
            continue
        build.add(component, stage["role"], "stage_started", "started", sim, details=details)
        if stage["status"] == "completed":
            build.add(component, stage["role"], "stage_completed", "completed", sim, stage["reason_codes"],
                      details={**details, "conclusion": stage["conclusion"]})
        else:
            build.add(component, stage["role"], "stage_failed", "failed", sim, stage["reason_codes"], details=details)
    own = [{"kind": "research_agent_run", "id": run["run_id"]}]
    if run["status"] == "completed":
        build.add(AGENT_CONTROLLER, "workflow", "stage_completed", "completed", sim, refs=own,
                  details={"verdict": run["final"]["verdict"]})
    else:
        build.add(AGENT_CONTROLLER, "workflow", "stage_failed", "failed", sim, [run["failure"]["code"]], refs=own,
                  details={"verdict": run["final"]["verdict"]})
    return build_view(timeline_id=timeline_id, origin="reconstructed", department="trading", kind="research_agent_workflow",
                      correlation_id=correlation_id, run_id=run["run_id"], source=_source("research_agent_run", run),
                      time_basis="simulated_only", started_at=None, components=AGENT_COMPONENTS, events=build.events,
                      completeness="complete", outcome=run["status"], live=False, issues=[])


def from_simulation_run(run):
    timeline_id, correlation_id = _ids(run)
    build = _Builder(timeline_id, correlation_id, run["run_id"])
    fills = {f["fill_id"]: f for f in run["fills"]}
    items = []
    for index, order in enumerate(run["orders"]):
        for position, entry in enumerate(order["history"]):
            if position == 0:
                rank, kind = 0, ("order_decision", entry["status"])
            elif entry["status"] == "filled":
                rank, kind = 1, ("simulated_fill", "filled")
            elif entry["status"] == "rejected":
                rank, kind = 1, ("order_decision", "rejected")
            else:
                rank, kind = 2, ("order_decision", "pending_at_end_of_data")
            items.append((entry["at_utc"], rank, index, position, order, entry, kind))
    kill = ["kill_switch_engaged"] if run["kill_switch"]["engaged"] else []
    build.add(SIM_ENGINE, "replay", "stage_started", "started", run["replay"]["start_utc"], kill,
              [{"kind": "dataset", "id": run["dataset"]["dataset_id"]}])
    for at, _, _, _, order, entry, (event_type, status) in sorted(items, key=lambda item: item[:4]):
        if event_type == "simulated_fill":
            build.add(SIM_ENGINE, "replay", event_type, status, **fill_event_fields(fills[order["fill_id"]], order, at))
        else:
            build.add(SIM_ENGINE, "replay", event_type, status, **order_event_fields(order, entry))
    build.add(SIM_ENGINE, "replay", "stage_completed", "completed", run["summary"]["last_bar_close_utc"] or run["replay"]["end_utc"],
              refs=[{"kind": "simulation_run", "id": run["run_id"]}])
    return build_view(timeline_id=timeline_id, origin="reconstructed", department="trading", kind="simulation",
                      correlation_id=correlation_id, run_id=run["run_id"], source=_source("simulation_run", run),
                      time_basis="simulated_only", started_at=None, components=SIM_COMPONENTS, events=build.events,
                      completeness="complete", outcome="completed", live=False, issues=[])
