"""Practice account commands (Trading PR 1). SIMULATED MONEY ONLY.

python -m vicekrack practice-start
python -m vicekrack practice-status
python -m vicekrack practice-ledger [--limit 50]
python -m vicekrack practice-price SYMBOL PRICE           record a price you observed now (runs the guard)
python -m vicekrack practice-buy SYMBOL DOLLARS --price P  simulated buy, guard checked first
python -m vicekrack practice-sell SYMBOL --price P         close a whole simulated position
python -m vicekrack practice-restart --confirm restart-practice

The watch loop (next PR) calls the same store operations; these commands exist so the account
can be checked by hand. Nothing here contacts a broker or the network.
"""

import argparse
import json
import sys

from ..contracts import utc_now
from ..errors import TradingError
from . import account as book
from .store import PracticeStore, load_config

COMMANDS = {"practice-start", "practice-status", "practice-ledger", "practice-price", "practice-buy", "practice-sell",
            "practice-restart"}


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack practice account (simulated money only)")
    root.add_argument("--config", default="config/practice-account.json")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("practice-start", help="Open the $100 practice account")
    commands.add_parser("practice-status", help="Cash, equity, positions and the shutdown guard")
    ledger = commands.add_parser("practice-ledger", help="Recent fills, refusals and stops")
    ledger.add_argument("--limit", type=int, default=50)
    price = commands.add_parser("practice-price", help="Record a price and run the shutdown guard")
    price.add_argument("symbol")
    price.add_argument("price")
    buy = commands.add_parser("practice-buy", help="Simulated buy (guard checked first)")
    buy.add_argument("symbol")
    buy.add_argument("dollars")
    buy.add_argument("--price", required=True)
    sell = commands.add_parser("practice-sell", help="Close a whole simulated position")
    sell.add_argument("symbol")
    sell.add_argument("--price", required=True)
    restart = commands.add_parser("practice-restart", help="Replace the account with a fresh $100 one (a person's decision)")
    restart.add_argument("--confirm", default="")
    return root


def main(argv=None, root=None, clock=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    now = (clock or utc_now)()
    store = PracticeStore(root)
    try:
        config = load_config(args.config, **({"root": root} if root is not None else {}))
        refusal = None
        if args.command == "practice-start":
            state = store.start(config, now)
        elif args.command == "practice-restart":
            state = store.restart(config, args.confirm, now)
        elif args.command == "practice-status":
            state = store.load(config)
        elif args.command == "practice-ledger":
            output = {"notice": book.NOTICE, "ledger": store.ledger(max(1, min(args.limit, 500)))}
            print(json.dumps(output, indent=2))
            return 0
        elif args.command == "practice-price":
            state, _, _ = store.apply(config, "mark", {args.symbol: {"price": args.price, "at": now}}, now=now)
        elif args.command == "practice-buy":
            state, _, refusal = store.apply(config, "buy", args.symbol, args.dollars, args.price, now=now)
        else:
            state, _, refusal = store.apply(config, "sell", args.symbol, args.price, now=now)
        output = book.summary(state, config, now)
        if refusal is not None:
            output["refused"] = refusal.as_dict()
        code = 1 if refusal is not None else 0
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "practice_storage_error", "message": "Cannot read or write the practice account."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
