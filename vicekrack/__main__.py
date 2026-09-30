"""Run with python -m vicekrack TASK.json (or - for stdin)."""

import argparse
import json
import sys
from pathlib import Path

from .errors import NetworkError
from .orchestrator import Orchestrator, read_json


def main():
    parser = argparse.ArgumentParser(description="Run one local Vicekrack task.")
    parser.add_argument("task", help="Task JSON file, or - to read JSON from stdin")
    args = parser.parse_args()
    try:
        if args.task == "-":
            task = json.loads(sys.stdin.read())
        else:
            task = read_json(Path(args.task))
        result = Orchestrator().run(task)
        exit_code = 0 if result["status"] == "completed" else 1
    except NetworkError as error:
        result, exit_code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        result, exit_code = {"error": {"code": "invalid_input", "message": "Cannot read a valid JSON task."}}, 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
