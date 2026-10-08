"""Step 37 commands: human review decisions for content previews (local, explicit, never publishes).

review-list PRODUCTION_ID       every decision with whether it applies now (newest first), the
                                current summary, and for each saved quality report what a new
                                decision needs (binding digest, acknowledgments, blockers).
review-inspect PRODUCTION_ID REVIEW_ID
                                one saved record and its applicability now.
review-record PRODUCTION_ID --report REPORT_ID --binding DIGEST --decision DECISION
              --reviewer LABEL [--ack CODE ...] [--notes TEXT | --notes-file PATH]
              [--supersedes REVIEW_ID]
                                append one decision. DECISION: approved_for_preview,
                                changes_requested or rejected. The label is self-declared.

Approval accepts the preview only; publishable stays false. Nothing is exported or uploaded.
"""
import argparse
import json

from .errors import NetworkError
from .review import ACKNOWLEDGMENTS, DECISIONS, MAX_NOTES, ReviewRecorder, history, inspect, reviewable


def main():
    parser = argparse.ArgumentParser(description="Human review decisions for local content previews; never publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("review-list")
    listing.add_argument("production_id", metavar="PRODUCTION_ID")
    one = commands.add_parser("review-inspect")
    one.add_argument("production_id", metavar="PRODUCTION_ID")
    one.add_argument("review_id", metavar="REVIEW_ID")
    record = commands.add_parser("review-record")
    record.add_argument("production_id", metavar="PRODUCTION_ID")
    record.add_argument("--report", required=True, metavar="REPORT_ID")
    record.add_argument("--binding", required=True, metavar="DIGEST",
                        help="the report's binding digest from review-list (at least 12 hex characters)")
    record.add_argument("--decision", required=True, choices=DECISIONS)
    record.add_argument("--reviewer", required=True, metavar="LABEL", help="self-declared label, not verified")
    record.add_argument("--ack", action="append", default=[], choices=ACKNOWLEDGMENTS)
    notes = record.add_mutually_exclusive_group()
    notes.add_argument("--notes", metavar="TEXT")
    notes.add_argument("--notes-file", metavar="PATH")
    record.add_argument("--supersedes", metavar="REVIEW_ID", help="the latest review ID (required once one exists)")
    args = parser.parse_args()
    try:
        if args.command == "review-list":
            result = history(args.production_id)
            result["reviewable_reports"] = reviewable(args.production_id)
        elif args.command == "review-inspect":
            result = inspect(args.production_id, args.review_id)
        else:
            text = args.notes
            if args.notes_file is not None:
                with open(args.notes_file, "rb") as stream:
                    data = stream.read(MAX_NOTES * 4 + 1)
                if len(data) > MAX_NOTES * 4:
                    raise NetworkError("invalid_notes", f"Notes are plain text of at most {MAX_NOTES} characters.")
                text = data.decode("utf-8")
            saved, path = ReviewRecorder().record(args.production_id, args.report, decision=args.decision,
                                                  reviewer=args.reviewer, binding=args.binding,
                                                  acknowledgments=args.ack, notes=text, supersedes=args.supersedes)
            result = {"review_file": str(path), "review_id": saved["review_id"], "sequence": saved["sequence"],
                      "decision": saved["decision"], "supersedes": saved["supersedes"],
                      "acknowledgments": saved["acknowledgments"], "scope": saved["scope"]}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": error.as_dict()}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
