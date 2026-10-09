"""Local, explicitly invoked saved runs. No scheduling or credential persistence."""

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .content_events import DISABLED, EventFailure, workflow_run_ref
from .errors import NetworkError
from .manager import WorkflowState
from .handoff import validate_handoff
from .orchestrator import ROOT, Orchestrator, read_json, timestamp
from .workflow import CheckpointError, STAGES, make_child, run_workflow


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def reject_secrets(value):
    """Reject secret fields or known active credentials without altering the original data."""
    forbidden = {"apikey", "authorization", "authentication", "headers", "environment", "env", "accesstoken", "secret", "password"}
    def check(item):
        if isinstance(item, dict):
            for key, val in item.items():
                normalized = re.sub(r"[^a-z]", "", str(key).lower())
                if any(normalized.endswith(name) for name in forbidden):
                    raise NetworkError("sensitive_state", "Saved data contains a credential or environment field.")
                check(val)
        elif isinstance(item, list):
            for val in item:
                check(val)
    check(value)
    encoded = json.dumps(value, allow_nan=False)
    if re.search(r"(?:sk-(?:ant-)?|xai-)[A-Za-z0-9_-]{16,}", encoded):
        raise NetworkError("sensitive_state", "Saved data contains a credential-shaped value.")
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY"):
        secret = os.environ.get(name, "").strip()
        if secret and secret in encoded:
            raise NetworkError("sensitive_state", "Saved data contains an active credential.")


def snapshot(runner):
    agents = [{key: deepcopy(agent[key]) for key in
               ("id", "definition", "enabled", "capabilities", "execution")} for agent in runner.agents.values()]
    # Never copy arbitrary execution fields or credential/config extensions.
    for agent in agents:
        agent["execution"] = {key: agent["execution"][key] for key in ("adapter", "model")}
    paths = ["schemas/task.schema.json", "schemas/handoff.schema.json"] + [a["definition"] for a in agents]
    hashes = {path: hashlib.sha256(runner._path(path).read_bytes()).hexdigest() for path in paths}
    result = {"agents": agents, "workflow": ({key: deepcopy(runner.workflow[key]) for key in ("agents", "max_steps", "max_retries") if key in runner.workflow} if isinstance(runner.workflow, dict) else None), "contract_hashes": hashes}
    reject_secrets(result)
    return result


class RunStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else ROOT / "runtime/runs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory = self.directory.resolve()

    def path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise NetworkError("invalid_run_id", "Use the 32-character saved run ID.")
        path = self.directory / (run_id + ".json")
        if path.is_symlink():
            raise NetworkError("invalid_state", "Saved run files cannot be symbolic links.")
        return path

    @contextmanager
    def lock(self, run_id):
        path = self.path(run_id).with_suffix(".lock")
        if path.is_symlink():
            raise NetworkError("invalid_state", "Run locks cannot be symbolic links.")
        stream = path.open("a+b")
        locked = False
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                raise NetworkError("run_locked", "Another process is using this saved run.") from None
            yield
        finally:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def write(self, state):
        reject_secrets(state)
        path = self.path(state["run_id"])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=state["run_id"] + ".", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError):
            raise NetworkError("storage_error", "Could not atomically save the run; inspect it before retrying.") from None
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # An orphan .tmp is ignored; never promote it automatically.

    def read(self, run_id):
        try:
            state = read_json(self.path(run_id))
        except FileNotFoundError:
            raise NetworkError("run_not_found", "Saved run does not exist.") from None
        except (OSError, ValueError, UnicodeError):
            raise NetworkError("invalid_state", "Saved run is unreadable or contains invalid JSON.") from None
        validate_state(state, run_id)
        return state

    def inspect(self, run_id):
        with self.lock(run_id):
            state = self.read(run_id)
            if state["status"] == "running":
                state["status"] = "uncertain"
            return state

    def list_runs(self):
        results = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                state = self.inspect(path.stem)
                results.append({key: state[key] for key in ("run_id", "status", "updated_at")})
            except NetworkError as error:
                results.append({"run_id": path.stem, "error": error.code})
        return results


def validate_saved_task(task):
    schema = read_json(ROOT / "schemas/task.schema.json")
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(task)
    if timestamp(task["updated_at"]) < timestamp(task["created_at"]):
        raise ValueError("Task timestamps are reversed")


def validate_state(state, run_id):
    """Reject malformed snapshots before they can influence routing or skip stages."""
    try:
        shape = read_json(ROOT / "schemas/saved-run.schema.json")
        Draft202012Validator(shape, format_checker=FormatChecker()).validate(state)
        expected = {"version", "run_id", "registry_path", "task", "config", "history", "trace", "status",
                    "pending_stage", "created_at", "updated_at", "outcome"}
        if not isinstance(state, dict) or set(state) not in (expected, expected | {"workflow_state"}) or state["version"] != 1 or state["run_id"] != run_id:
            raise ValueError
        if state["status"] not in {"ready", "running", "uncertain", "failed", "completed"}:
            raise ValueError
        if not isinstance(state["registry_path"], str) or not isinstance(state["config"], dict):
            raise ValueError
        if (not state["created_at"].endswith("Z") or not state["updated_at"].endswith("Z")
                or timestamp(state["updated_at"]) < timestamp(state["created_at"])):
            raise ValueError
        task = state["task"]
        validate_saved_task(task)
        if (task["status"] != "queued" or task["recipient"] != "orchestrator" or "execution_trace" in task
                or task.get("context", {}).get("workflow") != "research_review"
                or not isinstance(task.get("context", {}).get("capability"), str)
                or not task["context"]["capability"].strip()):
            raise ValueError
        history = state["history"]
        if not isinstance(history, list) or len(history) > 3:
            raise ValueError
        trace = []
        ids = {task["task_id"]}
        for index, row in enumerate(history, 1):
            agent, capability = STAGES[index - 1]
            if (set(row) != {"agent", "task_id", "status", "provider", "result"}
                    or row["agent"] != agent or row["status"] != "completed" or row["task_id"] in ids):
                raise ValueError
            ids.add(row["task_id"])
            configured = next(a for a in state["config"]["agents"] if a["id"] == agent)
            if row["provider"] != configured["execution"]["adapter"]:
                raise ValueError
            child = make_child(task, history[:index-1], agent, capability, index, task["updated_at"], row["task_id"])
            if index > 1:
                validate_handoff(child, agent)
            child.update(status="completed", result=row["result"])
            validate_saved_task(child)
            trace.append({"step": index, "agent": agent, "provider": row["provider"], "status": "completed"})
        pending = state["pending_stage"]
        if pending is not None and (type(pending) is not int or pending != len(history) + 1 or pending > 3):
            raise ValueError
        if state["status"] in {"running", "uncertain"} and pending is None:
            raise ValueError
        if state["status"] in {"ready", "completed", "failed"} and pending is not None:
            raise ValueError
        outcome = state["outcome"]
        if outcome is not None:
            validate_saved_task(outcome)
            for key, value in task.items():
                if key not in {"status", "updated_at"} and outcome.get(key) != value:
                    raise ValueError
            if outcome["status"] == "completed":
                if (state["status"] != "completed" or len(history) != 3
                        or outcome["result"]["data"]["stages"] != history
                        or outcome["result"]["summary"] != history[-1]["result"]["summary"]):
                    raise ValueError
            elif outcome["status"] != "failed" or state["status"] not in {"failed", "uncertain"}:
                raise ValueError
            if outcome["execution_trace"] != state["trace"]:
                raise ValueError
        elif state["status"] in {"completed", "failed"}:
            raise ValueError
        extra = state["trace"][len(trace):]
        if state["trace"][:len(trace)] != trace or len(extra) > 1:
            raise ValueError
        if extra and state["status"] not in {"failed", "uncertain"}:
            raise ValueError
        if extra:
            agent = STAGES[len(history)][0]
            provider = next(a for a in state["config"]["agents"] if a["id"] == agent)["execution"]["adapter"]
            if extra != [{"step": len(history)+1, "agent": agent, "provider": provider, "status": "failed"}]:
                raise ValueError
        if "workflow_state" in state:
            workflow = state["workflow_state"]
            WorkflowState.validate(workflow)
            if (workflow["task_id"] != task["task_id"]
                    or workflow["completed_stages"] != [row["agent"] for row in history]
                    or workflow["max_retries"] != (state["config"]["workflow"] or {}).get("max_retries", 1)):
                raise ValueError
            if ((state["status"] == "completed" and workflow["status"] != "completed")
                    or (state["status"] == "running" and workflow["status"] != "running")
                    or (state["status"] == "ready" and workflow["status"] not in {"ready", "completed"})
                    or (state["status"] in {"failed", "uncertain"} and workflow["status"] not in {"failed", "exhausted", "ready"})):
                raise ValueError
            for event in workflow["audit_trace"]:
                configured = next(a for a in state["config"]["agents"] if a["id"] == event["agent"])
                if event["provider"] != configured["execution"]["adapter"]:
                    raise ValueError
        reject_secrets(state)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, StopIteration, NetworkError, ValidationError):
        raise NetworkError("invalid_state", "Saved run failed schema, history, handoff, or consistency validation.") from None


class SavedRuns:
    """Saved workflow runs. Step 33: optional `events` (ContentEvents) record each start/resume as its
    own timeline; all attempts of a run share one correlation ID derived from the run ID."""

    def __init__(self, store=None, events=None):
        self.store = store or RunStore()
        self.events = events or DISABLED

    def start(self, task, registry_path="config/agents.workflow.json", *, providers=None):
        runner = Orchestrator(registry_path=registry_path, providers=providers)
        runner.validate(task)
        if (task["status"] != "queued" or task["recipient"] != "orchestrator" or "execution_trace" in task
                or task.get("context", {}).get("workflow") != "research_review"
                or not isinstance(task.get("context", {}).get("capability"), str)
                or not task["context"]["capability"].strip()):
            raise NetworkError("invalid_task", "Saved runs require a queued orchestrator workflow task.")
        if not runner.agents[runner.entrypoint]["enabled"]:
            raise NetworkError("agent_disabled", "The orchestrator is disabled.")
        run_id = uuid4().hex
        state = {"version": 1, "run_id": run_id, "registry_path": registry_path, "task": deepcopy(task),
                 "config": snapshot(runner), "history": [], "trace": [], "status": "ready",
                 "pending_stage": None, "created_at": now(), "updated_at": now(), "outcome": None}
        task_lock = hashlib.sha256(task["task_id"].encode()).hexdigest()[:32]
        with self.store.lock(task_lock), self.store.lock(run_id):
            for path in self.store.directory.glob("*.json"):
                if self.store.read(path.stem)["task"]["task_id"] == task["task_id"]:
                    raise NetworkError("duplicate_task", "A saved run already owns this task ID; inspect or resume it.")
            self.store.write(state)
            return self._recorded(runner, state)

    def resume(self, run_id, *, retry_uncertain=False, registry_path=None, providers=None):
        with self.store.lock(run_id):
            state = self.store.read(run_id)
            runner = Orchestrator(registry_path=registry_path or state["registry_path"], providers=providers)
            if snapshot(runner) != state["config"]:
                raise NetworkError("configuration_mismatch", "Configuration or contract changed; restore it or start a new run.")
            if state["status"] == "completed":
                raise NetworkError("run_completed", "This run is complete; no stages were repeated.")
            if state["status"] in {"running", "uncertain"} and not retry_uncertain:
                raise NetworkError("uncertain_stage", "The pending request may have completed. Use --retry-uncertain to explicitly authorize another request and possible charge.")
            if state["status"] in {"running", "uncertain"}:
                runner.resume_hint = "retry_after_uncertain"
            return self._recorded(runner, state)

    def _recorded(self, runner, state):
        """Run with optional event recording. A recording failure stops before new work and keeps every
        committed checkpoint; the run stays resumable."""
        events = self.events
        events.bind(workflow_run_ref(state["run_id"]), workflow_run_ref(state["run_id"]))
        runner.events = events
        try:
            result = self._execute(runner, state)
        except EventFailure as failure:
            saved = self.store.read(state["run_id"])
            events.abort(failure.code)
            return {"run_id": state["run_id"], "status": saved["status"], "task": saved["outcome"],
                    "error": {"code": failure.code, "message": "Event recording failed; no new stage was started. "
                              "Completed stages are saved; resume the run to continue."}, "events": events.summary()}
        except NetworkError as error:
            events.abort(error.code)
            raise
        if events.enabled:
            events.close("completed" if result["status"] == "completed" else "failed")
            result["events"] = events.summary()
            if events.failure:
                result["error"] = {"code": events.failure, "message": "The run's result is saved, but its event "
                                   "timeline is incomplete."}
        return result

    def _execute(self, runner, state):
        workflow = WorkflowState(state["task"]["task_id"], (runner.workflow or {}).get("max_retries", 1), state.get("workflow_state"))
        if "workflow_state" not in state:
            # Legacy v1 runs did not retain attempts. Import the known completed prefix.
            for row in state["history"]:
                workflow.begin(row["agent"], row["provider"], state["updated_at"])
                workflow.finish(True, at=state["updated_at"])
            if state["status"] in {"failed", "running", "uncertain"} and len(state["history"]) < 3:
                agent = STAGES[len(state["history"])][0]
                workflow.begin(agent, runner.agents[agent]["execution"]["adapter"], state["updated_at"])
                workflow.finish(False, "interrupted", state["updated_at"])
        abandoned = workflow.data["status"] == "running"
        if abandoned:
            workflow.finish(False, "interrupted")
        if workflow.data["status"] == "exhausted":
            if abandoned:
                trace = deepcopy(state["trace"])
                agent = STAGES[len(state["history"])][0]
                trace.append({"step": len(state["history"])+1, "agent": agent,
                              "provider": runner.agents[agent]["execution"]["adapter"], "status": "failed"})
                task = deepcopy(state["task"])
                task["execution_trace"] = trace
                outcome = runner._transition(task, "failed", error={"code": "retry_exhausted",
                    "message": "An interrupted attempt exhausted the recovery budget; its remote outcome remains unknown."})
                state.update(workflow_state=workflow.data, status="failed", pending_stage=None,
                             trace=trace, outcome=outcome, updated_at=now())
                self.store.write(state)
            raise NetworkError("retry_exhausted", "Stage recovery budget is exhausted; no provider was called.")
        state["workflow_state"] = workflow.data
        def checkpoint(event, history, trace, step):
            state.update(history=deepcopy(history), trace=deepcopy(trace), updated_at=now(), outcome=None,
                         status="running" if event == "before" else "ready",
                         pending_stage=step if event == "before" else None)
            try:
                self.store.write(state)
            except NetworkError:
                raise CheckpointError from None
        try:
            outcome = run_workflow(runner, state["task"], history=state["history"], checkpoint=checkpoint, workflow_state=workflow)
            status = outcome["status"]
            # Only failures known to occur before calling a provider are safe to retry normally.
            safe = {"missing_credentials", "missing_model", "invalid_provider_configuration", "missing_research_context",
                    "invalid_handoff", "unsupported_model", "adapter_unavailable", "workflow_agent_unavailable",
                    "invalid_workflow", "unsupported_workflow", "maximum_steps_exceeded"}
            uncertain = status == "failed" and state["pending_stage"] is not None and outcome["error"]["code"] not in safe
            if workflow.data["status"] == "exhausted":
                uncertain = False
            state.update(outcome=outcome, trace=deepcopy(outcome["execution_trace"]), updated_at=now(),
                         status="uncertain" if uncertain else status,
                         pending_stage=state["pending_stage"] if uncertain else None)
            self.store.write(state)
        except (CheckpointError, NetworkError):
            raise NetworkError("storage_error", f"Checkpoint failed for run {state['run_id']}. Inspect saved state before resuming; a request may have completed.") from None
        return {"run_id": state["run_id"], "status": state["status"], "task": outcome}
