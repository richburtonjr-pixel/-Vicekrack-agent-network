"""Research-agent CLI (Step 28): deterministic, offline, research only.

python -m vicekrack agent-run DATASET_ID [--as-of 2026-01-15T15:30:00Z] [--save]
python -m vicekrack agent-inspect RUN_ID [--role trend_agent]
python -m vicekrack agent-list
"""

import argparse
import json
import sys

from ..contracts import utc_now
from ..errors import TradingError
from ..indicators.store import load_indicator_config
from ..market.store import MarketStore, load_market_config
from ..signals.store import load_signal_config
from .controller import STAGES, run_workflow
from .store import AgentRunStore, load_agent_config

COMMANDS = {"agent-run", "agent-inspect", "agent-list"}


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack research-agent workflow (research only)")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("agent-run", help="Run Market Scout -> Trend -> Strategy -> Risk Review at a simulated time")
    run.add_argument("dataset_id")
    run.add_argument("--as-of", help="Simulated time, UTC (default: the dataset's last bar close)")
    run.add_argument("--strategy", action="append", help="Research strategy name (repeatable); overrides the config list")
    run.add_argument("--save", action="store_true", help="Save the run under runtime/trading/agents/")
    inspect = commands.add_parser("agent-inspect", help="Show a saved research-agent run")
    inspect.add_argument("run_id")
    inspect.add_argument("--role", choices=STAGES)
    commands.add_parser("agent-list", help="List saved research-agent runs")
    return root


def view(run, role=None):
    stages = [s for s in run["stages"] if role is None or s["role"] == role]
    return {"notice": run["notice"], "run_id": run["run_id"], "research_only": run["research_only"],
            "authorization_possible": run["authorization_possible"], "sim_time_utc": run["sim_time_utc"],
            "dataset": run["dataset"], "status": run["status"], "failure": run["failure"],
            "verdict": run["final"]["verdict"], "stages": stages, "hashes": run["hashes"],
            "results_sha256": run["results_sha256"]}


def main(argv=None, root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    store = AgentRunStore(root)
    try:
        if args.command == "agent-run":
            workflow_config, _ = load_agent_config()
            market_config, _ = load_market_config()
            indicator_config, _ = load_indicator_config()
            signal_config, _ = load_signal_config()
            dataset = MarketStore(root).load(args.dataset_id)          # tampered datasets are rejected here
            run = run_workflow(dataset, workflow_config=workflow_config, market_config=market_config,
                               indicator_config=indicator_config, signal_config=signal_config, created_at=utc_now(),
                               sim_time=args.as_of, strategies=args.strategy)
            if args.save:
                store.save(run)
            output = {**view(run), "saved": bool(args.save)}
        elif args.command == "agent-inspect":
            output = view(store.load(args.run_id), args.role)
        else:
            output = {"runs": store.list()}
        code = 0
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "agent_storage_error", "message": "Cannot read or write local research-agent data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
