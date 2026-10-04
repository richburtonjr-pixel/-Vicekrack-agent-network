"""Explicit Scout commands: check a source list, run research intake, list candidates.

`scout` reads local fixtures unless the source list uses http mode AND --live is given.
Nothing is verified, drafted, scheduled or published. Run output reports counts and
error codes only; `scout-list` shows candidate metadata (headlines are external text).
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
from .persistence import reject_secrets
from .scout import ROOT as PROJECT_ROOT, FixtureFetcher, HttpFetcher, load_sources, run_scout, validate_candidate

CANDIDATE_FILE = re.compile(r"^cand-[0-9a-f]{24}$")


def store_paths(root=None):
    base = Path(root if root is not None else ROOT) / "runtime/scout"
    return base / "candidates", base / "runs"


def _publish(document, folder, name):
    """Write a complete JSON file via temp file + exclusive hard link. False if name exists."""
    temporary = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True
    except OSError:
        raise NetworkError("scout_write_failed", "Could not publish a complete local Scout file without overwriting.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Never promote an orphan temporary file.


def existing_fingerprints(folder):
    """IDs of all stored candidate files, and (source_id, title_sha256) of valid ones."""
    ids, titles = set(), set()
    if not folder.is_dir():
        return ids, titles
    for path in folder.glob("cand-*.json"):
        if not CANDIDATE_FILE.match(path.stem):
            continue
        ids.add(path.stem)  # Even an unreadable file still reserves its ID: never overwrite.
        try:
            candidate = read_json(path)
            validate_candidate(candidate)
            titles.add((candidate["source"]["source_id"], candidate["fingerprints"]["title_sha256"]))
        except (NetworkError, OSError, ValueError, UnicodeError, TypeError, KeyError):
            continue
    return ids, titles


def scout_once(sources_path, *, live=False, root=None, fetcher=None, clock=None):
    """Run one bounded intake and save new candidates plus a run report."""
    config, config_sha = load_sources(sources_path)
    if config["fetch_mode"] == "http" and not live:
        raise NetworkError("live_fetch_not_enabled",
                           "This source list fetches from the internet; pass --live to allow it.")
    if fetcher is None:
        fetcher = HttpFetcher() if config["fetch_mode"] == "http" else FixtureFetcher(PROJECT_ROOT)
    if fetcher.mode != config["fetch_mode"]:
        raise NetworkError("invalid_scout_config", "Fetcher does not match the source list fetch_mode.")
    candidates_dir, runs_dir = store_paths(root)
    options = {"clock": clock} if clock else {}
    known_ids, known_titles = existing_fingerprints(candidates_dir)
    candidates, report = run_scout(config, config_sha, fetcher, existing_ids=known_ids,
                                   existing_titles=known_titles, **options)
    saved = []
    for candidate in candidates:
        if _publish(candidate, candidates_dir, candidate["candidate_id"] + ".json"):
            saved.append(candidate["candidate_id"])
    # A concurrent run may have stored the same story first; report what this run saved.
    race_duplicates = len(candidates) - len(saved)
    report["candidate_ids"] = saved
    report["totals"]["candidates_new"] = len(saved)
    report["totals"]["duplicates"] += race_duplicates
    report["run_id"] = uuid4().hex
    reject_secrets(report)
    _publish(report, runs_dir, report["run_id"] + ".json")
    return report, (runs_dir / (report["run_id"] + ".json")).resolve(), candidates_dir.resolve()


def list_candidates(root=None, limit=20):
    candidates_dir, _ = store_paths(root)
    rows = []
    if candidates_dir.is_dir():
        for path in sorted(candidates_dir.glob("cand-*.json")):
            try:
                candidate = read_json(path)
                validate_candidate(candidate)
                if candidate["candidate_id"] != path.stem:
                    raise NetworkError("invalid_candidate", "File name does not match candidate_id.")
            except (NetworkError, OSError, ValueError, UnicodeError, TypeError, KeyError):
                rows.append({"candidate_id": path.stem[:40], "error": "invalid_candidate"})
                continue
            rows.append({"candidate_id": candidate["candidate_id"], "source_id": candidate["source"]["source_id"],
                         "kind": candidate["source"]["kind"], "published_at": candidate["published_at"],
                         "retrieved_at": candidate["retrieved_at"], "title": candidate["title"],
                         "claims": len(candidate["candidate_claims"]),
                         "verification": candidate["verification"]["status"]})
    valid = sorted((r for r in rows if "error" not in r),
                   key=lambda r: (r["published_at"] or "", r["candidate_id"]), reverse=True)
    return valid[:limit] + [r for r in rows if "error" in r]


def main():
    parser = argparse.ArgumentParser(description="Bounded research intake from approved feeds; never verifies or publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("scout-sources")
    check.add_argument("--sources", default="config/scout-sources.mock.json",
                       help="Source list relative to the project root")
    run = commands.add_parser("scout")
    run.add_argument("--sources", default="config/scout-sources.mock.json",
                     help="Source list relative to the project root (default: offline fixtures)")
    run.add_argument("--live", action="store_true", help="Allow HTTPS requests to the listed feeds")
    show = commands.add_parser("scout-list")
    show.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    args = parser.parse_args()
    try:
        if args.command == "scout-sources":
            config, config_sha = load_sources(args.sources)
            result = {"valid": True, "profile": config["profile"], "fetch_mode": config["fetch_mode"],
                      "requires_live_flag": config["fetch_mode"] == "http",
                      "sources_config_sha256": config_sha, "keywords": config["keywords"],
                      "limits": config["limits"],
                      "sources": [{"source_id": s["source_id"], "enabled": s["enabled"], "kind": s["kind"],
                                   "category": s["category"], "feed_url": s["feed_url"],
                                   "link_hosts": s["link_hosts"]} for s in config["sources"]]}
        elif args.command == "scout":
            report, run_file, candidates_dir = scout_once(args.sources, live=args.live)
            result = {"run_file": str(run_file), "run_id": report["run_id"], "fetch_mode": report["fetch_mode"],
                      "candidates_dir": str(candidates_dir), **report["totals"],
                      "truncated": report["truncated"],
                      "failed_sources": {r["source_id"]: r["error_code"] for r in report["sources"] if r["status"] == "failed"},
                      "verified": False, "published": False}
        else:
            result = {"candidates": list_candidates(limit=args.limit), "verified": False}
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
