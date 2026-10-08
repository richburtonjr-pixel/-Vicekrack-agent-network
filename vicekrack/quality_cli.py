"""Quality report commands (Step 22). Read-only checks; never repair, retry or publish.

quality-report PRODUCTION_ID   check one production run and save a new bound report (version 1.1)
quality-list                   list saved reports
quality-binding REPORT_ID      Step 36, read-only: is a saved report still bound to the files as they are
                               now? matching | changed | legacy_unverified | unavailable. Nothing is
                               checked again and nothing is written.
"""
import argparse
import json

from .errors import NetworkError
from .quality import QualityChecker, binding_of, list_reports


def main():
    parser = argparse.ArgumentParser(description="Technical quality report for a production run; never publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("quality-report")
    run.add_argument("production_id", metavar="PRODUCTION_ID")
    run.add_argument("--record-events", action="store_true",
                     help="Record execution events under runtime/events/content/ (Step 33)")
    commands.add_parser("quality-list")
    binding = commands.add_parser("quality-binding")
    binding.add_argument("report_id", metavar="REPORT_ID")
    args = parser.parse_args()
    from .content_events import ContentEvents, EventFailure
    events = ContentEvents.persistent("quality") if getattr(args, "record_events", False) else None
    try:
        if args.command == "quality-report":
            report, path = QualityChecker(events=events).run(args.production_id)
            result = {"report_file": str(path), "report_id": report["report_id"], "result": report["result"],
                      "reasons": report["reasons"],
                      "checks": {c["check_id"]: c["status"] for c in report["checks"]},
                      "scope": report["scope"],
                      "binding": {"status": report["binding"]["status"], "reasons": report["binding"]["reasons"],
                                  "artifacts": len(report["binding"]["artifacts"]), "note": report["binding"]["note"]}}
            if events is not None:
                events.close("completed")
                result["events"] = events.summary()
            print(json.dumps(result, indent=2))
            return 0 if report["result"] == "pass" and not (events is not None and events.failure) else 1
        if args.command == "quality-binding":
            result = binding_of(args.report_id)
            print(json.dumps(result, indent=2))
            return 0 if result["binding"]["status"] == "matching" else 1
        print(json.dumps({"reports": list_reports()}, indent=2))
        return 0
    except EventFailure as failure:
        events.abort(failure.code)
        print(json.dumps({"error": {"code": failure.code}, "events": events.summary()}))
    except NetworkError as error:
        if events is not None:
            events.abort(error.code)
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
