"""Central agent inventory and an explicitly bounded workflow state machine."""
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from copy import deepcopy
from datetime import datetime, timezone
from .errors import NetworkError

PREFLIGHT_CODES = {"unsupported_workflow", "invalid_workflow", "maximum_steps_exceeded", "workflow_agent_unavailable", "adapter_unavailable", "invalid_state"}

ORDER = ["researcher", "analyst", "reviewer"]
SAFE_CODES = {"missing_credentials", "missing_model", "provider_timeout", "provider_rate_limit", "provider_error", "invalid_provider_response", "execution_failed", "invalid_agent_result", "interrupted", "invalid_handoff", "adapter_unavailable"}

def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

class AgentManager:
    def __init__(self, agents):
        self.agents = agents
        self.status = {name: "available" if row["enabled"] else "disabled" for name, row in agents.items()}
        self.handoffs = {"researcher": "analyst", "analyst": "reviewer", "reviewer": None}

    def inventory(self):
        return [{"agent": name, "capabilities": list(row["capabilities"]),
                 "provider": row["execution"]["adapter"],
                 "status": self.status[name] if row["enabled"] else "disabled",
                 "next_agent": self.handoffs.get(name)} for name, row in self.agents.items()]

    def matches(self, capability, entrypoint):
        return [row for name, row in self.agents.items() if name != entrypoint
                and row["enabled"] and capability in row["capabilities"]]

    def begin(self, agent, previous):
        expected = "researcher" if previous is None else self.handoffs.get(previous)
        if agent != expected:
            raise NetworkError("invalid_transition", "Agent handoff is not permitted.")
        if not self.agents[agent]["enabled"] or self.status[agent] == "running":
            raise NetworkError("workflow_agent_unavailable", "Agent is unavailable.")
        self.status[agent] = "running"

class WorkflowState:
    def __init__(self, task_id, max_retries=1, saved=None):
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise NetworkError("invalid_workflow", "max_retries must be an integer from 0 to 3.")
        self.data = {"task_id": task_id, "max_retries": max_retries, "completed_stages": [],
                     "current_agent": None, "next_agent": "researcher", "status": "ready",
                     "blocked_error": None, "retry_count": 0, "attempts": dict.fromkeys(ORDER, 0), "failures": [], "audit_trace": []}
        if saved is not None:
            self.validate(saved)
            if saved["task_id"] != task_id or saved["max_retries"] != max_retries:
                raise NetworkError("invalid_state", "Workflow state configuration does not match.")
            self.data = deepcopy(saved)

    @staticmethod
    def validate(data):
        try:
            schema = json.loads((Path(__file__).resolve().parent.parent / "schemas/workflow-state.schema.json").read_text())
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)
            replay = WorkflowState(data["task_id"], data["max_retries"])
            for event in data["audit_trace"]:
                if event["status"] == "running":
                    replay.begin(event["agent"], event["provider"], event["timestamp"])
                else:
                    replay.finish(event["status"] == "completed", event["error"], event["timestamp"])
                if replay.data["audit_trace"][-1] != event:
                    raise ValueError
            if data["blocked_error"] is not None:
                replay.block(data["blocked_error"])
            if replay.data != data:
                raise ValueError
        except (KeyError, TypeError, ValueError, NetworkError, IndexError, ValidationError):
            raise NetworkError("invalid_state", "Workflow state or transition history is invalid.") from None

    def event(self, status, provider, error, at):
        from datetime import datetime
        if not isinstance(at, str) or not at.endswith("Z"):
            raise ValueError("Invalid timestamp")
        current = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if self.data["audit_trace"] and current < datetime.fromisoformat(self.data["audit_trace"][-1]["timestamp"].replace("Z", "+00:00")):
            raise ValueError("Reversed timestamp")
        agent = self.data["current_agent"]
        self.data["audit_trace"].append({"agent": agent, "provider": provider,
            "stage": ORDER.index(agent)+1, "attempt": self.data["attempts"][agent],
            "timestamp": at, "status": status, "error": error})

    def block(self, code):
        if self.data["status"] not in {"ready", "failed"} or code not in PREFLIGHT_CODES:
            raise NetworkError("invalid_transition", "Invalid preflight failure.")
        self.data.update(status="failed", blocked_error=code)

    def begin(self, agent, provider, at=None):
        d = self.data
        if d["status"] in {"running", "completed", "exhausted"} or agent != d["next_agent"]:
            raise NetworkError("invalid_transition", "Workflow transition is not permitted.")
        if provider not in {"mock", "openai", "anthropic"}:
            raise NetworkError("adapter_unavailable", "Unsupported workflow provider.")
        if d["attempts"][agent] >= 1+d["max_retries"]:
            raise NetworkError("retry_exhausted", "Stage recovery budget is exhausted.")
        d["attempts"][agent] += 1
        d["retry_count"] = sum(max(0, n-1) for n in d["attempts"].values())
        d.update(current_agent=agent, status="running", blocked_error=None)
        self.event("running", provider, None, at or stamp())

    def finish(self, success, error=None, at=None):
        d = self.data
        if d["status"] != "running":
            raise NetworkError("invalid_transition", "No stage is running.")
        agent = d["current_agent"]
        code = None if success else error if error in SAFE_CODES else "execution_failed"
        self.event("completed" if success else "failed", d["audit_trace"][-1]["provider"], code, at or stamp())
        if success:
            d["completed_stages"].append(agent)
            d["next_agent"] = ORDER[len(d["completed_stages"])] if len(d["completed_stages"]) < 3 else None
            d["status"] = "ready" if d["next_agent"] else "completed"
        else:
            d["failures"].append(deepcopy(d["audit_trace"][-1]))
            d["status"] = "exhausted" if d["attempts"][agent] >= 1+d["max_retries"] else "failed"
        d["current_agent"] = None
