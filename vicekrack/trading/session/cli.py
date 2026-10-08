"""Trading research session CLI (Step 39): offline, simulated, explicit.

python -m vicekrack trading-session-start DATASET_ID [--config config/trading-session.json] [--record-events]
python -m vicekrack trading-session-resume SESSION_ID [--record-events]
python -m vicekrack trading-session-list
python -m vicekrack trading-session-inspect SESSION_ID [--stage STAGE]

Start and resume are the only commands that run anything. Neither retries on its own; a
failed stage waits for an explicit resume. List and inspect only read and re-validate.
"""

import argparse
import json
import sys

from ..errors import TradingError
from .runner import NOTICE, SessionRunner
from .store import STAGES
from .view import MAX_SESSIONS, describe, list_sessions

COMMANDS = {"trading-session-start", "trading-session-resume", "trading-session-list", "trading-session-inspect"}
RECORD_HELP = ("Record the research and simulation stages as Step 31 timelines under runtime/events/trading/; "
               "a recording failure fails that stage explicitly")


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack",
                                   description="ViceKrack trading research session (offline, simulated only)")
    commands = root.add_subparsers(dest="command", required=True)
    start = commands.add_parser("trading-session-start",
                                help="Validate dataset -> research agents -> simulation -> analytics -> HQ summary")
    start.add_argument("dataset_id")
    start.add_argument("--config", default="config/trading-session.json")
    start.add_argument("--record-events", action="store_true", help=RECORD_HELP)
    resume = commands.add_parser("trading-session-resume", help="Continue from the first incomplete stage (explicit)")
    resume.add_argument("session_id")
    resume.add_argument("--record-events", action="store_true", help=RECORD_HELP)
    commands.add_parser("trading-session-list", help="List saved sessions")
    inspect = commands.add_parser("trading-session-inspect", help="Show and re-validate one session")
    inspect.add_argument("session_id")
    inspect.add_argument("--stage", choices=STAGES, help="Include that stage's full artifact")
    return root


def inspect_view(session_id, stage, root):
    view = describe(session_id, root)
    documents = view.pop("documents")
    manifest = view.pop("manifest")
    view["notice"] = NOTICE
    view["summary"] = None if manifest is None else {
        "time_domains": manifest["time_domains"], "results": manifest["results"], "links": manifest["links"],
        "continuity": manifest["continuity"]}
    if stage:
        view["artifact"] = documents.get(stage)
        if view["artifact"] is None:
            view["artifact_note"] = "This stage has no verified artifact."
    return view


def main(argv=None, root=None, config_root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    extra = {} if config_root is None else {"config_root": config_root}
    try:
        if args.command == "trading-session-start":
            output = SessionRunner(root, record_events=args.record_events, **extra).start(args.dataset_id, args.config)
        elif args.command == "trading-session-resume":
            output = SessionRunner(root, record_events=args.record_events, **extra).resume(args.session_id)
        elif args.command == "trading-session-list":
            items, total = list_sessions(root)
            output = {"notice": NOTICE, "sessions": items, "total": total, "shown_limit": MAX_SESSIONS}
        else:
            output = inspect_view(args.session_id, args.stage, root)
        code = 0 if output.get("failure") is None else 1
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "session_storage_error", "message": "Cannot read or write local session data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
