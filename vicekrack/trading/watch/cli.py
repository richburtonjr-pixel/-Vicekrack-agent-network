"""Watch loop commands (Trading PR 2). Research only; no trades.

python -m vicekrack watch-once       one cycle now (fetch prices, research, run the $80 guard)
python -m vicekrack watch-run        keep going every 5 minutes until the window is closed
python -m vicekrack watch-status     the last saved cycle
python -m vicekrack desk-cases       recent trade desk case files (who approved, who vetoed, why)
"""

import argparse
import json
import sys

from ..errors import TradingError
from ..desk.desk import CaseFiles
from .cycle import WatchFolder, load_watch_config, run_cycle
from .runner import run_loop

COMMANDS = {"watch-once", "watch-run", "watch-status", "desk-cases"}


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack 24/7 crypto watch loop (no trades)")
    root.add_argument("--config", default="config/watch.json")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("watch-once", help="Run one watch cycle now")
    commands.add_parser("watch-run", help="Run the watch loop until stopped")
    commands.add_parser("watch-status", help="Show the last saved cycle")
    cases = commands.add_parser("desk-cases", help="Recent trade desk case files")
    cases.add_argument("--limit", type=int, default=20)
    return root


def main(argv=None, root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "desk-cases":
            output = {"cases": CaseFiles(root).recent(max(1, min(args.limit, 200)))}
        elif args.command == "watch-status":
            output = WatchFolder(root).status() or {"status": "no_cycles_yet"}
        else:
            config = load_watch_config(args.config, **({"root": root} if root is not None else {}))
            if args.command == "watch-once":
                output = run_cycle(config, root=root)
            else:
                try:
                    run_loop(config, root=root)
                except KeyboardInterrupt:
                    print("Watch loop stopped.")
                return 0
        code = 0
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "watch_storage_error", "message": "Cannot read or write watch data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
