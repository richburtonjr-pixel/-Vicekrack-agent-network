"""Broker plug commands (paper only).

python -m vicekrack brokers-list
python -m vicekrack ibkr-paper-check --allow-network     read-only: account, positions, open orders
"""

import argparse
import json
import sys

from ..errors import TradingError
from . import REGISTRY, open_broker

COMMANDS = {"brokers-list", "ibkr-paper-check"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="ViceKrack broker plug (paper only)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("brokers-list")
    check = commands.add_parser("ibkr-paper-check")
    check.add_argument("--allow-network", action="store_true",
                       help="Required: connect to IB Gateway/TWS on this PC (read-only)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.command == "brokers-list":
            result = {"brokers": [{"name": k, **v} for k, v in REGISTRY.items()], "mode": "paper"}
        else:
            if not args.allow_network:
                raise TradingError("network_not_allowed", "Add --allow-network to connect to IB Gateway/TWS (read-only).")
            with open_broker("ibkr", readonly=True) as broker:
                result = {"connected": True, "read_only": True, "account": broker.account(),
                          "positions": broker.positions(), "open_orders": broker.open_orders()}
        code = 0
    except TradingError as error:
        result, code = {"error": error.as_dict()}, 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return code
