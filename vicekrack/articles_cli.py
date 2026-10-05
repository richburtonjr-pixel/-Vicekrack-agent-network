"""Explicit article commands (Step 20). Never crawl, schedule, verify or publish.

fetch-articles  fetch the article pages of stored Scout candidates (fixtures unless --live)
article-list    list stored Article Evidence (metadata only)
"""
import argparse
import json
import re
from pathlib import Path
from uuid import uuid4

from .articles import FixtureArticleFetcher, HttpArticleFetcher, fetch_for_candidates, load_policy, utc_now, validate_evidence
from .errors import NetworkError
from .orchestrator import ROOT, read_json
from .scout_cli import _publish
from .verification_cli import CANDIDATE_ID, load_pool

ARTICLE_FILE = re.compile(r"^art-[0-9a-f]{24}$")
MAX_ARTICLE_FILES = 2000


def article_paths(root=None):
    base = Path(root if root is not None else ROOT) / "runtime/articles"
    return base / "evidence", base / "runs"


def load_articles(root=None):
    """Valid stored Article Evidence and the number of invalid files."""
    folder, _ = article_paths(root)
    found, invalid = [], 0
    if folder.is_dir():
        for path in sorted(folder.glob("art-*.json"))[:MAX_ARTICLE_FILES]:
            try:
                if not ARTICLE_FILE.match(path.stem) or path.is_symlink():
                    raise ValueError
                evidence = read_json(path)
                validate_evidence(evidence)
                if evidence["article_id"] != path.stem:
                    raise ValueError
                found.append(evidence)
            except (NetworkError, OSError, ValueError, UnicodeError, TypeError, KeyError):
                invalid += 1
    return found, invalid


def fetch_articles(candidate_ids, policy_path, *, fetch_all=False, live=False, root=None, fetcher=None, clock=utc_now):
    policy = load_policy(policy_path)
    if policy["fetch_mode"] == "http" and not live:
        raise NetworkError("live_fetch_not_enabled", "This policy fetches from the internet; pass --live to allow it.")
    if fetcher is None:
        fetcher = HttpArticleFetcher() if policy["fetch_mode"] == "http" else FixtureArticleFetcher(policy)
    if fetcher.mode != policy["fetch_mode"]:
        raise NetworkError("invalid_article_policy", "Fetcher does not match the policy fetch_mode.")
    pool, _ = load_pool(root)
    by_id = {c["candidate_id"]: c for c in pool if c["content_profile"] == policy["profile"]}
    if fetch_all:
        targets = [by_id[cid] for cid in sorted(by_id)]
    else:
        if not candidate_ids:
            raise NetworkError("no_candidates_selected", "Name candidate IDs or pass --all.")
        if any(not CANDIDATE_ID.match(cid) for cid in candidate_ids):
            raise NetworkError("invalid_candidate_id", "Candidate IDs look like cand- followed by 24 hex characters.")
        missing = [cid for cid in candidate_ids if cid not in by_id]
        if missing:
            raise NetworkError("candidate_not_found", f"{len(missing)} selected candidate(s) are not stored or not valid.")
        targets = [by_id[cid] for cid in dict.fromkeys(candidate_ids)]
    evidence, rows = fetch_for_candidates(targets, policy, fetcher, clock=clock)
    evidence_dir, runs_dir = article_paths(root)
    saved = {}
    for item in evidence:
        saved[item["article_id"]] = _publish(item, evidence_dir, item["article_id"] + ".json")
    for row in rows:
        if row["status"] == "fetched" and not saved.get(row["article_id"], True):
            row["status"] = "unchanged"  # Same content already stored; never overwritten.
    run = {"contract": "article_fetch_run", "version": "1.0", "run_id": uuid4().hex,
           "fetch_mode": fetcher.mode, "profile": policy["profile"], "finished_at": clock(),
           "limits": policy["limits"], "articles": rows,
           "totals": {status: sum(r["status"] == status for r in rows)
                      for status in ("fetched", "unchanged", "failed", "skipped")},
           "verified": False}
    _publish(run, runs_dir, run["run_id"] + ".json")
    return run, (runs_dir / (run["run_id"] + ".json")).resolve()


def main():
    parser = argparse.ArgumentParser(description="Bounded article fetching for stored Scout candidates; never verifies or publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch-articles")
    fetch.add_argument("candidate_ids", nargs="*", metavar="CANDIDATE_ID")
    fetch.add_argument("--all", action="store_true", help="Every stored candidate (still capped by max_articles_per_run)")
    fetch.add_argument("--sources", default="config/article-sources.mock.json",
                       help="Article fetch policy relative to the project root (default: offline fixtures)")
    fetch.add_argument("--live", action="store_true", help="Allow HTTPS requests to approved article pages")
    commands.add_parser("article-list")
    args = parser.parse_args()
    try:
        if args.command == "fetch-articles":
            if args.all and args.candidate_ids:
                raise NetworkError("no_candidates_selected", "Use either candidate IDs or --all, not both.")
            run, path = fetch_articles(args.candidate_ids, args.sources, fetch_all=args.all, live=args.live)
            result = {"run_file": str(path), "fetch_mode": run["fetch_mode"], **run["totals"],
                      "articles": [{k: r[k] for k in ("candidate_id", "status", "code", "article_id")} for r in run["articles"]],
                      "verified": False, "published": False}
        else:
            found, invalid = load_articles()
            result = {"articles": [{"article_id": a["article_id"], "candidate_id": a["candidate_id"],
                                    "source_id": a["source"]["source_id"], "final_url": a["final_url"],
                                    "fetched_at": a["fetched_at"], "published_at": a["published_at"],
                                    "published_at_origin": a["published_at_origin"], "sentences": len(a["sentences"]),
                                    "excluded": a["excluded"], "verification": "unverified"} for a in found],
                      "invalid_files": invalid}
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
