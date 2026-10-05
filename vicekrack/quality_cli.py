"""Quality report commands (Step 22). Read-only checks; never repair, retry or publish.

quality-report PRODUCTION_ID   check one production run and save a report
quality-list                   list saved reports
"""
import argparse
import json

from .errors import NetworkError
from .quality import QualityChecker, list_reports


def main():
    parser = argparse.ArgumentParser(description="Technical quality report for a production run; never publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("quality-report")
    run.add_argument("production_id", metavar="PRODUCTION_ID")
    commands.add_parser("quality-list")
    args = parser.parse_args()
    try:
        if args.command == "quality-report":
            report, path = QualityChecker().run(args.production_id)
            result = {"report_file": str(path), "report_id": report["report_id"], "result": report["result"],
                      "reasons": report["reasons"],
                      "checks": {c["check_id"]: c["status"] for c in report["checks"]},
                      "scope": report["scope"]}
            print(json.dumps(result, indent=2))
            return 0 if report["result"] == "pass" else 1
        print(json.dumps({"reports": list_reports()}, indent=2))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
