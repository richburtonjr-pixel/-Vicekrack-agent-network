"""Run with python -m vicekrack TASK.json (or - for stdin)."""

import argparse
import json
import sys
from pathlib import Path

from .errors import NetworkError
from .orchestrator import Orchestrator, read_json


def saved_command():
    from .persistence import SavedRuns
    parser = argparse.ArgumentParser(description="Explicit local saved workflow runs")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("task")
    run.add_argument("--registry", default="config/agents.workflow.json")
    commands.add_parser("list")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("run_id")
    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--registry", default=None)
    resume.add_argument("--retry-uncertain", action="store_true")
    args = parser.parse_args()
    try:
        runs = SavedRuns()
        if args.command == "run":
            task = json.loads(sys.stdin.read()) if args.task == "-" else read_json(Path(args.task))
            result = runs.start(task, args.registry)
        elif args.command == "resume":
            result = runs.resume(args.run_id, registry_path=args.registry, retry_uncertain=args.retry_uncertain)
        elif args.command == "inspect":
            result = runs.store.inspect(args.run_id)
        else:
            result = {"runs": runs.store.list_runs()}
        code = 1 if args.command in {"run", "resume"} and result["status"] != "completed" else 0
    except NetworkError as error:
        result, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        result, code = {"error": {"code": "storage_error", "message": "Cannot read input or access local saved-run storage."}}, 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return code


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "render-preview":
        from .preview import main as preview_main
        return preview_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"validate-short-script", "plan-short"}:
        from .scene_cli import main as scene_main
        return scene_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"create-task", "validate-task"}:
        from .task_preparation import main as preparation_main
        return preparation_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"dashboard", "agents"}:
        from .dashboard import main as dashboard_main
        return dashboard_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"run", "list", "inspect", "resume"}:
        return saved_command()

    parser = argparse.ArgumentParser(description="Run one local Vicekrack task.")
    parser.add_argument("task", help="Task JSON file, or - to read JSON from stdin")
    parser.add_argument("--registry", default="config/agents.json",
                        help="Registry path relative to the project root (default: local mock)")
    args = parser.parse_args()
    try:
        if args.task == "-":
            task = json.loads(sys.stdin.read())
        else:
            task = read_json(Path(args.task))
        result = Orchestrator(registry_path=args.registry).run(task)
        exit_code = 0 if result["status"] == "completed" else 1
    except NetworkError as error:
        result, exit_code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        result, exit_code = {"error": {"code": "invalid_input", "message": "Cannot read a valid JSON task."}}, 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
