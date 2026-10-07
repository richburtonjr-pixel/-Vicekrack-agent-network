"""Research-agent controller (Step 28): fixed four-stage sequence, validated handoffs.

Sequence (fixed, at most four stages): market_scout -> trend_agent -> strategy_agent ->
risk_review. The controller alone builds the evidence, calls each handler once with its
own evidence slice and read-only copies of earlier handoffs, validates the output, and
records it. Handlers cannot launch tasks, pick a successor or retry: they get no
reference to the controller and their output schema has no field for it.

Failure states: a handler exception, an output that fails the `agent_output` schema or
exceeds `max_handoff_bytes`, or a stage that runs longer than `stage_timeout_seconds`
(measured after the deterministic handler returns; its output is then discarded) marks
that stage `failed`. The run stops immediately: later stages are `not_run`, the run is
`failed`, and nothing is retried. Agent conclusions such as `insufficient_data` are not
failures; the workflow still completes and explains them.

Execution events (Step 31): with an optional `events` sink the controller emits
stage_started / stage_completed / stage_failed for itself and each stage, and
stage_blocked for stages not run after a failure. Events never enter the run or its
hashes; a sink failure stops the workflow with that event code (no retry).

The controller never imports the paper-account, risk-engine, order or journal modules.
"""

import json
import time
from copy import deepcopy

from ..contracts import reject_trading_secrets, sha256, validate_schema
from ..errors import TradingError
from .analysis import NoAnalysisLayer
from .evidence import build_evidence
from .handlers import default_handlers
from ..timeline import AGENT_CONTROLLER, TradingEvents

STAGES = ("market_scout", "trend_agent", "strategy_agent", "risk_review")
NOTICE = ("Research-only workflow output from deterministic local handlers on historical data at a simulated time. "
          "Not advice, not a profitability claim, not an order authorization; no account access.")


def _validate_handlers(handlers):
    if len(handlers) > 4:
        raise TradingError("too_many_stages", "The research workflow allows at most four stages.")
    if tuple(getattr(h, "role", None) for h in handlers) != STAGES:
        raise TradingError("invalid_transition",
                           "Stages must be exactly market_scout, trend_agent, strategy_agent, risk_review in that order.")


def _handoff(position, role, handler, sim_time, **values):
    record = {"position": position, "role": role, "handler": handler, "status": "not_run", "conclusion": None,
              "summary": "", "findings": {}, "reason_codes": [], "limitations": [], "input_sha256": None,
              "output_sha256": None, "sim_time_utc": sim_time}
    record.update(values)
    return record


def run_workflow(dataset, *, workflow_config, market_config, indicator_config, signal_config, created_at,
                 sim_time=None, handlers=None, analysis_layer=None, strategies=None, monotonic=time.monotonic,
                 events=None):
    if strategies:                        # explicit override; the run records the effective configuration's hash
        workflow_config = {**workflow_config, "strategies": list(strategies)}
    validate_schema("research_agent_config", workflow_config)
    if workflow_config["trend"]["ema_fast"] >= workflow_config["trend"]["ema_slow"]:
        raise TradingError("invalid_agent_config", "trend.ema_fast must be smaller than trend.ema_slow.")
    handlers = list(handlers) if handlers is not None else default_handlers()
    _validate_handlers(handlers)
    layer = analysis_layer or NoAnalysisLayer()
    if layer.name != "none":
        raise TradingError("analysis_layer_unavailable", "Only the 'none' analysis layer exists in this step.")

    events = TradingEvents(events)
    dataset_ref = [{"kind": "dataset", "id": dataset["dataset_id"]}]
    # The requested simulated time is not validated yet, so the first events carry none.
    events.emit(AGENT_CONTROLLER, "workflow", "stage_started", "started", refs=dataset_ref)
    try:
        evidence, hashes = build_evidence(dataset, sim_time=sim_time, workflow_config=workflow_config,
                                          market_config=market_config, indicator_config=indicator_config,
                                          signal_config=signal_config, created_at=created_at)
    except TradingError as error:
        events.emit(AGENT_CONTROLLER, "workflow", "stage_failed", "failed", reason_codes=[error.code])
        raise
    sim_time = evidence["sim_time_utc"]
    identities = [getattr(h, "identity", f"{h.role}@1.0") for h in handlers]
    run_id = "rar-" + sha256({"dataset_id": dataset["dataset_id"], "bars": dataset["bars_sha256"],
                              "config": sha256(workflow_config), "sim_time": sim_time, "handlers": identities})[:24]
    if events.enabled:
        events.sink.run_id = run_id
    limits = workflow_config["limits"]
    stages, failure = [], None
    for position, handler in enumerate(handlers, start=1):
        role, identity = handler.role, identities[position - 1]
        component, details = f"trading.research.{role}", {"position": position}
        if failure is not None:
            stages.append(_handoff(position, role, identity, sim_time))
            events.emit(component, role, "stage_blocked", "blocked", sim_time=sim_time,
                        reason_codes=["earlier_stage_failed"], details=details)
            continue
        events.emit(component, role, "stage_started", "started", sim_time=sim_time, details=details)
        stage_input = {"evidence": {"sim_time_utc": sim_time, **evidence[role]}, "prior": deepcopy(stages)}
        input_sha = sha256(stage_input)
        started = monotonic()
        try:
            output = handler.analyze(deepcopy(stage_input["evidence"]), tuple(deepcopy(stages)))
            elapsed = monotonic() - started
            if elapsed > limits["stage_timeout_seconds"]:
                raise TradingError("stage_timeout", "The stage exceeded its time budget; its output was discarded.")
            _check_output(output, limits["max_handoff_bytes"])
        except TradingError as error:
            code = error.code if error.code in {"stage_timeout", "invalid_handoff", "handoff_too_large"} else "stage_error"
            failure = {"role": role, "code": code}
        except Exception:
            failure = {"role": role, "code": "stage_error"}
        if failure is not None:
            stages.append(_handoff(position, role, identity, sim_time, status="failed", reason_codes=[failure["code"]],
                                   input_sha256=input_sha, summary="The stage failed; later stages were not run."))
            events.emit(component, role, "stage_failed", "failed", sim_time=sim_time, reason_codes=[failure["code"]],
                        details=details)
            continue
        layer.commentary(role, stage_input["evidence"], output)          # "none": always None, never stored
        stages.append(_handoff(position, role, identity, sim_time, status="completed", conclusion=output["conclusion"],
                               summary=output["summary"], findings=output["findings"],
                               reason_codes=output["reason_codes"], limitations=output["limitations"],
                               input_sha256=input_sha, output_sha256=sha256(output)))
        events.emit(component, role, "stage_completed", "completed", sim_time=sim_time,
                    reason_codes=output["reason_codes"][:10], details={**details, "conclusion": output["conclusion"]})

    review = stages[-1]
    verdict = ("workflow_failed" if failure else review["conclusion"])
    final = {"verdict": verdict, "research_only": True, "authorization_possible": False,
             "explanation": [{"role": s["role"], "status": s["status"], "conclusion": s["conclusion"], "summary": s["summary"],
                              "limitations": s["limitations"]} for s in stages]}
    body = {"sim_time_utc": sim_time, "dataset": evidence["market_scout"]["dataset"],
            "hashes": {"workflow_config_sha256": sha256(workflow_config), **hashes},
            "analysis_layer": {"name": "none", "advisory_only": True}, "status": "failed" if failure else "completed",
            "failure": failure, "stages": stages, "final": final}
    run = {
        "contract": "research_agent_run", "version": "1.0", "run_id": run_id,
        "mode": "historical_replay", "research_only": True, "authorization_possible": False, "account_access": False,
        **body, "results_sha256": sha256(body), "created_at": created_at, "notice": NOTICE,
    }
    validate_run(run)
    own = [{"kind": "research_agent_run", "id": run_id}]
    if failure:
        events.emit(AGENT_CONTROLLER, "workflow", "stage_failed", "failed", sim_time=sim_time,
                    reason_codes=[failure["code"]], refs=own, details={"verdict": verdict})
    else:
        events.emit(AGENT_CONTROLLER, "workflow", "stage_completed", "completed", sim_time=sim_time, refs=own,
                    details={"verdict": verdict})
    return run


def _check_output(output, max_bytes):
    try:
        validate_schema("research_agent_output", output)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise TradingError("invalid_handoff", "The stage output contains credential-like data.") from None
        raise TradingError("invalid_handoff", "The stage output does not match the agent_output contract.") from None
    if len(json.dumps(output, ensure_ascii=False)) > max_bytes:
        raise TradingError("handoff_too_large", "The stage output exceeds max_handoff_bytes.")


def validate_run(run):
    validate_schema("research_agent_run", run)
    body = {k: run[k] for k in ("sim_time_utc", "dataset", "hashes", "analysis_layer", "status", "failure", "stages", "final")}
    if sha256(body) != run["results_sha256"]:
        raise TradingError("invalid_agent_run", "Research agent run hash mismatch.")
    if tuple(s["role"] for s in run["stages"]) != STAGES or [s["position"] for s in run["stages"]] != [1, 2, 3, 4]:
        raise TradingError("invalid_agent_run", "Stages are not the fixed four-stage sequence.")
    statuses = [s["status"] for s in run["stages"]]
    if run["failure"] is None:
        if statuses != ["completed"] * 4 or run["status"] != "completed":
            raise TradingError("invalid_agent_run", "A completed run needs four completed stages.")
    else:
        index = STAGES.index(run["failure"]["role"])
        if (run["status"] != "failed" or statuses[:index] != ["completed"] * index or statuses[index] != "failed"
                or statuses[index + 1:] != ["not_run"] * (3 - index) or run["final"]["verdict"] != "workflow_failed"):
            raise TradingError("invalid_agent_run", "Failure state is inconsistent.")
    for stage in run["stages"]:
        if stage["sim_time_utc"] != run["sim_time_utc"]:
            raise TradingError("invalid_agent_run", "Every stage must use the run's simulated time.")
    reject_trading_secrets(run)
