"""Explicit production commands (Step 21). Local previews only; never publishes.

produce               run brief -> creator -> validate -> plan -> preview for one selected story
production-resume     continue from the first incomplete stage
production-list       list productions (metadata only)
production-inspect    show one production's saved state (no prompts or credentials are stored)
"""
import argparse
import json
from pathlib import Path

from .errors import NetworkError
from .production import DEFAULTS, Pipeline, inspect_production, list_productions


def main():
    parser = argparse.ArgumentParser(description="Controlled local production pipeline; never publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("produce")
    run.add_argument("selection_run_id", metavar="SELECTION_RUN_ID")
    run.add_argument("record_id", metavar="RECORD_ID", help="A select entry's record_id from that Selection Report")
    run.add_argument("--policy", default=DEFAULTS["policy"])
    run.add_argument("--profile", default=DEFAULTS["editorial_profile"])
    run.add_argument("--creator-config", default=DEFAULTS["creator"],
                     help="Default: offline mock Creator. OpenAI/Anthropic configs also need --allow-paid")
    run.add_argument("--capabilities", default=DEFAULTS["capabilities"])
    run.add_argument("--narration", type=Path, default=None, help="Optional local 16-bit PCM WAV (Step 14 rules)")
    run.add_argument("--allow-paid", action="store_true", help="Consent to one paid Creator request")
    run.add_argument("--allow-draft-preview", action="store_true", help="Allow a watermarked draft preview")
    resume = commands.add_parser("production-resume")
    resume.add_argument("production_id", metavar="PRODUCTION_ID")
    resume.add_argument("--allow-paid", action="store_true", help="Consent to a paid Creator request if one is next")
    resume.add_argument("--retry-uncertain", action="store_true",
                        help="Authorize retrying a paid request that may already have completed (possible charge)")
    commands.add_parser("production-list")
    show = commands.add_parser("production-inspect")
    show.add_argument("production_id", metavar="PRODUCTION_ID")
    args = parser.parse_args()
    try:
        if args.command == "produce":
            result = Pipeline().produce(
                args.selection_run_id, args.record_id,
                paths={"policy": args.policy, "editorial_profile": args.profile, "creator": args.creator_config,
                       "capabilities": args.capabilities},
                allow_paid=args.allow_paid, narration=args.narration, allow_draft_preview=args.allow_draft_preview)
        elif args.command == "production-resume":
            result = Pipeline().resume(args.production_id, allow_paid=args.allow_paid, retry_uncertain=args.retry_uncertain)
        elif args.command == "production-list":
            result = {"productions": list_productions()}
        else:
            result = inspect_production(args.production_id)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0 if args.command not in ("produce", "production-resume") or result["status"] == "completed" else 1
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
