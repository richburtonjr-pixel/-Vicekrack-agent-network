"""Deterministic HQ demo (Steps 32-33): synthetic, clearly labelled, never stored, never executed.

The demo lets the house be explored without credentials or saved runs. It follows the real
workflow orders and every event passes the same Step 31 payload validation as real events:
- trading research: controller -> market scout -> trend -> strategy (fails) -> risk review
  (blocked) -> controller fails;
- the Step 5 roles: orchestrator -> Researcher -> Analyst -> Reviewer -> orchestrator;
- simulated order decisions and a fill at the simulator station (Step 34: they match the demo results
  desk's synthetic run in `results_demo.py` exactly);
- the production pipeline: brief builder -> Creator -> script validator -> scene planner ->
  preview renderer -> pipeline done, then a quality check.
Times and IDs are synthetic. It shows idle, working, waiting, blocked, completed and failed
(unknown only appears in real recorded or reconstructed histories).
"""

from datetime import datetime, timedelta, timezone

from ..events.contract import digest, event_id, validate_event
from .scene import assemble

START = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
CONTROLLER, ENGINE = "trading.research.controller", "trading.simulation.engine"
R, W, P = "trading.research.", "content.workflow.", "content.production."


def _id(prefix, name):
    return f"{prefix}-{digest({'vicekrack_hq_demo': name})[:24]}"


DATASET = {"kind": "dataset", "id": _id("mds", "dataset")}
ORDER_1, ORDER_2, FILL_1 = _id("sord", "order-1"), _id("sord", "order-2"), _id("sfil", "fill-1")
SIGNAL_1, SIGNAL_2, RUN = _id("rsig", "signal-1"), _id("rsig", "signal-2"), _id("srun", "run")
PRODUCTION = {"kind": "production", "id": _id("prod", "production")}
ONE = {"attempt": 1}

# (component, stage, event_type, status, reason_codes, refs, details, simulated minute or None)
SCRIPT = (
    (CONTROLLER, "workflow", "stage_started", "started", [], [DATASET], {}, None),
    (W + "orchestrator", "workflow", "stage_started", "started", [], [], {}, None),
    (R + "market_scout", "market_scout", "stage_started", "started", [], [], {"position": 1}, 60),
    (W + "researcher", "researcher", "stage_started", "started", [], [], ONE, None),
    (R + "market_scout", "market_scout", "stage_completed", "completed", ["coverage_ok"], [],
     {"position": 1, "conclusion": "sufficient_data"}, 60),
    (R + "trend_agent", "trend_agent", "stage_started", "started", [], [], {"position": 2}, 60),
    (W + "researcher", "researcher", "stage_completed", "completed", [], [], ONE, None),
    (W + "analyst", "analyst", "stage_started", "started", [], [], ONE, None),
    (R + "trend_agent", "trend_agent", "stage_completed", "completed", ["ema_fast_above_slow"], [],
     {"position": 2, "conclusion": "uptrend"}, 60),
    (R + "strategy_agent", "strategy_agent", "stage_started", "started", [], [], {"position": 3}, 60),
    (W + "analyst", "analyst", "stage_completed", "completed", [], [], ONE, None),
    (W + "reviewer", "reviewer", "stage_started", "started", [], [], ONE, None),
    (R + "strategy_agent", "strategy_agent", "stage_failed", "failed", ["stage_error"], [], {"position": 3}, 60),
    (R + "risk_review", "risk_review", "stage_blocked", "blocked", ["earlier_stage_failed"], [], {"position": 4}, 60),
    (CONTROLLER, "workflow", "stage_failed", "failed", ["stage_error"], [], {"verdict": "workflow_failed"}, 60),
    (W + "reviewer", "reviewer", "stage_completed", "completed", [], [], ONE, None),
    (W + "orchestrator", "workflow", "stage_completed", "completed", [], [], {}, None),
    (ENGINE, "replay", "stage_started", "started", [], [DATASET], {}, 0),
    (P + "pipeline", "pipeline", "stage_started", "started", [], [PRODUCTION], {}, None),
    (P + "brief", "brief", "stage_started", "started", [], [], ONE, None),
    (ENGINE, "replay", "order_decision", "accepted", ["signal_accepted_by_policy", "fills_at_next_available_open"],
     [{"kind": "order", "id": ORDER_1}, {"kind": "research_signal", "id": SIGNAL_1}],
     {"purpose": "entry", "side": "buy", "quantity": 10, "strategy": "ema-cross-3-5", "rule": None, "bar_sequence": 7}, 35),
    (P + "brief", "brief", "stage_completed", "completed", [], [], ONE, None),
    (P + "creator", "creator", "stage_started", "started", [], [], ONE, None),
    (ENGINE, "replay", "simulated_fill", "filled", ["filled_at_bar_open"],
     [{"kind": "fill", "id": FILL_1}, {"kind": "order", "id": ORDER_1}],
     {"purpose": "entry", "side": "buy", "quantity": 10, "bar_sequence": 8}, 35),
    (P + "creator", "creator", "stage_completed", "completed", [], [], ONE, None),
    (ENGINE, "replay", "order_decision", "rejected", ["position_already_open"],
     [{"kind": "order", "id": ORDER_2}, {"kind": "research_signal", "id": SIGNAL_2}],
     {"purpose": "entry", "side": "buy", "quantity": 10, "strategy": "breakout-3", "rule": None, "bar_sequence": 9}, 45),
    (P + "validate", "validate", "stage_started", "started", [], [], ONE, None),
    (ENGINE, "replay", "stage_completed", "completed", [], [{"kind": "simulation_run", "id": RUN}], {}, 50),
    (P + "validate", "validate", "stage_completed", "completed", [], [], ONE, None),
    (P + "plan", "plan", "stage_started", "started", [], [], ONE, None),
    (P + "plan", "plan", "stage_completed", "completed", [], [], ONE, None),
    (P + "preview", "preview", "stage_started", "started", [], [], ONE, None),
    (P + "preview", "preview", "stage_completed", "completed", [], [], ONE, None),
    (P + "pipeline", "pipeline", "stage_completed", "completed", [], [PRODUCTION], {}, None),
    (P + "quality", "quality", "stage_started", "started", [], [PRODUCTION], {}, None),
    (P + "quality", "quality", "stage_completed", "completed", ["result_pass"], [PRODUCTION], {}, None),
)


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def demo_events():
    events = []
    for number, (component, stage, kind, status, reasons, refs, details, minute) in enumerate(SCRIPT, start=1):
        department = component.split(".")[0]
        event = {"sequence": number, "event_type": kind, "status": status, "component": component, "stage": stage,
                 "department": department, "sim_time_utc": _stamp(START + timedelta(minutes=minute)) if minute is not None
                 else None, "recorded_at": _stamp(START + timedelta(seconds=20 * number)), "reason_codes": list(reasons),
                 "refs": [dict(r) for r in refs], "details": dict(details), "run_id": None}
        # Same payload rules as real events: checked through the Step 31 validator.
        probe = {"contract": "execution_event", "version": "1.0", "event_id": event_id("rtl-" + "0" * 24, number),
                 "origin": "reconstructed", "timeline_id": "rtl-" + "0" * 24, "correlation_id": "cor-" + "0" * 24,
                 **{k: event[k] for k in ("run_id", "department", "component", "stage", "sequence", "event_type", "status",
                                          "sim_time_utc", "recorded_at", "reason_codes", "refs", "details")}}
        validate_event(probe)
        events.append(event)
    return events


def demo_scene():
    events = demo_events()
    components = sorted({e["component"] for e in events})
    timeline = {"timeline_id": "demo", "origin": "demo", "department": "both", "kind": "hq_demo", "completeness": "complete",
                "outcome": "completed", "live": False, "time_basis": "synthetic", "started_at": _stamp(START),
                "event_count": len(events), "run_id": None, "source": None, "issues": [], "correlation_id": None}
    return assemble(mode="demo", timeline=timeline, components=components, events=events)
