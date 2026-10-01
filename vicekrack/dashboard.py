"""One-shot, metadata-only CLI views. Never executes agents or resumes runs."""
import argparse
import json
import os
import re
from .errors import NetworkError
from .manager import ORDER, SAFE_CODES, PREFLIGHT_CODES
from .orchestrator import Orchestrator
from .persistence import RunStore


def label(value):
    # Registry fields are user controlled; suppress credential-shaped text and controls.
    text = str(value) if value is not None else "-"
    if any(os.environ.get(name) and os.environ[name] in text for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")):
        return "[redacted]"
    if re.search(r"sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}", text):
        return "[redacted]"
    return "".join(c if c.isprintable() and c.isascii() else "?" for c in text)[:100]


def summarize(state):
    workflow = state.get("workflow_state")
    completed = len(state["history"])
    next_agent = ORDER[completed] if completed < 3 else None
    result = {"run_id": state["run_id"], "status": state["status"],
              "completed": completed, "next_agent": next_agent, "updated_at": state["updated_at"],
              "retry_count": None, "remaining_attempts": None, "error": None, "trace": []}
    if workflow:
        result["retry_count"] = workflow["retry_count"]
        result["remaining_attempts"] = (max(0, 1 + workflow["max_retries"] - workflow["attempts"][next_agent])
                                        if next_agent else 0)
        result["error"] = workflow["blocked_error"] or (workflow["failures"][-1]["error"] if workflow["failures"] else None)
        result["trace"] = workflow["audit_trace"]
    else:
        result["trace"] = state["trace"]
        code = (state.get("outcome") or {}).get("error", {}).get("code")
        result["error"] = code if code in SAFE_CODES | PREFLIGHT_CODES else None
    if state["status"] == "completed":
        result["recovery"] = "complete"
    elif workflow and (workflow["status"] == "exhausted" or result["remaining_attempts"] == 0):
        result["recovery"] = "budget exhausted; no further requests permitted"
    elif state["status"] == "uncertain":
        result["recovery"] = "explicit resume --retry-uncertain required; another charge is possible"
    else:
        result["recovery"] = "explicit resume required; configuration is rechecked on resume"
    return result


def collect(store, run_id=None):
    if run_id:
        return [summarize(store.inspect(run_id))]
    rows = []
    for path in sorted(store.directory.glob("*.json")):
        # Do not echo arbitrary filenames or corrupt file contents.
        if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
            rows.append({"run_id": "[invalid filename]", "error": "invalid_run_id"})
            continue
        try:
            rows.append(summarize(store.inspect(path.stem)))
        except NetworkError as error:
            rows.append({"run_id": path.stem, "error": error.code})
        except OSError:
            rows.append({"run_id": path.stem, "error": "storage_error"})
    return rows


def render(data):
    lines = ["VICEKRACK - LOCAL DASHBOARD", "One-shot snapshot; no agents executed."]
    if "agents" in data:
        lines.append("\nAGENTS (configured availability, not a live provider health check)")
        for row in data["agents"]:
            lines.append(f"{row['agent']} | {row['status']} | provider={row['provider']} | capabilities={row['capabilities']} | next={row['next_agent']}")
    if "runs" in data:
        lines.append("\nSAVED RUNS")
        if not data["runs"]:
            lines.append("No saved runs. Start one with: python -m vicekrack run examples/workflow-task.json")
        for row in data["runs"]:
            if "status" not in row:
                lines.append(f"{row['run_id']} | {row['error']}")
                continue
            lines.append(f"{row['run_id']} | {row['status']} | stages={row['completed']}/3 | next={row['next_agent'] or '-'} | updated={row['updated_at']}")
            lines.append(f"  retries used={row['retry_count'] if row['retry_count'] is not None else 'unknown (legacy)'} | next-stage attempts left={row['remaining_attempts'] if row['remaining_attempts'] is not None else 'unknown (legacy)'} | last error={row['error'] or '-'}")
            lines.append("  " + row["recovery"])
            if data.get("detail"):
                for event in row["trace"]:
                    lines.append("  " + " | ".join(label(event.get(key)) for key in ("timestamp", "agent", "provider", "attempt", "status", "error")))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Local metadata dashboard; never starts or resumes agents")
    parser.add_argument("command", choices=["dashboard", "agents"])
    parser.add_argument("run_id", nargs="?", help="Show the trace of one saved run (dashboard only)")
    parser.add_argument("--registry", default="config/agents.workflow.json")
    parser.add_argument("--json", action="store_true", help="Output the same metadata as JSON")
    args = parser.parse_args()
    if args.command == "agents" and args.run_id:
        parser.error("agents does not accept a run ID")
    try:
        data = {}
        if not args.run_id:
            runner = Orchestrator(registry_path=args.registry, providers={})
            data["agents"] = [{key: label(value) for key, value in row.items()} for row in runner.manager.inventory()]
        if args.command == "dashboard":
            data.update(runs=collect(RunStore(), args.run_id), detail=bool(args.run_id))
        print(json.dumps(data, indent=2) if args.json else render(data))
        return 1 if any("status" not in row for row in data.get("runs", [])) else 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}) if args.json else "Dashboard error: " + error.code)
        return 1
    except (OSError, ValueError):
        print('{"error": {"code": "dashboard_unavailable"}}' if args.json else "Dashboard unavailable: check local files and permissions.")
        return 1
