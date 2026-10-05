"""Market-data CLI (Step 25): offline import, inspect, list and replay. No live feeds.

python -m vicekrack market-import --adapter synthetic --fixture synth1-5m
python -m vicekrack market-import --adapter csv --file PATH --symbol SYM --interval 5m --timezone America/New_York [--label historical]
python -m vicekrack market-inspect DATASET_ID|REPLAY_ID [--bars N]
python -m vicekrack market-list [--replays]
python -m vicekrack market-replay DATASET_ID [--start UTC] [--end UTC] [--step-seconds N]
"""

import argparse
import json
import sys

from ..contracts import utc_now
from ..errors import TradingError
from .bars import expand_bar
from .replay import CONSUMERS, run_replay
from .store import MarketStore, load_market_config, make_adapter

COMMANDS = {"market-import", "market-inspect", "market-list", "market-replay"}
DATA_NOTICE = ("Imported market data is NOT verified as authentic, current, complete or licensed. "
               "It cannot be used for paper-account authorization.")


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack offline market data (no live feeds)")
    commands = root.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("market-import", help="Validate and store a dataset from a fixture or local CSV")
    imp.add_argument("--adapter", choices=("synthetic", "csv"), required=True)
    imp.add_argument("--fixture")
    imp.add_argument("--file")
    imp.add_argument("--symbol")
    imp.add_argument("--interval", choices=("1m", "5m", "15m", "30m", "1h", "1d"))
    imp.add_argument("--timezone", help="IANA timezone used for interval alignment and daily bars")
    imp.add_argument("--naive-timezone", help="Interpret timestamps without an offset in this IANA timezone")
    imp.add_argument("--currency")
    imp.add_argument("--label", choices=("synthetic", "historical", "delayed", "unknown"))
    imp.add_argument("--source-name")
    imp.add_argument("--config", default="config/market-data.json")
    inspect = commands.add_parser("market-inspect", help="Show a stored dataset or replay report")
    inspect.add_argument("item_id")
    inspect.add_argument("--bars", type=int, default=5, help="Number of bars to show (0-50)")
    listing = commands.add_parser("market-list", help="List stored datasets (or replay reports)")
    listing.add_argument("--replays", action="store_true")
    replay = commands.add_parser("market-replay", help="Bounded offline replay on a simulation clock")
    replay.add_argument("dataset_id")
    replay.add_argument("--start", help="Simulation start, UTC (default: first bar start)")
    replay.add_argument("--end", help="Simulation end, UTC (default: last bar close)")
    replay.add_argument("--step-seconds", type=int, help="Simulation step (default: the interval)")
    replay.add_argument("--consumer", action="append", choices=sorted(CONSUMERS))
    replay.add_argument("--show-steps", action="store_true")
    replay.add_argument("--config", default="config/market-data.json")
    return root


def summary(dataset, bars=5):
    return {
        "notice": DATA_NOTICE, "dataset_id": dataset["dataset_id"], "symbol": dataset["symbol"],
        "interval": dataset["interval"], "timezone": dataset["timezone"], "currency": dataset["currency"],
        "data_label": dataset["data_label"], "source": dataset["source"], "import_settings": dataset["import_settings"],
        "imported_at": dataset["imported_at"], "bar_count": dataset["bar_count"],
        "first_start_utc": dataset["first_start_utc"], "last_start_utc": dataset["last_start_utc"],
        "last_available_utc": dataset["last_available_utc"], "gaps": dataset["gaps"],
        "verification": dataset["verification"],
        "bars": [expand_bar(dataset, i) for i in range(min(bars, dataset["bar_count"]))],
    }


def main(argv=None, root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    store = MarketStore(root)
    try:
        if args.command == "market-import":
            config, config_sha = load_market_config(args.config)
            adapter = make_adapter(args.adapter, config=config, fixture=args.fixture, file=args.file,
                                   symbol=args.symbol, source_name=args.source_name)
            dataset = store.import_dataset(adapter, config=config, config_sha256=config_sha, symbol=args.symbol,
                                           interval=args.interval, tz=args.timezone, naive_timezone=args.naive_timezone,
                                           currency=args.currency, label=args.label)
            result = {"imported": True, **summary(dataset, 3)}
        elif args.command == "market-inspect":
            if not 0 <= args.bars <= 50:
                raise TradingError("invalid_bar_count", "--bars must be between 0 and 50.")
            if str(args.item_id).startswith("rpl-"):
                result = store.load_replay(args.item_id)
            else:
                result = summary(store.load(args.item_id), args.bars)
        elif args.command == "market-list":
            result = {"notice": DATA_NOTICE,
                      **({"replays": store.list_replays()} if args.replays else {"datasets": store.list_datasets()})}
        else:
            config, _ = load_market_config(args.config)
            dataset = store.load(args.dataset_id)
            report = store.save_replay(run_replay(dataset, config=config, created_at=utc_now(), start=args.start,
                                                  end=args.end, step_seconds=args.step_seconds, consumers=args.consumer))
            result = {key: report[key] for key in ("notice", "replay_id", "mode", "account_access",
                                                   "authorization_possible", "dataset_id", "symbol", "interval",
                                                   "data_label", "simulation", "consumers", "summary", "results_sha256")}
            result["report"] = f"runtime/trading/market/replays/{report['replay_id']}.json"
            if args.show_steps:
                result["steps"] = report["steps"]
        code = 0
    except TradingError as error:
        result, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        result, code = {"error": {"code": "market_storage_error", "message": "Cannot read or write local market data."}}, 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return code
