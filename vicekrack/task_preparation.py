"""Prepare local workflow inputs without running agents or inspecting provider accounts."""
import argparse
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from .errors import NetworkError
from .orchestrator import ROOT, Orchestrator, read_json
from .persistence import now, reject_secrets, snapshot
from .workflow import STAGES, preflight_workflow


def validate_task(task, registry="config/agents.workflow.json"):
    runner = Orchestrator(registry_path=registry)
    # Reject sensitive content before a schema diagnostic could include user field names.
    reject_secrets(task)
    runner.validate(task)
    context = task.get("context", {})
    if (task["status"] != "queued" or task["recipient"] != "orchestrator"
            or "execution_trace" in task or "handoff" in context
            or context.get("workflow") != "research_review" or context.get("capability") != "research"
            or not task["instructions"].strip()):
        raise NetworkError("invalid_task", "Prepare a queued research_review task addressed to the orchestrator.")
    if not runner.agents[runner.entrypoint]["enabled"]:
        raise NetworkError("agent_disabled", "The orchestrator is disabled.")
    notes = context.get("design_notes")
    if not isinstance(notes, list) or not notes or not all(isinstance(n, str) and n.strip() for n in notes):
        raise NetworkError("missing_research_context", "Supply at least one nonblank design note.")
    preflight_workflow(runner, task)
    snapshot(runner)  # Validate the allowlisted configuration for later persistence.
    route = []
    for agent, _ in STAGES:
        execution = runner.agents[agent]["execution"]
        if execution["adapter"] != "mock" and not execution["model"]:
            raise NetworkError("missing_model", "Configure a model for each real provider.")
        route.append({"agent": agent, "provider": execution["adapter"]})
    return {"valid": True, "route": route, "max_steps": runner.workflow["max_steps"],
            "max_retries": runner.workflow.get("max_retries", 1),
            "scope": "Local input and routing checks only; credentials, account access and duplicate saved IDs are checked at execution."}


def create_task(instructions, notes, registry="config/agents.workflow.json", *, directory=None):
    timestamp = now()
    task = {"schema_version": "1.0", "task_id": str(uuid4()), "sender": "user",
            "recipient": "orchestrator", "instructions": instructions,
            "context": {"capability": "research", "workflow": "research_review", "design_notes": notes},
            "status": "queued", "created_at": timestamp, "updated_at": timestamp}
    validation = validate_task(task, registry)
    folder = Path(directory) if directory is not None else ROOT / "runtime/tasks"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (task["task_id"] + ".json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(task, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish a complete file without ever overwriting an existing task.
        os.link(temporary, destination)
    except OSError:
        raise NetworkError("task_write_failed", "Cannot publish task file; check local filesystem permissions and hard-link support.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"task_file": str(destination.resolve()), "task_id": task["task_id"], **validation}


def main():
    parser = argparse.ArgumentParser(description="Create or validate a local workflow task; no provider requests")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-task")
    request = create.add_mutually_exclusive_group(required=True)
    request.add_argument("--instructions")
    request.add_argument("--instructions-file", type=Path)
    notes = create.add_mutually_exclusive_group(required=True)
    notes.add_argument("--note", action="append", help="Repeat for each evidence note")
    notes.add_argument("--notes-file", type=Path, help="UTF-8 text; each nonblank line is one note")
    create.add_argument("--registry", default="config/agents.workflow.json")
    validate = sub.add_parser("validate-task")
    validate.add_argument("task", type=Path)
    validate.add_argument("--registry", default="config/agents.workflow.json")
    args = parser.parse_args()
    try:
        if args.command == "create-task":
            instructions = args.instructions_file.read_text(encoding="utf-8-sig") if args.instructions_file else args.instructions
            notes = [line.strip() for line in args.notes_file.read_text(encoding="utf-8-sig").splitlines() if line.strip()] if args.notes_file else args.note
            result = create_task(instructions, notes, args.registry)
        else:
            result = validate_task(read_json(args.task), args.registry)
        print(json.dumps(result, indent=2))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
