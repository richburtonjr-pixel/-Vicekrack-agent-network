"""Indicator CLI (Step 26): offline, bounded, descriptive only.

python -m vicekrack indicator-calc DATASET_ID --ema 20 --rsi 14 --volume-sma 20 --vwap [--save]
python -m vicekrack indicator-inspect RESULT_ID [--key ema_20] [--points 10]
python -m vicekrack indicator-list
"""

import argparse
import json
import sys

from ..contracts import utc_now
from ..errors import TradingError
from ..market.store import MarketStore, load_market_config
from .engine import build_settings, calculate
from .store import IndicatorStore, load_indicator_config

COMMANDS = {"indicator-calc", "indicator-inspect", "indicator-list"}


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack offline indicators (no signals)")
    commands = root.add_subparsers(dest="command", required=True)
    calc = commands.add_parser("indicator-calc", help="Calculate indicators over a stored dataset by bounded replay")
    calc.add_argument("dataset_id")
    calc.add_argument("--ema", type=int, action="append", default=[], help="EMA period (repeatable)")
    calc.add_argument("--rsi", type=int, action="append", default=[], help="Wilder RSI period (repeatable)")
    calc.add_argument("--volume-sma", type=int, action="append", default=[], help="Volume average period (repeatable)")
    calc.add_argument("--vwap", action="store_true", help="Session VWAP (intraday datasets only)")
    calc.add_argument("--vwap-timezone")
    calc.add_argument("--vwap-start", help="Session start HH:MM in the VWAP timezone")
    calc.add_argument("--vwap-end", help="Session end HH:MM in the VWAP timezone")
    calc.add_argument("--gap-policy", choices=("reset", "continue"))
    calc.add_argument("--start", help="Replay start, UTC")
    calc.add_argument("--end", help="Replay end, UTC")
    calc.add_argument("--step-seconds", type=int)
    calc.add_argument("--points", type=int, default=5, help="Latest points per indicator to print")
    calc.add_argument("--save", action="store_true", help="Save the full result under runtime/trading/indicators/")
    inspect = commands.add_parser("indicator-inspect", help="Show a saved indicator result")
    inspect.add_argument("result_id")
    inspect.add_argument("--key", help="Only this indicator, e.g. rsi_14")
    inspect.add_argument("--points", type=int, default=10)
    commands.add_parser("indicator-list", help="List saved indicator results")
    return root


def view(result, points, key=None):
    series = [s for s in result["series"] if key is None or s["key"] == key]
    if key is not None and not series:
        raise TradingError("unknown_indicator_key", "This result has no indicator with that key.")
    return {
        "notice": result["notice"], "result_id": result["result_id"], "dataset": result["dataset"],
        "settings": {k: result["settings"][k] for k in ("indicators", "gap_policy", "vwap_session", "rounding")},
        "replay": result["replay"], "summary": result["summary"], "results_sha256": result["results_sha256"],
        "series": [{"key": s["key"], "points_total": len(s["points"]),
                    "ready_points": sum(p["status"] == "ready" for p in s["points"]),
                    "latest": s["points"][-points:] if points else []} for s in series],
    }


def main(argv=None, root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    store = IndicatorStore(root)
    try:
        config, _ = load_indicator_config()
        limit = config["limits"]["max_cli_points"]
        if getattr(args, "points", 0) is not None and not 0 <= getattr(args, "points", 0) <= limit:
            raise TradingError("invalid_point_count", f"--points must be between 0 and {limit}.")
        if args.command == "indicator-calc":
            market_config, _ = load_market_config()
            dataset = MarketStore(root).load(args.dataset_id)
            session = None
            if any((args.vwap_timezone, args.vwap_start, args.vwap_end)):
                if not all((args.vwap_timezone, args.vwap_start, args.vwap_end)):
                    raise TradingError("invalid_indicator_settings", "Give --vwap-timezone, --vwap-start and --vwap-end together.")
                session = {"timezone": args.vwap_timezone, "start": args.vwap_start, "end": args.vwap_end}
            settings = build_settings(dataset, config=config, ema=args.ema, rsi=args.rsi, volume_sma=args.volume_sma,
                                      vwap=args.vwap, gap_policy=args.gap_policy, vwap_session=session)
            result = calculate(dataset, settings, market_config=market_config, indicator_config=config,
                               created_at=utc_now(), start=args.start, end=args.end, step_seconds=args.step_seconds)
            if args.save:
                store.save(result)
            output = {**view(result, args.points), "saved": bool(args.save)}
            if args.save:
                output["path"] = f"runtime/trading/indicators/{result['result_id']}.json"
        elif args.command == "indicator-inspect":
            output = view(store.load(args.result_id), args.points, args.key)
        else:
            output = {"results": store.list()}
        code = 0
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "indicator_storage_error", "message": "Cannot read or write local indicator data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
