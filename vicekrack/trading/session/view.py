"""Read-only views of trading sessions (Step 39), shared by the CLI and the Living HQ.

Nothing here takes the session lock, runs a stage, creates folders or writes files. Every
record, checkpoint and completed artifact is re-validated on each read; a session whose
files fail validation is reported as such (with a fixed code), never shown as valid.
"""

from ..errors import TradingError
from .runner import RECORD_KINDS, ID_FIELD, next_commands, verify_artifact
from .store import SessionStore

MAX_SESSIONS = 200


def derived_status(checkpoint, live):
    """What the session is doing now: `running` only while its process holds the lock."""
    if live:
        return "running"
    if checkpoint["status"] in ("completed", "failed"):
        return checkpoint["status"]
    return "interrupted"                      # `created`/`running` with no process: it stopped unexpectedly


def _verified(store, record, checkpoint):
    """Per-stage verification of completed artifacts, in order. Returns (rows, documents, problem)."""
    documents, rows, problem = {}, [], None
    for stage in checkpoint["stages"]:
        name, row = stage["stage"], {"stage": stage["stage"], "verification": "not_applicable"}
        if stage["status"] == "completed" and problem is None:
            try:
                found = store.read_artifact(record["session_id"], name)
                if found is None or found[1] != stage["artifact"]["file_sha256"]:
                    raise TradingError("session_artifact_tampered", "x")
                document = verify_artifact(record, name, found[0], documents)
                if (stage["artifact"]["record_id"] != document[ID_FIELD[name]]
                        or stage["artifact"]["record_kind"] != RECORD_KINDS[name]
                        or stage["artifact"]["results_sha256"] != document["results_sha256"]):
                    raise TradingError("session_artifact_tampered", "x")
                documents[name] = document
                row["verification"] = "verified"
            except TradingError as error:
                if error.code == "sensitive_state":
                    raise
                problem = {"stage": name, "code": "session_artifact_tampered"}
                row["verification"] = "failed"
        elif stage["status"] == "completed":
            row["verification"] = "not_checked_after_earlier_failure"
        rows.append(row)
    return rows, documents, problem


def describe(session_id, root=None):
    store = SessionStore(root)
    record = store.load_record(session_id)
    checkpoint = store.load_checkpoint(session_id)
    live = store.live(session_id)
    verification, documents, problem = _verified(store, record, checkpoint)
    status = derived_status(checkpoint, live)
    stages = []
    for stage, check in zip(checkpoint["stages"], verification):
        stages.append({"stage": stage["stage"], "position": stage["position"], "status": stage["status"],
                       "attempts": stage["attempts"], "artifact": stage["artifact"],
                       "verification": check["verification"]})
    return {"session_id": record["session_id"], "correlation_id": record["correlation_id"], "status": status,
            "live": live, "checkpoint_status": checkpoint["status"], "checkpoint_revision": checkpoint["revision"],
            "created_at": record["created_at"], "dataset": record["dataset"], "notice": record["notice"],
            "inputs": {name: {"path": item["path"], "sha256": item["sha256"]} for name, item in record["inputs"].items()},
            "research_as_of_rule": record["inputs"]["session_config"]["document"]["research"]["as_of"],
            "versions": record["versions"], "stages": stages, "integrity_problem": problem,
            "manifest": documents.get("hq_summary"), "documents": documents,
            "next": [] if live else next_commands(record["session_id"], checkpoint["status"])}


def list_sessions(root=None, limit=MAX_SESSIONS):
    store = SessionStore(root)
    ids = store.ids()
    items = []
    for session_id in ids[:limit]:
        try:
            record = store.load_record(session_id)
            checkpoint = store.load_checkpoint(session_id)
            live = store.live(session_id)
            items.append({"session_id": session_id, "readable": True, "status": derived_status(checkpoint, live),
                          "live": live, "created_at": record["created_at"], "dataset_id": record["dataset"]["dataset_id"],
                          "symbol": record["dataset"]["symbol"], "interval": record["dataset"]["interval"],
                          "data_label": record["dataset"]["data_label"],
                          "stages_completed": sum(1 for s in checkpoint["stages"] if s["status"] == "completed")})
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            items.append({"session_id": session_id, "readable": False, "code": error.code})
    return items, len(ids)
