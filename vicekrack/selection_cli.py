"""Explicit Story Selection commands. Offline and deterministic; never publish.

select-stories        rank Verification Records into a saved Selection Report
selection-history     show the bounded memory of stories already made into briefs
brief-from-selection  turn one `select` story into a Story Brief and record it in history
"""
import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from .errors import NetworkError
from .orchestrator import ROOT, read_json
from .scout_cli import _publish
from .selection import (
    empty_history, history_digest, load_profile, prune_history, rank_stories, select_brief, utc_now,
    validate_history, validate_report,
)
from .verification import load_policy
from .verification_cli import load_record, paths as verification_paths

RUN_ID = re.compile(r"^sel-[0-9a-f]{24}$")
MAX_RECORD_FILES = 1000


def selection_paths(root=None):
    base = Path(root if root is not None else ROOT) / "runtime/selection"
    return base / "reports", base


def history_path(profile, root=None):
    _, base = selection_paths(root)
    return base / f"history-{profile['profile']}.json"  # profile ID is schema-checked: no path characters.


def load_history(profile, root=None):
    """Return (history, digest). A missing file is an empty history; a corrupt one stops selection."""
    path = history_path(profile, root)
    if not path.exists():
        history = empty_history(profile)
        return history, history_digest(history)
    if path.is_symlink():
        raise NetworkError("invalid_story_history", "Story history cannot be a symbolic link.")
    try:
        history = read_json(path)
    except (OSError, ValueError, UnicodeError):
        raise NetworkError("invalid_story_history", "Story history is unreadable; restore it before selecting.") from None
    validate_history(history, profile)
    return history, history_digest(history)


def save_history(history, expected_digest, profile, root=None):
    """Atomically replace the history, refusing if someone else changed it meanwhile."""
    validate_history(history, profile)
    _, current = load_history(profile, root)
    if current != expected_digest:
        raise NetworkError("history_conflict", "Story history changed during this command; run it again.")
    path = history_path(profile, root)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(history, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise NetworkError("history_write_failed", "Could not save the story history.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _configs(policy_path, profile_path):
    policy, policy_sha = load_policy(policy_path)
    profile, profile_sha = load_profile(profile_path)
    if policy["profile"] != profile["profile"]:
        raise NetworkError("profile_mismatch", "Verification policy and editorial profile use different profiles.")
    return policy, policy_sha, profile, profile_sha


def select_stories(record_ids, policy_path, profile_path, *, select_all=False, root=None, clock=None):
    now = (clock or utc_now)()
    policy, policy_sha, profile, profile_sha = _configs(policy_path, profile_path)
    records, invalid = [], 0
    if select_all:
        folder, _ = verification_paths(root)
        files = sorted(folder.glob("ver-*.json"))[:MAX_RECORD_FILES] if folder.is_dir() else []
        for path in files:
            try:
                records.append(load_record(path.stem, policy, policy_sha, root))
            except NetworkError:
                invalid += 1
    else:
        if not record_ids:
            raise NetworkError("no_records_selected", "Name Verification Record IDs or pass --all.")
        records = [load_record(rid, policy, policy_sha, root) for rid in dict.fromkeys(record_ids)]
    if not records:
        raise NetworkError("no_verification_records", "No valid Verification Records to rank; run verify first.")
    history, _ = load_history(profile, root)
    report = rank_stories(records, profile, profile_sha, policy_sha, prune_history(history, profile, now), now)
    report["skipped"]["invalid_records"] = invalid
    validate_report(report)
    reports_dir, _ = selection_paths(root)
    _publish(report, reports_dir, report["selection_run_id"] + ".json")  # Same inputs -> same file.
    return report, (reports_dir / (report["selection_run_id"] + ".json")).resolve()


def load_report(run_id, root=None):
    if not isinstance(run_id, str) or not RUN_ID.match(run_id):
        raise NetworkError("invalid_selection_id", "Selection run IDs look like sel- followed by 24 hex characters.")
    reports_dir, _ = selection_paths(root)
    try:
        report = read_json(reports_dir / (run_id + ".json"))
    except FileNotFoundError:
        raise NetworkError("selection_not_found", "Selection Report does not exist.") from None
    except (OSError, ValueError, UnicodeError):
        raise NetworkError("invalid_selection_report", "Selection Report is unreadable.") from None
    validate_report(report)
    if report["selection_run_id"] != run_id:
        raise NetworkError("invalid_selection_report", "Selection Report file name does not match its ID.")
    return report


def brief_from_selection(run_id, policy_path, profile_path, *, record_id=None, brief_id=None, root=None, clock=None):
    now = (clock or utc_now)()
    policy, policy_sha, profile, profile_sha = _configs(policy_path, profile_path)
    report = load_report(run_id, root)
    if record_id is None:
        top = next((e for e in report["entries"] if e["disposition"] == "select"), None)
        if top is None:
            raise NetworkError("not_selected", "This Selection Report has no select entries.")
        record_id = top["record_id"]
    record = load_record(record_id, policy, policy_sha, root)
    history, digest = load_history(profile, root)
    current = prune_history(history, profile, now)
    brief, entry = select_brief(record, report, profile=profile, profile_sha256=profile_sha, policy=policy,
                                policy_sha256=policy_sha, history=current, now=now, brief_id=brief_id)
    _, briefs_dir = verification_paths(root)
    name = f"{brief['brief_id']}-{uuid4().hex}.json"
    if not _publish(brief, briefs_dir, name):
        raise NetworkError("brief_write_failed", "Could not publish the brief without overwriting.")
    updated = dict(current, entries=[entry, *current["entries"]])
    save_history(prune_history(updated, profile, now), digest, profile, root)
    return brief, (briefs_dir / name).resolve(), entry


def main():
    parser = argparse.ArgumentParser(description="Editorial story selection; never publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    rank = commands.add_parser("select-stories")
    rank.add_argument("record_ids", nargs="*", metavar="RECORD_ID")
    rank.add_argument("--all", action="store_true", help="Rank every stored Verification Record")
    show = commands.add_parser("selection-history")
    make = commands.add_parser("brief-from-selection")
    make.add_argument("selection_run_id", metavar="SELECTION_RUN_ID")
    make.add_argument("--record", metavar="RECORD_ID", help="Default: the top select entry")
    make.add_argument("--brief-id")
    for command in (rank, show, make):
        command.add_argument("--policy", default="config/verification.mock.json")
        command.add_argument("--profile", default="config/editorial.mock.json")
    args = parser.parse_args()
    try:
        if args.command == "select-stories":
            if args.all and args.record_ids:
                raise NetworkError("no_records_selected", "Use either record IDs or --all, not both.")
            report, path = select_stories(args.record_ids, args.policy, args.profile, select_all=args.all)
            result = {"report_file": str(path), "selection_run_id": report["selection_run_id"],
                      "summary": report["summary"], "skipped": report["skipped"],
                      "audience_signals": report["audience_signals"],
                      "ranking": [{key: e[key] for key in ("rank", "record_id", "disposition", "score", "title", "reasons")}
                                  for e in report["entries"]],
                      "published": False}
        elif args.command == "selection-history":
            _, _, profile, _ = _configs(args.policy, args.profile)
            history, _ = load_history(profile)
            result = {"entries": [{key: e[key] for key in ("selection_id", "brief_id", "record_id", "topics", "selected_at")}
                                  for e in history["entries"]],
                      "max_entries": profile["history"]["max_entries"], "window_days": profile["history"]["window_days"]}
        else:
            brief, path, entry = brief_from_selection(args.selection_run_id, args.policy, args.profile,
                                                      record_id=args.record, brief_id=args.brief_id)
            result = {"brief_file": str(path), "brief_id": brief["brief_id"], "selection_id": entry["selection_id"],
                      "score": brief["editorial"]["score"], "novelty": brief["editorial"]["novelty"],
                      "claims_verified": sum(c["status"] == "verified" for c in brief["claims"]),
                      "next_command": "draft-short BRIEF_FILE", "published": False}
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
