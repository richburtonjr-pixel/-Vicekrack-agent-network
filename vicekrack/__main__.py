"""Run with python -m vicekrack TASK.json (or - for stdin)."""

import argparse
import json
import sys
from pathlib import Path

from .errors import NetworkError
from .orchestrator import Orchestrator, read_json


RECORD_HELP = "Record execution events under runtime/events/content/ (Step 33); stops before new work if they cannot be saved"


def saved_command():
    from .content_events import ContentEvents
    from .persistence import SavedRuns
    parser = argparse.ArgumentParser(description="Explicit local saved workflow runs")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("task")
    run.add_argument("--registry", default="config/agents.workflow.json")
    run.add_argument("--record-events", action="store_true", help=RECORD_HELP)
    commands.add_parser("list")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("run_id")
    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--registry", default=None)
    resume.add_argument("--retry-uncertain", action="store_true")
    resume.add_argument("--record-events", action="store_true", help=RECORD_HELP)
    args = parser.parse_args()
    try:
        events = ContentEvents.persistent("workflow") if getattr(args, "record_events", False) else None
        runs = SavedRuns(events=events)
        if args.command == "run":
            task = json.loads(sys.stdin.read()) if args.task == "-" else read_json(Path(args.task))
            result = runs.start(task, args.registry)
        elif args.command == "resume":
            result = runs.resume(args.run_id, registry_path=args.registry, retry_uncertain=args.retry_uncertain)
        elif args.command == "inspect":
            result = runs.store.inspect(args.run_id)
        else:
            result = {"runs": runs.store.list_runs()}
        code = 1 if args.command in {"run", "resume"} and (result["status"] != "completed" or "error" in result) else 0
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

    if len(sys.argv) > 1 and sys.argv[1] == "hq-serve":
        from .hq.server import main as hq_main
        return hq_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"events-list", "events-inspect", "events-replay"}:
        from .events.cli import main as events_main
        return events_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"trading-session-start", "trading-session-resume", "trading-session-list",
                                             "trading-session-inspect"}:
        from .trading.session.cli import main as session_main
        return session_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"analytics-generate", "analytics-inspect", "analytics-list"}:
        from .trading.analytics.cli import main as analytics_main
        return analytics_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"sim-run", "sim-inspect", "sim-list", "sim-kill-switch"}:
        from .trading.simulation.cli import main as simulation_main
        return simulation_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"agent-run", "agent-inspect", "agent-list"}:
        from .trading.agents.cli import main as agent_main
        return agent_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"signal-run", "signal-inspect", "signal-list"}:
        from .trading.signals.cli import main as signal_main
        return signal_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"indicator-calc", "indicator-inspect", "indicator-list"}:
        from .trading.indicators.cli import main as indicator_main
        return indicator_main()

    if len(sys.argv) > 1 and sys.argv[1].startswith("broker-paper-"):
        from .trading.broker.cli import main as broker_main
        return broker_main()

    if len(sys.argv) > 1 and sys.argv[1] == "market-fetch":
        from .trading.providers.cli import main as fetch_main
        return fetch_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"market-import", "market-inspect", "market-list", "market-replay"}:
        from .trading.market.cli import main as market_main
        return market_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"trading-demo", "trading-journal", "trading-config-check", "trading-kill-switch", "trading-state"}:
        from .trading.cli import main as trading_main
        return trading_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"export-preview", "export-verify"}:
        from .export_cli import main as export_main
        return export_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"review-record", "review-list", "review-inspect"}:
        from .review_cli import main as review_main
        return review_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"quality-report", "quality-list", "quality-binding"}:
        from .quality_cli import main as quality_main
        return quality_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"produce", "production-resume", "production-list", "production-inspect"}:
        from .production_cli import main as production_main
        return production_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"fetch-articles", "article-list"}:
        from .articles_cli import main as articles_main
        return articles_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"select-stories", "selection-history", "brief-from-selection"}:
        from .selection_cli import main as selection_main
        return selection_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"verify", "verify-list", "brief-from-verified"}:
        from .verification_cli import main as verification_main
        return verification_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"scout-sources", "scout", "scout-list"}:
        from .scout_cli import main as scout_main
        return scout_main()

    if len(sys.argv) > 1 and sys.argv[1] in {"validate-brief", "draft-short"}:
        from .creator_cli import main as creator_main
        return creator_main()

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
    parser.add_argument("--record-events", action="store_true",
                        help="Record workflow events (Step 33); output becomes {\"task\": ..., \"events\": ...}")
    args = parser.parse_args()
    from .content_events import ContentEvents, EventFailure
    events = ContentEvents.persistent("workflow") if args.record_events else None
    try:
        if args.task == "-":
            task = json.loads(sys.stdin.read())
        else:
            task = read_json(Path(args.task))
        runner = Orchestrator(registry_path=args.registry)
        runner.events = events
        result = runner.run(task)
        exit_code = 0 if result["status"] == "completed" else 1
        if events is not None:
            events.close("completed" if result["status"] == "completed" else "failed")
            result = {"task": result, "events": events.summary()}
            exit_code = 1 if events.failure else exit_code
    except EventFailure as failure:
        events.abort(failure.code)
        result, exit_code = {"error": {"code": failure.code, "message": "Event recording failed before any agent ran."},
                             "events": events.summary()}, 1
    except NetworkError as error:
        result, exit_code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        result, exit_code = {"error": {"code": "invalid_input", "message": "Cannot read a valid JSON task."}}, 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
