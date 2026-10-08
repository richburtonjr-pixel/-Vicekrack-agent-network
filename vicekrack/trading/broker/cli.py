"""Alpaca PAPER broker CLI (Step 41). Every network command needs --allow-network; submission and
cancellation also need an exact consent phrase. Nothing runs on its own: no polling, no retries.

python -m vicekrack broker-paper-check --allow-network
python -m vicekrack broker-paper-prepare --symbol AAPL --side buy --qty 1 --limit-price 190.10 --allow-network
python -m vicekrack broker-paper-submit INTENT_ID --consent paper-execute:INTENT_ID --allow-network
python -m vicekrack broker-paper-status INTENT_ID --allow-network
python -m vicekrack broker-paper-cancel INTENT_ID --consent paper-cancel:INTENT_ID --allow-network
python -m vicekrack broker-paper-list
python -m vicekrack broker-paper-inspect INTENT_ID
python -m vicekrack broker-paper-kill-switch status|engage|release
"""

import argparse
import json
import sys
from pathlib import Path

from ..contracts import ROOT, utc_now, validate_schema
from ..errors import TradingError
from .orders import NOTICE, PaperBroker

COMMANDS = {"broker-paper-check", "broker-paper-prepare", "broker-paper-submit", "broker-paper-status",
            "broker-paper-cancel", "broker-paper-list", "broker-paper-inspect", "broker-paper-kill-switch"}
NETWORK_HELP = "Required: allow this one bounded request sequence to the Alpaca PAPER endpoint"


def load_broker_config(path="config/broker.paper.json", root=ROOT):
    try:
        config = json.loads((Path(root) / path).read_text(encoding="utf-8-sig"))
        validate_schema("broker_paper_config", config)
    except (TradingError, OSError, ValueError, UnicodeError):
        raise TradingError("invalid_broker_config", "config/broker.paper.json is invalid.") from None
    return config


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack Alpaca PAPER broker (no real money)")
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser("broker-paper-check", help="Connect to and check the Alpaca paper account")
    check.add_argument("--allow-network", action="store_true", help=NETWORK_HELP)
    prepare = commands.add_parser("broker-paper-prepare", help="Propose one paper limit order and save it as an immutable intent")
    prepare.add_argument("--symbol", required=True)
    prepare.add_argument("--side", choices=("buy", "sell"), required=True)
    prepare.add_argument("--qty", required=True, help="Whole shares")
    prepare.add_argument("--limit-price", required=True)
    prepare.add_argument("--time-in-force", default="day", choices=("day",))
    prepare.add_argument("--allow-network", action="store_true", help=NETWORK_HELP)
    submit = commands.add_parser("broker-paper-submit", help="Submit exactly one prepared intent (needs consent)")
    submit.add_argument("intent_id")
    submit.add_argument("--consent", required=True, help="Type exactly: paper-execute:INTENT_ID")
    submit.add_argument("--allow-network", action="store_true", help=NETWORK_HELP)
    status = commands.add_parser("broker-paper-status", help="Refresh and reconcile one order by its client order ID")
    status.add_argument("intent_id")
    status.add_argument("--allow-network", action="store_true", help=NETWORK_HELP)
    cancel = commands.add_parser("broker-paper-cancel", help="Request cancellation (not a confirmed cancellation)")
    cancel.add_argument("intent_id")
    cancel.add_argument("--consent", required=True, help="Type exactly: paper-cancel:INTENT_ID")
    cancel.add_argument("--allow-network", action="store_true", help=NETWORK_HELP)
    commands.add_parser("broker-paper-list", help="List saved paper intents and their last known state (offline)")
    inspect = commands.add_parser("broker-paper-inspect", help="Show one intent and its records (offline)")
    inspect.add_argument("intent_id")
    switch = commands.add_parser("broker-paper-kill-switch", help="Block or allow new paper submissions")
    switch.add_argument("action", choices=("status", "engage", "release"))
    return root


def main(argv=None, root=None, transport=None, environ=None, clock=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = load_broker_config()
        broker = PaperBroker(root, config=config, allow_network=getattr(args, "allow_network", False), transport=transport,
                             environ=environ, **({"clock": clock} if clock is not None else {}))
        if args.command == "broker-paper-check":
            output = {"notice": NOTICE, **broker.check()}
        elif args.command == "broker-paper-prepare":
            intent = broker.prepare(symbol=args.symbol, side=args.side, qty=args.qty, limit_price=args.limit_price,
                                    time_in_force=args.time_in_force)
            output = {"notice": NOTICE, "prepared": True, "intent_id": intent["intent_id"], "proposal": intent["order"],
                      "max_notional": intent["exposure"]["max_notional"], "exposure": intent["exposure"]["explanation"],
                      "expires_at": intent["expires_at"], "client_order_id": intent["client_order_id"],
                      "inputs_at_preparation": intent["inputs_at_preparation"], "preview_checks": intent["preview_checks"],
                      "to_submit": f"python -m vicekrack broker-paper-submit {intent['intent_id']} --consent "
                                   f"{intent['consent_phrase']} --allow-network"}
        elif args.command == "broker-paper-submit":
            output = {"notice": NOTICE, **broker.submit(args.intent_id, args.consent)}
        elif args.command == "broker-paper-status":
            output = {"notice": NOTICE, **broker.refresh(args.intent_id)}
        elif args.command == "broker-paper-cancel":
            output = {"notice": NOTICE, **broker.cancel(args.intent_id, args.consent)}
        elif args.command == "broker-paper-list":
            output = {"notice": NOTICE, "intents": broker.list()}
        elif args.command == "broker-paper-inspect":
            output = {"notice": NOTICE, **broker.show(args.intent_id)}
        else:
            if args.action != "status":
                broker.store.set_kill_switch(args.action == "engage", utc_now())
            engaged, source = broker.store.kill_switch(config)
            output = {"broker_paper_kill_switch_engaged": engaged, "source": source,
                      "note": "Blocks new paper submissions only; status checks and cancel requests stay allowed."}
        code = 1 if output.get("blocked") or output.get("outcome") in ("rejected", "unknown") else 0
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "broker_storage_error", "message": "Cannot read or write local broker-paper data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
