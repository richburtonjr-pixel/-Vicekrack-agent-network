"""Explicit Verification commands. Offline, deterministic, never publish.

verify               verify stored Scout candidates and save Verification Records
verify-list          list saved records (re-validated by decision replay)
brief-from-verified  build a Story Brief from verified record claims
"""
import argparse
import json
import re
from pathlib import Path
from uuid import uuid4

from .errors import NetworkError
from .orchestrator import ROOT, read_json
from .scout import validate_candidate
from .scout_cli import _publish, store_paths as scout_paths
from .verification import load_policy, utc_now, validate_record, verify_candidate
from .verified_brief import build_verified_brief

MAX_TARGETS = 50
RECORD_FILE = re.compile(r"^ver-[0-9a-f]{24}$")
CANDIDATE_ID = re.compile(r"^cand-[0-9a-f]{24}$")


def paths(root=None):
    base = Path(root if root is not None else ROOT) / "runtime"
    return base / "verification/records", base / "briefs"


def load_pool(root=None, limit=500):
    """Valid stored Scout candidates (sorted by ID) and the number of invalid files."""
    folder, _ = scout_paths(root)
    pool, invalid = [], 0
    if folder.is_dir():
        for path in sorted(folder.glob("cand-*.json"))[:limit * 2]:
            try:
                candidate = read_json(path)
                validate_candidate(candidate)
                if candidate["candidate_id"] != path.stem:
                    raise ValueError
                pool.append(candidate)
            except (NetworkError, OSError, ValueError, UnicodeError, TypeError, KeyError):
                invalid += 1
    return pool, invalid


def verify_stored(candidate_ids, policy_path, *, verify_all=False, root=None, clock=utc_now):
    policy, policy_sha = load_policy(policy_path)
    pool, invalid = load_pool(root, policy["rules"]["max_candidates"])
    profile_pool = [c for c in pool if c["content_profile"] == policy["profile"]]
    by_id = {c["candidate_id"]: c for c in profile_pool}
    if verify_all:
        targets = sorted(by_id)
    else:
        if not candidate_ids:
            raise NetworkError("no_candidates_selected", "Name candidate IDs or pass --all.")
        if any(not CANDIDATE_ID.match(cid) for cid in candidate_ids):
            raise NetworkError("invalid_candidate_id", "Candidate IDs look like cand- followed by 24 hex characters.")
        missing = [cid for cid in candidate_ids if cid not in by_id]
        if missing:
            raise NetworkError("candidate_not_found", f"{len(missing)} selected candidate(s) are not stored or not valid.")
        targets = list(dict.fromkeys(candidate_ids))
    truncated = len(targets) > MAX_TARGETS
    targets = targets[:MAX_TARGETS]
    records_dir, _ = paths(root)
    rows, totals = [], {"verified": 0, "corroborated": 0, "disputed": 0, "insufficient_evidence": 0, "rejected": 0}
    for cid in targets:
        record = verify_candidate(by_id[cid], profile_pool, policy, policy_sha, clock=clock)
        saved = _publish(record, records_dir, record["record_id"] + ".json")
        for status, count in record["summary"].items():
            totals[status] += count
        rows.append({"record_id": record["record_id"], "candidate_id": cid,
                     "claim_statuses": {c["claim_id"]: c["status"] for c in record["claims"]},
                     "flags": record["flags"], "saved": saved,
                     "record_file": str((records_dir / (record["record_id"] + ".json")).resolve())})
    return {"policy_profile": policy["profile"], "candidates_considered": len(profile_pool),
            "invalid_candidate_files": invalid, "records": rows, "claim_totals": totals,
            "truncated": truncated, "published": False}


def load_record(record_id, policy, policy_sha, root=None):
    if not isinstance(record_id, str) or not RECORD_FILE.match(record_id):
        raise NetworkError("invalid_record_id", "Record IDs look like ver- followed by 24 hex characters.")
    records_dir, _ = paths(root)
    path = records_dir / (record_id + ".json")
    try:
        record = read_json(path)
    except FileNotFoundError:
        raise NetworkError("record_not_found", "Verification Record does not exist.") from None
    except (OSError, ValueError, UnicodeError):
        raise NetworkError("invalid_verification_record", "Verification Record is unreadable.") from None
    validate_record(record, policy, policy_sha)
    if record["record_id"] != record_id:
        raise NetworkError("invalid_verification_record", "Record file name does not match its ID.")
    return record


def list_records(policy_path, root=None, status=None):
    policy, policy_sha = load_policy(policy_path)
    records_dir, _ = paths(root)
    rows = []
    if records_dir.is_dir():
        for path in sorted(records_dir.glob("ver-*.json")):
            try:
                record = load_record(path.stem, policy, policy_sha, root)
            except NetworkError as error:
                rows.append({"record_id": path.stem[:40], "error": error.code})
                continue
            if status and record["summary"].get(status, 0) == 0:
                continue
            rows.append({"record_id": record["record_id"], "candidate_id": record["candidate"]["candidate_id"],
                         "source_id": record["candidate"]["source_id"], "title": record["candidate"]["title"],
                         "verified_at": record["verified_at"], "summary": record["summary"], "flags": record["flags"]})
    return rows


def brief_from_records(record_ids, policy_path, *, topic=None, angle=None, brief_id=None,
                       include_corroborated=False, root=None, clock=utc_now):
    policy, policy_sha = load_policy(policy_path)
    records = [load_record(rid, policy, policy_sha, root) for rid in record_ids]
    brief = build_verified_brief(records, policy, policy_sha, topic=topic, angle=angle, brief_id=brief_id,
                                 include_corroborated=include_corroborated, clock=clock)
    _, briefs_dir = paths(root)
    name = f"{brief['brief_id']}-{uuid4().hex}.json"
    if not _publish(brief, briefs_dir, name):
        raise NetworkError("brief_write_failed", "Could not publish the brief without overwriting.")
    return brief, (briefs_dir / name).resolve()


def main():
    parser = argparse.ArgumentParser(description="Deterministic claim verification; never publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("verify")
    run.add_argument("candidate_ids", nargs="*", metavar="CANDIDATE_ID")
    run.add_argument("--all", action="store_true", help="Verify every stored candidate (at most 50 per run)")
    run.add_argument("--policy", default="config/verification.mock.json")
    show = commands.add_parser("verify-list")
    show.add_argument("--policy", default="config/verification.mock.json")
    show.add_argument("--status", choices=["verified", "corroborated", "disputed", "insufficient_evidence", "rejected"])
    brief = commands.add_parser("brief-from-verified")
    brief.add_argument("record_ids", nargs="+", metavar="RECORD_ID")
    brief.add_argument("--policy", default="config/verification.mock.json")
    brief.add_argument("--topic")
    brief.add_argument("--angle")
    brief.add_argument("--brief-id")
    brief.add_argument("--include-corroborated", action="store_true",
                       help="Also add corroborated claims, as unverified (draft-only)")
    args = parser.parse_args()
    try:
        if args.command == "verify":
            if args.all and args.candidate_ids:
                raise NetworkError("no_candidates_selected", "Use either candidate IDs or --all, not both.")
            result = verify_stored(args.candidate_ids, args.policy, verify_all=args.all)
        elif args.command == "verify-list":
            result = {"records": list_records(args.policy, status=args.status)}
        else:
            made, target = brief_from_records(args.record_ids, args.policy, topic=args.topic, angle=args.angle,
                                              brief_id=args.brief_id, include_corroborated=args.include_corroborated)
            unverified = sum(c["status"] != "verified" for c in made["claims"])
            result = {"brief_file": str(target), "brief_id": made["brief_id"],
                      "claims_verified": len(made["claims"]) - unverified, "claims_unverified": unverified,
                      "next_command": "draft-short BRIEF_FILE", "published": False}
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
