"""Research-signal CLI (Step 27): offline, bounded, research only.

python -m vicekrack signal-run DATASET_ID --strategy ema-cross-3-5 [--strategy breakout-3] [--save]
python -m vicekrack signal-inspect RUN_ID|SIGNAL_ID [--strategy NAME] [--outcome not_ready] [--entries 20]
python -m vicekrack signal-list [--signals | --strategies]
"""

import argparse
import json
import sys

from ..contracts import utc_now
from ..errors import TradingError
from ..indicators.store import load_indicator_config
from ..market.store import MarketStore, load_market_config
from .engine import RUN_NOTICE, load_strategies, run_signals
from .store import SignalStore, load_signal_config

COMMANDS = {"signal-run", "signal-inspect", "signal-list"}
OUTCOMES = ("triggered", "not_triggered", "not_ready")


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack research signals (no orders)")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("signal-run", help="Evaluate named research strategies over a stored dataset")
    run.add_argument("dataset_id")
    run.add_argument("--strategy", action="append", required=True, help="Name from config/research-signals.json (repeatable)")
    run.add_argument("--start", help="Replay start, UTC")
    run.add_argument("--end", help="Replay end, UTC")
    run.add_argument("--step-seconds", type=int)
    run.add_argument("--entries", type=int, default=5, help="Latest evaluations per strategy to print")
    run.add_argument("--save", action="store_true", help="Save the run and new signal records")
    inspect = commands.add_parser("signal-inspect", help="Show a saved run (rsr-...) or research signal (rsig-...)")
    inspect.add_argument("item_id")
    inspect.add_argument("--strategy")
    inspect.add_argument("--outcome", choices=OUTCOMES)
    inspect.add_argument("--entries", type=int, default=20)
    listing = commands.add_parser("signal-list", help="List saved runs, signals or configured strategies")
    group = listing.add_mutually_exclusive_group()
    group.add_argument("--signals", action="store_true")
    group.add_argument("--strategies", action="store_true")
    return root


def view(run, entries, strategy=None, outcome=None):
    evaluations = [e for e in run["evaluations"] if strategy is None or e["strategy"] == strategy]
    if strategy is not None and not evaluations:
        raise TradingError("unknown_strategy", "This run has no strategy with that name.")
    shown = []
    for evaluation in evaluations:
        selected = [e for e in evaluation["entries"] if outcome is None or e["outcome"] == outcome]
        shown.append({"strategy": evaluation["strategy"], "matching_entries": len(selected),
                      "latest": selected[-entries:] if entries else []})
    return {"notice": run["notice"], "run_id": run["run_id"], "purpose": run["purpose"],
            "authorization_possible": run["authorization_possible"], "dataset": run["dataset"],
            "strategies": run["strategies"], "replay": run["replay"], "summary": run["summary"],
            "signals": [{k: s[k] for k in ("signal_id", "event", "bar", "detected_at_sim_utc", "expires_at_utc", "expired_when_detected",
                                         "supporting_values", "reason_codes")}
                        | {"strategy": s["strategy"]["name"]} for s in run["signals"][-max(entries, 1):]],
            "evaluations": shown, "results_sha256": run["results_sha256"]}


def main(argv=None, root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    store = SignalStore(root)
    try:
        signal_config, _ = load_signal_config()
        limit = signal_config["limits"]["max_cli_entries"]
        if not 0 <= getattr(args, "entries", 0) <= limit:
            raise TradingError("invalid_entry_count", f"--entries must be between 0 and {limit}.")
        if args.command == "signal-run":
            market_config, _ = load_market_config()
            indicator_config, _ = load_indicator_config()
            dataset = MarketStore(root).load(args.dataset_id)      # tampered datasets are rejected here
            strategies = load_strategies(args.strategy, signal_config)
            run = run_signals(dataset, strategies, market_config=market_config, indicator_config=indicator_config,
                              signal_config=signal_config, created_at=utc_now(), start=args.start, end=args.end,
                              step_seconds=args.step_seconds)
            output = view(run, args.entries)
            output["saved"] = store.save_run(run) if args.save else False
        elif args.command == "signal-inspect":
            if str(args.item_id).startswith("rsig-"):
                output = store.load_signal(args.item_id)
            else:
                output = view(store.load_run(args.item_id), args.entries, args.strategy, args.outcome)
        elif args.strategies:
            output = {"notice": RUN_NOTICE, "strategies": signal_config["strategies"]}
        elif args.signals:
            output = {"notice": RUN_NOTICE, "signals": store.list_signals()}
        else:
            output = {"notice": RUN_NOTICE, "runs": store.list_runs()}
        code = 0
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "signal_storage_error", "message": "Cannot read or write local signal data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
