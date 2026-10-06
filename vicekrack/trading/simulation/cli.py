"""Simulation CLI (Step 29): offline, bounded, SIMULATED ONLY.

python -m vicekrack sim-run DATASET_ID [--policy config/simulation.paper.json] [--save]
python -m vicekrack sim-inspect RUN_ID [--section orders|fills|ledger|positions]
python -m vicekrack sim-list
python -m vicekrack sim-kill-switch status|engage|release
"""

import argparse
import json
import sys

from ..contracts import utc_now
from ..errors import TradingError
from ..indicators.store import load_indicator_config
from ..market.store import MarketStore, load_market_config
from ..signals.store import load_signal_config
from .engine import NOTICE, run_simulation
from .store import SimulationStore, load_policy

COMMANDS = {"sim-run", "sim-inspect", "sim-list", "sim-kill-switch"}
SECTIONS = {"orders": "orders", "fills": "fills", "ledger": "cash_ledger", "positions": "positions"}


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack offline paper-execution simulation (simulated only)")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("sim-run", help="Simulate research-signal execution on a stored dataset")
    run.add_argument("dataset_id")
    run.add_argument("--policy", default="config/simulation.paper.json")
    run.add_argument("--start", help="Replay start, UTC")
    run.add_argument("--end", help="Replay end, UTC")
    run.add_argument("--step-seconds", type=int)
    run.add_argument("--save", action="store_true")
    inspect = commands.add_parser("sim-inspect", help="Show a saved simulation run")
    inspect.add_argument("run_id")
    inspect.add_argument("--section", choices=sorted(SECTIONS))
    commands.add_parser("sim-list", help="List saved simulation runs")
    switch = commands.add_parser("sim-kill-switch", help="Simulation-only kill switch (blocks new simulated entries)")
    switch.add_argument("action", choices=("status", "engage", "release"))
    switch.add_argument("--policy", default="config/simulation.paper.json")
    return root


def view(run, section=None):
    base = {"notice": run["notice"], "simulated": True, "run_id": run["run_id"], "paper_account_access": False,
            "account": run["account"], "dataset": run["dataset"], "policy_sha256": run["policy_sha256"],
            "kill_switch": run["kill_switch"], "summary": run["summary"], "results_sha256": run["results_sha256"]}
    if section:
        base[section] = run[SECTIONS[section]]
    else:
        base["orders"] = [{"order_id": o["order_id"], "purpose": o["purpose"], "status": o["status"],
                           "source": o["source"]["strategy"] or o["source"]["rule"],
                           "decision_bar": o["source"]["decision_bar_sequence"], "quantity": o["quantity"],
                           "reasons": [h["reason_codes"] for h in o["history"]]} for o in run["orders"][-50:]]
    return base


def main(argv=None, root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    store = SimulationStore(root)
    try:
        if args.command == "sim-run":
            policy, _ = load_policy(args.policy)
            dataset = MarketStore(root).load(args.dataset_id)          # tampered datasets are rejected here
            run = run_simulation(dataset, policy, market_config=load_market_config()[0],
                                 indicator_config=load_indicator_config()[0], signal_config=load_signal_config()[0],
                                 kill_switch=store.kill_switch(policy), created_at=utc_now(), start=args.start,
                                 end=args.end, step_seconds=args.step_seconds)
            if args.save:
                store.save(run)
            output = {**view(run), "saved": bool(args.save)}
        elif args.command == "sim-inspect":
            output = view(store.load(args.run_id), args.section)
        elif args.command == "sim-list":
            output = {"notice": NOTICE, "runs": store.list()}
        else:
            policy, _ = load_policy(args.policy)
            if args.action != "status":
                store.set_kill_switch(args.action == "engage", utc_now())
            engaged, source = store.kill_switch(policy)
            output = {"simulation_kill_switch_engaged": engaged, "source": source}
        code = 0
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "sim_storage_error", "message": "Cannot read or write local simulation data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
