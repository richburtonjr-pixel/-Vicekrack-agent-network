"""Sequential registry-based routing with validated task inputs and outcomes."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from .errors import NetworkError
from .providers import default_providers
from .researcher import run_research


ROOT = Path(__file__).resolve().parent.parent


def read_json(path: Path):
    def reject_constant(value):
        raise ValueError("Non-finite numbers are not JSON values")

    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_constant)


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Orchestrator:
    def __init__(self, root: Path = ROOT, *, providers=None, registry_path="config/agents.json"):
        self.root = Path(root).resolve()
        self.seen_ids = set()
        self.providers = default_providers() if providers is None else dict(providers)
        self.handlers = {"researcher": run_research}
        try:
            registry = read_json(self._path(registry_path))
            self._load_registry(registry)
            schema = read_json(self._path(registry["task_schema"]))
            Draft202012Validator.check_schema(schema)
            self.validator = Draft202012Validator(schema, format_checker=FormatChecker())
        except NetworkError:
            raise
        except Exception:
            raise NetworkError("invalid_configuration", "Cannot load a valid registry and task schema.") from None

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise NetworkError("invalid_configuration", "Registry paths must remain inside the project.")
        return path

    def _load_registry(self, registry):
        if registry["registry_version"] != "1.0" or not registry["agents"]:
            raise ValueError("Unsupported or empty registry")
        self.agents = {}
        for agent in registry["agents"]:
            identifier = agent["id"]
            if not isinstance(identifier, str) or not identifier or identifier in self.agents:
                raise ValueError("Invalid or duplicate agent ID")
            if type(agent["enabled"]) is not bool:
                raise ValueError("Invalid enabled flag")
            capabilities = agent["capabilities"]
            if not isinstance(capabilities, list) or not capabilities or not all(
                isinstance(item, str) and item.strip() for item in capabilities
            ):
                raise ValueError("Invalid capabilities")
            if not self._path(agent["definition"]).is_file():
                raise ValueError("Missing role definition")
            for field in ("adapter", "model"):
                value = agent["execution"][field]
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise ValueError("Invalid execution setting")
            self.agents[identifier] = agent
        self.entrypoint = registry["default_agent"]
        if self.entrypoint != "orchestrator" or self.entrypoint not in self.agents:
            raise ValueError("Step 2 requires the orchestrator entrypoint")

    def validate(self, task):
        try:
            # Also reject Python objects/NaN that are not interoperable JSON.
            json.dumps(task, allow_nan=False)
            error = next(self.validator.iter_errors(task), None)
            if error is not None:
                location = ".".join(str(part) for part in error.absolute_path) or "$"
                raise NetworkError("invalid_task", f"Task schema validation failed at {location} ({error.validator}).")
            if timestamp(task["updated_at"]) < timestamp(task["created_at"]):
                raise NetworkError("invalid_task", "updated_at must not precede created_at.")
        except NetworkError:
            raise
        except (ValueError, TypeError, OverflowError):
            raise NetworkError("invalid_task", "Task must contain valid JSON and UTC timestamps.") from None

    def _transition(self, task, status, **outcome):
        allowed = {"queued": {"running", "failed"}, "running": {"completed", "failed"}}
        if status not in allowed.get(task["status"], set()):
            raise NetworkError("invalid_transition", "Task lifecycle transition is not allowed.")
        updated = deepcopy(task)
        updated["status"] = status
        now = datetime.now(timezone.utc)
        updated["updated_at"] = max(now, timestamp(task["updated_at"])).isoformat().replace("+00:00", "Z")
        updated.update(outcome)
        self.validate(updated)
        return updated

    def run(self, task):
        """Return a terminal task; reject invalid inputs with NetworkError."""
        self.validate(task)
        if task["status"] != "queued":
            raise NetworkError("invalid_status", "Only queued tasks may be submitted.")
        if task["task_id"] in self.seen_ids:
            raise NetworkError("duplicate_task", "Task ID has already been submitted to this orchestrator instance.")
        self.seen_ids.add(task["task_id"])
        current = deepcopy(task)
        try:
            recipient = self.agents.get(current["recipient"])
            if recipient is None:
                raise NetworkError("unknown_agent", "The requested recipient is not registered.")
            if not recipient["enabled"]:
                raise NetworkError("agent_disabled", "The requested recipient is disabled.")
            capability = current.get("context", {}).get("capability")
            if not isinstance(capability, str) or not capability.strip():
                raise NetworkError("missing_capability", "Set context.capability to a nonblank capability name.")
            if recipient["id"] == self.entrypoint:
                matches = [agent for agent in self.agents.values() if agent["id"] != self.entrypoint
                           and agent["enabled"] and capability in agent["capabilities"]]
                if not matches:
                    raise NetworkError("unsupported_capability", "No enabled worker supports the requested capability.")
                if len(matches) > 1:
                    raise NetworkError("ambiguous_capability", "Multiple workers match; address a specific recipient.")
                current = self._transition(current, "running")
                child = deepcopy(current)
                child.update(task_id=str(uuid4()), parent_task_id=current["task_id"],
                             sender=self.entrypoint, recipient=matches[0]["id"], status="queued",
                             created_at=current["updated_at"], updated_at=current["updated_at"])
                child = self.run(child)
                if child["status"] == "failed":
                    return self._transition(current, "failed", error=child["error"])
                result = {"summary": child["result"]["summary"], "data": {"delegated_task": child}}
            else:
                if capability not in recipient["capabilities"]:
                    raise NetworkError("unsupported_capability", "The recipient does not support the requested capability.")
                handler = self.handlers.get(recipient["id"])
                if handler is None:
                    raise NetworkError("agent_unavailable", "No local implementation is registered for this agent.")
                provider = self.providers.get(recipient["execution"]["adapter"])
                if provider is None:
                    raise NetworkError("adapter_unavailable", "The configured provider adapter is not available.")
                current = self._transition(current, "running")
                result = handler(deepcopy(current), provider, recipient["execution"]["model"])
            try:
                return self._transition(current, "completed", result=result)
            except NetworkError:
                raise NetworkError("invalid_agent_result", "Agent returned a result that does not match the task schema.") from None
        except NetworkError as error:
            return self._transition(current, "failed", error=error.as_dict())
        except Exception:
            # Do not expose provider exception text, which may contain credentials.
            return self._transition(current, "failed", error={
                "code": "execution_failed", "message": "Agent execution failed unexpectedly."
            })
