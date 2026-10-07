"""Timeline CLI (Step 31): read-only. Lists, inspects and replays timelines; never runs anything.

python -m vicekrack events-list [--department trading|content] [--origin recorded|reconstructed|all]
python -m vicekrack events-inspect ID [--from-sequence N] [--component NAME]
python -m vicekrack events-replay ID [--delay-ms N] [--max-events N] [--from-sequence N]

ID is a recorded timeline (tl-...) or a saved record to reconstruct from: a research-agent
run (rar-...), a simulation run (srun-...) or a content production (prod-...).

Replay prints one JSON line per recorded event, waiting a bounded delay between lines. It
only displays what was recorded or reconstructed: it loads saved files read-only and
never reruns agents, simulations or productions, places orders, or writes anything.
Department adapters are imported here, lazily, so the event core stays department-free.
"""

import argparse
import json
import sys
import time

from ..errors import NetworkError
from .contract import NOTICE, display, fold
from .store import EventStore, load_events_config

COMMANDS = {"events-list", "events-inspect", "events-replay"}


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack execution timelines (read-only)")
    commands = root.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("events-list", help="List recorded and reconstructable timelines")
    listing.add_argument("--department", choices=("trading", "content"))
    listing.add_argument("--origin", choices=("recorded", "reconstructed", "all"), default="all")
    inspect = commands.add_parser("events-inspect", help="Show one timeline and its display states")
    inspect.add_argument("timeline")
    inspect.add_argument("--from-sequence", type=int, default=1)
    inspect.add_argument("--component")
    replay = commands.add_parser("events-replay", help="Replay a saved timeline's events at a controlled speed")
    replay.add_argument("timeline")
    replay.add_argument("--delay-ms", type=int)
    replay.add_argument("--max-events", type=int)
    replay.add_argument("--from-sequence", type=int, default=1)
    return root


def load_timeline(identifier, root=None, config=None):
    """Recorded timeline, or a reconstruction of one saved record (re-validated on load)."""
    text = str(identifier)
    if text.startswith("tl-"):
        return EventStore(root, config).load(text)
    if text.startswith("rar-"):
        from ..trading.agents.store import AgentRunStore
        from ..trading.timeline import from_agent_run
        return from_agent_run(AgentRunStore(root).load(text))
    if text.startswith("srun-"):
        from ..trading.simulation.store import SimulationStore
        from ..trading.timeline import from_simulation_run
        return from_simulation_run(SimulationStore(root).load(text))
    if text.startswith("prod-"):
        from ..production_timeline import load_production_timeline
        return load_production_timeline(text, root)
    raise NetworkError("invalid_timeline_id", "Use a tl-, rar-, srun- or prod- ID.")


def reconstructable(department, root=None):
    rows = []
    if department in (None, "trading"):
        from ..trading.agents.store import AgentRunStore
        from ..trading.simulation.store import SimulationStore
        for item in AgentRunStore(root).list():
            rows.append({"source_id": item["run_id"], "origin": "reconstructed", "department": "trading",
                         "kind": "research_agent_workflow", "readable": item.get("readable", True)})
        for item in SimulationStore(root).list():
            rows.append({"source_id": item["run_id"], "origin": "reconstructed", "department": "trading",
                         "kind": "simulation", "readable": item.get("readable", True)})
    if department in (None, "content"):
        from ..production_timeline import list_production_timelines
        rows.extend(list_production_timelines(root))
    return rows


def _bounded(rows, limit):
    return {"items": rows[:limit], "total": len(rows), "shown_limit": limit}


def _clip(value, low, high):
    return max(low, min(high, value))


def main(argv=None, root=None, sleep=time.sleep, out=None):
    out = out or sys.stdout
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = load_events_config()
        limit = config["limits"]["max_cli_items"]
        if args.command == "events-list":
            output = {"notice": NOTICE}
            if args.origin in ("recorded", "all"):
                output["recorded"] = _bounded(EventStore(root, config).list(args.department), limit)
            if args.origin in ("reconstructed", "all"):
                output["reconstructable"] = _bounded(reconstructable(args.department, root), limit)
        elif args.command == "events-inspect":
            view = load_timeline(args.timeline, root, config)
            events = [e for e in view["events"] if e["sequence"] >= args.from_sequence
                      and (args.component is None or e["component"] == args.component)]
            output = {**{k: v for k, v in view.items() if k != "events"}, "events": events[:limit],
                      "events_matching": len(events), "shown_limit": limit}
        else:
            return _replay(args, root, config, sleep, out)
        code = 0
    except NetworkError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "events_storage_error", "message": "Cannot read local event data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False), file=out)
    return code


def _replay(args, root, config, sleep, out):
    replay = config["replay"]
    delay = _clip(replay["default_delay_ms"] if args.delay_ms is None else args.delay_ms, 0, replay["max_delay_ms"])
    count = _clip(replay["max_events"] if args.max_events is None else args.max_events, 1, replay["max_events"])
    try:
        view = load_timeline(args.timeline, root, config)
    except NetworkError as error:
        print(json.dumps({"error": error.as_dict()}), file=out)
        return 1
    components = [row["component"] for row in view["components"]]
    print(json.dumps({"replay_of": view["timeline_id"], "origin": view["origin"], "department": view["department"],
                      "kind": view["kind"], "completeness": view["completeness"], "issues": view["issues"],
                      "delay_ms": delay, "max_events": count, "notice": "REPLAY of saved events; nothing is running."}),
          file=out)
    frames = 0
    for index, event in enumerate(view["events"]):
        if event["sequence"] < args.from_sequence:
            continue
        if frames >= count:
            break
        if frames:
            sleep(delay / 1000)
        states, last, _ = fold(view["events"][:index + 1], components)
        # Historical state right after this event, as recorded: not a claim about now.
        historical = display(states, last, live=True, degraded=False)
        frames += 1
        print(json.dumps({"frame": frames, "event": event,
                          "state_at_event": next(r["display_state"] for r in historical if r["component"] == event["component"])}),
              file=out)
    print(json.dumps({"replay_finished": True, "frames": frames, "remaining": max(0, len([
        e for e in view["events"] if e["sequence"] >= args.from_sequence]) - frames), "completeness": view["completeness"],
        "outcome": view["outcome"], "live": view["live"], "current_display": view["components"]}), file=out)
    return 0
