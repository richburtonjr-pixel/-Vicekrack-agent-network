"""Trading CLI (paper only, offline).

python -m vicekrack trading-demo --scenario allowed
python -m vicekrack trading-journal [RUN_ID]
python -m vicekrack trading-config-check [--config PATH]
python -m vicekrack trading-kill-switch status|engage|release
"""

import argparse
import json
import sys

from . import PAPER_ONLY_NOTICE
from .config import kill_switch_state, load_config, set_kill_switch
from .demo import SCENARIOS, run_demo, utc_now
from .errors import TradingError
from .journal import TradingJournal, summarize

COMMANDS = {"trading-demo", "trading-journal", "trading-config-check", "trading-kill-switch"}


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack paper-trading foundation (simulated only)")
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("trading-demo", help="Run one bounded offline synthetic scenario")
    demo.add_argument("--scenario", choices=SCENARIOS, default="allowed")
    demo.add_argument("--config", default="config/trading.paper.json")
    journal = commands.add_parser("trading-journal", help="List runs, or show one run's events")
    journal.add_argument("run_id", nargs="?")
    journal.add_argument("--full", action="store_true", help="Include full event documents")
    check = commands.add_parser("trading-config-check", help="Validate the paper configuration")
    check.add_argument("--config", default="config/trading.paper.json")
    switch = commands.add_parser("trading-kill-switch", help="Show, engage or release the local kill switch")
    switch.add_argument("action", choices=("status", "engage", "release"))
    switch.add_argument("--config", default="config/trading.paper.json")
    return root


def main(argv=None, root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "trading-demo":
            result = run_demo(args.scenario, root=root, config_path=args.config)
        elif args.command == "trading-journal":
            journal = TradingJournal(root)
            if args.run_id:
                events = journal.read_run(args.run_id)
                result = {"notice": PAPER_ONLY_NOTICE, "run_id": args.run_id,
                          "events": events if args.full else summarize(events)}
            else:
                result = {"notice": PAPER_ONLY_NOTICE, "runs": journal.list_runs()}
        elif args.command == "trading-config-check":
            config, digest = load_config(args.config)
            engaged, source = kill_switch_state(config, root)
            result = {"valid": True, "mode": config["mode"], "config_sha256": digest, "limits": config["limits"],
                      "allowed_symbols": config["allowed_symbols"], "kill_switch_engaged": engaged,
                      "kill_switch_source": source}
        else:
            if args.action != "status":
                set_kill_switch(args.action == "engage", utc_now(), root)
            try:
                config = load_config(args.config)[0]
            except TradingError as error:
                if error.code == "sensitive_state":
                    raise
                config = None
            engaged, source = kill_switch_state(config, root)
            result = {"kill_switch_engaged": engaged, "source": source}
        code = 0
    except TradingError as error:
        result, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        result, code = {"error": {"code": "trading_storage_error", "message": "Cannot read or write local trading storage."}}, 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return code
