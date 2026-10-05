"""Trading CLI (paper only, offline).

python -m vicekrack trading-demo --scenario allowed
python -m vicekrack trading-journal [RUN_ID]
python -m vicekrack trading-config-check [--config PATH]
python -m vicekrack trading-kill-switch status|engage|release
python -m vicekrack trading-state init|show|list|cancel|recover [--account NAME]
"""

import argparse
import json
import sys

from . import PAPER_ONLY_NOTICE
from .config import kill_switch_state, load_config, set_kill_switch
from .contracts import utc_now
from .demo import DEFAULT_ACCOUNT, SCENARIOS, run_demo
from .errors import TradingError
from .journal import TradingJournal, summarize
from .state import DEFAULT_TIMEZONE, PaperAccount

COMMANDS = {"trading-demo", "trading-journal", "trading-config-check", "trading-kill-switch", "trading-state"}


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack paper-trading foundation (simulated only)")
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("trading-demo", help="Run one bounded offline synthetic scenario")
    demo.add_argument("--scenario", choices=SCENARIOS, default="allowed")
    demo.add_argument("--config", default="config/trading.paper.json")
    demo.add_argument("--account", default=DEFAULT_ACCOUNT, help="Paper account (create it first with trading-state init)")
    journal = commands.add_parser("trading-journal", help="List runs, or show one run's events")
    journal.add_argument("run_id", nargs="?")
    journal.add_argument("--full", action="store_true", help="Include full event documents")
    check = commands.add_parser("trading-config-check", help="Validate the paper configuration")
    check.add_argument("--config", default="config/trading.paper.json")
    switch = commands.add_parser("trading-kill-switch", help="Show, engage or release the local kill switch")
    switch.add_argument("action", choices=("status", "engage", "release"))
    switch.add_argument("--config", default="config/trading.paper.json")
    state = commands.add_parser("trading-state", help="Persistent paper account state (no execution)")
    actions = state.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="Explicitly create a new paper account")
    init.add_argument("--account", default=DEFAULT_ACCOUNT)
    init.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="IANA trading-day timezone (rolls over at local midnight)")
    show = actions.add_parser("show", help="Show counters, reservations and recent intents")
    show.add_argument("--account", default=DEFAULT_ACCOUNT)
    show.add_argument("--full", action="store_true", help="Print the complete state document")
    actions.add_parser("list", help="List paper accounts")
    cancel = actions.add_parser("cancel", help="Cancel an unsubmitted paper intent and release its reservation")
    cancel.add_argument("intent_id")
    cancel.add_argument("--reason", required=True, help="Short code, e.g. operator_request")
    cancel.add_argument("--note", default=None)
    cancel.add_argument("--account", default=DEFAULT_ACCOUNT)
    recover = actions.add_parser("recover", help="Resolve an interrupted operation and reconcile the journal")
    recover.add_argument("--account", default=DEFAULT_ACCOUNT)
    return root


def state_command(args, root):
    journal = TradingJournal(root)
    if args.action == "list":
        folder = PaperAccount(DEFAULT_ACCOUNT, root=root).folder.parent
        names = sorted(p.name for p in folder.iterdir() if p.is_dir() and p.name.startswith("acct-")) if folder.is_dir() else []
        return {"notice": PAPER_ONLY_NOTICE, "accounts": names}
    account = PaperAccount(args.account, root=root)
    if args.action == "init":
        state = account.initialize(args.timezone, journal=journal)
        return {"notice": PAPER_ONLY_NOTICE, "account_id": state["account_id"], "initialized": True,
                "revision": state["revision"], "trading_day": state["trading_day"]}
    if args.action == "show":
        view = account.inspect()
        state = view["state"]
        if args.full:
            return {"notice": PAPER_ONLY_NOTICE, "recovery_required": view["recovery_required"], "state": state}
        return {
            "notice": PAPER_ONLY_NOTICE, "account_id": state["account_id"], "revision": state["revision"],
            "recovery_required": view["recovery_required"], "trading_day": state["trading_day"],
            "reserved_by_symbol": view["reserved_by_symbol"], "ledger": state["ledger"],
            "processed_signals": len(state["processed_signals"]),
            "intents": [{k: i[k] for k in ("intent_id", "signal_id", "symbol", "side", "quantity", "notional",
                                             "trading_date", "status")} | {"reservation": i["reservation"]["status"],
                                                                           "release_reason": i["reservation"]["release_reason"]}
                        for i in state["intents"][-20:]],
            "recoveries": state["recoveries"][-5:],
        }
    with account.lock():
        if args.action == "cancel":
            return {"notice": PAPER_ONLY_NOTICE, **account.cancel(args.intent_id, args.reason, args.note, journal=journal)}
        return {"notice": PAPER_ONLY_NOTICE, **account.recover(journal)}


def main(argv=None, root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "trading-demo":
            result = run_demo(args.scenario, account=args.account, root=root, config_path=args.config)
        elif args.command == "trading-state":
            result = state_command(args, root)
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
