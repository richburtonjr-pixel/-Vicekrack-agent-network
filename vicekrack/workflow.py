"""A bounded, orchestrator-owned pipeline. Outputs can never select the next stage."""

from copy import deepcopy
from uuid import uuid4

from .errors import NetworkError


STAGES = [("researcher", "research"), ("analyst", "analysis"), ("reviewer", "review")]
HARD_MAX_STEPS = 3


class CheckpointError(Exception):
    """Storage failure must escape without pretending the request outcome is known."""


def make_child(task, history, agent_id, capability, index, updated_at, child_id=None):
    child_id = child_id or str(uuid4())
    context = {"capability": capability, "design_notes": deepcopy(task["context"].get("design_notes"))}
    if history:
        context["handoff"] = {
            "version": "1.0", "task_id": task["task_id"], "stage_task_id": child_id,
            "original_request": {"instructions": task["instructions"],
                                 "design_notes": deepcopy(task["context"].get("design_notes"))},
            "recipient": agent_id, "previous_output": deepcopy(history[-1]["result"]),
            "status": "completed", "provider_used": history[-1]["provider"],
            "metadata": {"step": index, "workflow": "research_review"},
            "history": deepcopy(history),
        }
    child = {"schema_version": "1.0", "task_id": child_id, "parent_task_id": task["task_id"],
             "sender": "orchestrator", "recipient": agent_id, "instructions": task["instructions"],
             "context": context, "status": "queued", "created_at": updated_at,
             "updated_at": updated_at}
    return child


def run_workflow(runner, task, *, history=None, checkpoint=None):
    current = deepcopy(task)
    history = deepcopy(history or [])
    current["execution_trace"] = [{"step": i, "agent": row["agent"], "provider": row["provider"],
                                    "status": "completed"} for i, row in enumerate(history, 1)]
    try:
        settings = runner.workflow
        if task["context"]["workflow"] != "research_review" or not isinstance(settings, dict):
            raise NetworkError("unsupported_workflow", "Select the configured research_review workflow.")
        limit = settings.get("max_steps")
        if type(limit) is not int or not 1 <= limit <= HARD_MAX_STEPS:
            raise NetworkError("invalid_workflow", "max_steps must be an integer from 1 to 3.")
        if settings.get("agents") != [agent for agent, _ in STAGES]:
            raise NetworkError("invalid_workflow", "The workflow must be researcher, analyst, reviewer exactly once.")
        if len(STAGES) > limit:
            raise NetworkError("maximum_steps_exceeded", "The workflow exceeds its configured step budget; no agents ran.")
        # Check all routing before any potentially billable request.
        for agent_id, capability in STAGES:
            agent = runner.agents.get(agent_id)
            if not agent or not agent["enabled"] or capability not in agent["capabilities"]:
                raise NetworkError("workflow_agent_unavailable", "A required workflow agent is unavailable.")
            if (agent["execution"]["adapter"] not in runner.providers
                    or agent["execution"]["adapter"] not in {"mock", "openai", "anthropic"}):
                raise NetworkError("adapter_unavailable", "A workflow provider adapter is unavailable.")
        current = runner._transition(current, "running")
        for index, (agent_id, capability) in enumerate(STAGES, 1):
            if index <= len(history):
                continue
            if index > min(limit, HARD_MAX_STEPS):
                raise NetworkError("maximum_steps_exceeded", "Workflow step budget exhausted.")
            child = make_child(task, history, agent_id, capability, index, current["updated_at"])
            if checkpoint:
                checkpoint("before", history, current["execution_trace"], index)
            child = runner.run(child)
            provider = runner.agents[agent_id]["execution"]["adapter"]
            # Only controlled routing metadata, never prompts, outputs, env, or raw errors.
            current["execution_trace"].append({"step": index, "agent": agent_id,
                                               "provider": provider, "status": child["status"]})
            if child["status"] == "failed":
                return runner._transition(current, "failed", error={
                    "code": child["error"]["code"],
                    "message": f"Workflow stopped at {agent_id}; the stage failed. No later stages ran."})
            history.append({"agent": agent_id, "task_id": child["task_id"], "status": "completed",
                            "provider": provider, "result": deepcopy(child["result"])})
            if checkpoint:
                checkpoint("after", history, current["execution_trace"], index)
        return runner._transition(current, "completed", result={
            "summary": history[-1]["result"]["summary"],
            "data": {"workflow": "research_review", "stages": history}})
    except CheckpointError:
        raise
    except NetworkError as error:
        return runner._transition(current, "failed", error=error.as_dict())
    except Exception:
        return runner._transition(current, "failed", error={
            "code": "workflow_failed", "message": "The workflow failed unexpectedly."})
