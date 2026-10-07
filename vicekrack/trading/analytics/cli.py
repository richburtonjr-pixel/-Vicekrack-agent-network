"""Analytics CLI (Step 30): read-only, offline, SIMULATED descriptive analytics.

python -m vicekrack analytics-generate SIM_RUN_ID [--save]
python -m vicekrack analytics-inspect REPORT_ID [--section ...]
python -m vicekrack analytics-list
"""

import argparse
import json
import sys

from ..contracts import utc_now
from ..errors import TradingError
from ..market.store import MarketStore, load_market_config
from ..simulation.store import SimulationStore
from .report import NOTICE, build_report
from .store import AnalyticsStore, load_analytics_config

COMMANDS = {"analytics-generate", "analytics-inspect", "analytics-list"}
SECTIONS = ("account", "closed_trades", "open_positions", "equity_curve", "drawdown", "holding", "exposure",
            "orders", "attribution")
SUMMARY = ("account", "drawdown", "holding", "exposure")


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack simulation analytics (read-only, simulated)")
    commands = root.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("analytics-generate", help="Analyse a saved simulation run")
    generate.add_argument("run_id")
    generate.add_argument("--save", action="store_true")
    inspect = commands.add_parser("analytics-inspect", help="Show a saved analytics report")
    inspect.add_argument("report_id")
    inspect.add_argument("--section", choices=SECTIONS)
    commands.add_parser("analytics-list", help="List saved analytics reports")
    return root


def view(report, section, max_items):
    base = {"notice": report["notice"], "simulated": True, "read_only": True, "annualized": False,
            "report_id": report["report_id"], "source": report["source"], "period": report["period"],
            "results_sha256": report["results_sha256"]}
    if section is None:
        base.update({key: report[key] for key in SUMMARY})
        base["closed_trades"] = {k: v for k, v in report["closed_trades"].items() if k != "trades"}
        base["orders"] = {k: v for k, v in report["orders"].items() if k != "pending_orders"}
        base["attribution"] = {"shared_account": report["attribution"]["shared_account"],
                               "explanation": report["attribution"]["explanation"]}
        return base
    value = report[section]
    if section == "equity_curve":
        base["equity_curve_points"] = len(value)
        value = value[-max_items:]
    elif section == "closed_trades":
        value = {**value, "trades": value["trades"][-max_items:]}
    elif section == "open_positions":
        value = value[-max_items:]
    base[section] = value
    base["shown_limit"] = max_items
    return base


def main(argv=None, root=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    store = AnalyticsStore(root)
    try:
        config, config_sha = load_analytics_config()
        limit = config["limits"]["max_cli_items"]
        if args.command == "analytics-generate":
            run = SimulationStore(root).load(args.run_id)                 # tampered runs: sim_run_corrupt
            dataset = MarketStore(root).load(run["dataset"]["dataset_id"])  # tampered datasets: dataset_corrupt
            report = build_report(run, dataset, market_config=load_market_config()[0], analytics_config=config,
                                  analytics_config_sha256=config_sha, created_at=utc_now())
            if args.save:
                store.save(report)
            output = {**view(report, None, limit), "saved": bool(args.save)}
        elif args.command == "analytics-inspect":
            output = view(store.load(args.report_id), args.section, limit)
        else:
            reports, total = store.list(limit)
            output = {"notice": NOTICE, "reports": reports, "total": total, "shown_limit": limit}
        code = 0
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "analytics_storage_error", "message": "Cannot read or write local analytics data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
