"""Story Selection (Step 18): editorial ranking between Verification and Story Brief.

Reads Verification Records (already replay-validated) and decides which verified stories
are worth making, with an explicit disposition per story:

  select  verified by a primary source, relevant, recent, materially new, score high enough
  hold    potentially worthwhile but needs more evidence, is disputed, is an unconfirmed
          rumor, is not recent, or is too similar to an existing story
  reject  no verified evidence, contradicted, irrelevant, stale, a duplicate, or too weak

Hard gates are applied before scoring, so excitement can never compensate for weak
evidence. The 0-100 score uses fixed weights over confidence, authority, corroboration,
recency, relevance and significance. Audience/popularity data is not available and is
recorded as unavailable; it is never invented and never scored.

A bounded story history (fingerprints, not article text) separates exact duplicates,
near-duplicates and genuine updates (a new verified claim with new facts). Selection
never invents facts or changes verification: the brief handoff reuses the Step 17
builder, which admits verified claims only.
"""

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import NetworkError
from .persistence import reject_secrets
from .story_brief import validate_story_brief
from .verification import INSTRUCTION_TEXT, analyze
from .verified_brief import build_verified_brief

ROOT = Path(__file__).resolve().parent.parent
PROFILE_SCHEMA = ROOT / "schemas/editorial-profile.schema.json"
REPORT_SCHEMA = ROOT / "schemas/selection-report.schema.json"
HISTORY_SCHEMA = ROOT / "schemas/story-history.schema.json"
COMPONENTS = ("confidence", "authority", "corroboration", "recency", "relevance", "significance")
DISPOSITION_ORDER = {"select": 0, "hold": 1, "reject": 2}
UNAVAILABLE = {"status": "unavailable", "used_in_score": False}
REPORT_MAX_AGE = timedelta(days=1)

REJECT_REASONS = ("instruction_like_text", "no_verified_claims", "not_relevant", "future_date", "stale",
                  "duplicate_of_previous", "duplicate_in_batch", "below_hold_score")
HOLD_REASONS = ("corroborated_only", "contradicted_claims", "disputed_claims", "unconfirmed_rumor", "not_recent",
                "publish_date_unknown", "near_duplicate_of_previous", "similar_to_higher_ranked",
                "below_select_score", "selection_limit")
PHRASES = {
    "primary_verified": "verified by a first-hand primary source",
    "independent_corroboration": "supported by independent origins",
    "recent": "recent",
    "high_priority_topic": "high-priority topic",
    "new_story": "materially new",
    "update_to_previous": "a genuine update with new verified facts",
    "supersedes_older_official": "a newer official statement replaces an older conflicting one",
    "no_verified_claims": "no verified claims",
    "corroborated_only": "only corroborated, not officially verified; needs more evidence",
    "contradicted_claims": "some claims are contradicted",
    "disputed_claims": "some claims are disputed",
    "unconfirmed_rumor": "rumor or leak without primary confirmation",
    "not_relevant": "not relevant to this profile",
    "stale": "too old",
    "not_recent": "older than the selection window",
    "publish_date_unknown": "publication date unknown",
    "future_date": "publication date is in the future",
    "instruction_like_text": "contains instruction-like text",
    "duplicate_of_previous": "duplicate of a story already made",
    "near_duplicate_of_previous": "too similar to a story already made, with nothing materially new",
    "similar_to_higher_ranked": "too similar to a higher-ranked story in this run",
    "duplicate_in_batch": "duplicate of a higher-ranked story in this run",
    "below_select_score": "score below the selection threshold",
    "below_hold_score": "score below the hold threshold",
    "selection_limit": "selection limit for this run reached",
}


@lru_cache(maxsize=None)
def _validator(path):
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _parse(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now():
    return _stamp(datetime.now(timezone.utc))


def _schema_check(document, path, code, label):
    error = next(_validator(str(path)).iter_errors(document), None)
    if error is not None:
        location = ".".join(str(p) for p in error.absolute_path) or "$"
        raise NetworkError(code, f"{label} rejected at {location}: schema rule '{error.validator}'.")


# ---------------------------------------------------------------- profile

def validate_profile(profile):
    reject_secrets(profile)
    _schema_check(profile, PROFILE_SCHEMA, "invalid_editorial_profile", "Editorial profile")

    def fail(reason):
        raise NetworkError("invalid_editorial_profile", f"Editorial profile rejected: {reason}.")

    if sum(profile["weights"].values()) != 100:
        fail("weights must total 100")
    limits = profile["thresholds"]
    if limits["hold_min_score"] >= limits["select_min_score"]:
        fail("hold_min_score must be below select_min_score")
    if limits["select_max_age_days"] >= limits["stale_after_days"]:
        fail("select_max_age_days must be below stale_after_days")
    if profile["history"]["similar_threshold"] >= profile["history"]["duplicate_threshold"]:
        fail("similar_threshold must be below duplicate_threshold")
    ids = [topic["topic_id"] for topic in profile["topics"]]
    if len(ids) != len(set(ids)):
        fail("topic IDs must be unique")


def load_profile(path, root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise NetworkError("invalid_editorial_profile", "Editorial profile must remain inside the project.")
    try:
        profile = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise NetworkError("invalid_editorial_profile", "Cannot read a valid editorial profile.") from None
    validate_profile(profile)
    return profile, _sha256(_canonical(profile))


# ---------------------------------------------------------------- history

def empty_history(profile):
    return {"contract": "story_history", "version": "1.0", "profile": profile["profile"], "entries": []}


def validate_history(history, profile):
    reject_secrets(history)
    _schema_check(history, HISTORY_SCHEMA, "invalid_story_history", "Story history")
    if history["profile"] != profile["profile"]:
        raise NetworkError("invalid_story_history", "Story history belongs to a different profile.")
    ids = [entry["selection_id"] for entry in history["entries"]]
    if len(ids) != len(set(ids)) or len(ids) > profile["history"]["max_entries"]:
        raise NetworkError("invalid_story_history", "Story history has duplicate or too many entries.")
    # Step 21: production reservations carry both fields; plain briefs carry neither.
    if any(("state" in entry) != ("production_id" in entry) for entry in history["entries"]):
        raise NetworkError("invalid_story_history", "Story history production fields must appear together.")
    owners = [entry["production_id"] for entry in history["entries"] if "production_id" in entry]
    if len(owners) != len(set(owners)):
        raise NetworkError("invalid_story_history", "A production may own only one history entry.")


def history_digest(history):
    return _sha256(_canonical(history))


def prune_history(history, profile, now):
    """Keep entries inside the window, newest first, at most max_entries."""
    cutoff = _parse(now) - timedelta(days=profile["history"]["window_days"])
    kept = [e for e in history["entries"] if _parse(e["selected_at"]) >= cutoff]
    kept.sort(key=lambda e: (e["selected_at"], e["selection_id"]), reverse=True)
    pruned = deepcopy(history)
    pruned["entries"] = kept[:profile["history"]["max_entries"]]
    return pruned


# ---------------------------------------------------------------- features

def _normalized(text):
    return " " + " ".join(re.findall(r"[a-z0-9]+", text.lower())) + " "


def _has_term(normalized, terms):
    return [term for term in terms if f" {term} " in normalized]


def _claim_fingerprint(text):
    return _sha256(" ".join(sorted(analyze(text)["content"])))


def story_features(record):
    """Deterministic fingerprints of a record's verified story (no article text kept)."""
    verified = [c for c in record["claims"] if c["status"] == "verified"]
    tokens, facts = set(analyze(record["candidate"]["title"])["content"]), set()
    for claim in verified:
        features = analyze(claim["text"])
        tokens |= features["content"]
        facts |= features["facts"]
    clean = sorted(t for t in tokens if len(t) <= 40)
    return {"claim_fingerprints": sorted({_claim_fingerprint(c["text"]) for c in verified}),
            "story_tokens": clean[:80], "fact_tokens": sorted(t for t in facts if len(t) <= 40)[:40]}


def _jaccard(left, right):
    left, right = set(left), set(right)
    return len(left & right) / len(left | right) if left | right else 0.0


def classify_novelty(record, features, history, profile, now):
    """('new'|'update'|'near_duplicate'|'duplicate', related selection_id or None, similarity)."""
    rules = profile["history"]
    cutoff = _parse(now) - timedelta(days=rules["window_days"])
    best = ("new", None, 0.0)
    for entry in history["entries"]:
        if _parse(entry["selected_at"]) < cutoff:
            continue
        similarity = round(_jaccard(features["story_tokens"], entry["story_tokens"]), 3)
        same_story = (entry["candidate_id"] == record["candidate"]["candidate_id"]
                      or entry["url_sha256"] == record["candidate"]["url_sha256"])
        new_claims = set(features["claim_fingerprints"]) - set(entry["claim_fingerprints"])
        new_facts = set(features["fact_tokens"]) - set(entry["fact_tokens"])
        # Step 19: a new verified claim carrying new dates/numbers is a genuine update even when
        # the wording is almost identical (e.g. an official release-date change).
        genuine_update = bool(new_claims and new_facts)
        if (same_story and not new_claims) or (features["claim_fingerprints"] and not new_claims) \
                or (similarity >= rules["duplicate_threshold"] and not genuine_update):
            return "duplicate", entry["selection_id"], max(similarity, 1.0 if same_story else similarity)
        if same_story or similarity >= rules["similar_threshold"]:
            status = "update" if genuine_update else "near_duplicate"
            rank = {"near_duplicate": 2, "update": 1, "new": 0}
            if (rank[status], similarity) > (rank[best[0]], best[2]):
                best = (status, entry["selection_id"], similarity)
    return best


# ---------------------------------------------------------------- evaluation

def evaluate_story(record, profile, history, now):
    """Score and gate one record. Pure: same inputs, same entry."""
    thresholds, weights = profile["thresholds"], profile["weights"]
    summary = record["summary"]
    claims = record["claims"]
    verified = [c for c in claims if c["status"] == "verified"]
    usable = [c for c in claims if c["status"] in ("verified", "corroborated")]
    primary = max((c["primary_support"] for c in verified), default=0)
    origins = max((c["independent_origins"] for c in usable), default=0)
    text = _normalized(" ".join([record["candidate"]["title"], *(c["text"] for c in usable)]))
    topics = sorted((t for t in profile["topics"] if _has_term(text, t["terms"])),
                    key=lambda t: (-t["priority"], t["topic_id"]))
    priority = topics[0]["priority"] if topics else 1
    core = _has_term(text, profile["relevance"]["core_terms"])
    related = _has_term(text, profile["relevance"]["related_terms"])
    rumor = bool(_has_term(text, profile["rumor_terms"]))

    now_moment = _parse(now)
    dated = record["candidate"]["published_at"] or record["candidate"]["retrieved_at"]
    age = (now_moment - _parse(dated)).total_seconds() / 86400
    stale_after = thresholds["stale_after_days"]
    components = {
        "confidence": (summary["verified"] + 0.5 * summary["corroborated"]) / len(claims),
        "authority": 1.0 if primary else (0.5 if usable else 0.0),
        "corroboration": min(1.0, origins / 3),
        "recency": 1.0 if age <= 1 else max(0.0, 1 - (age - 1) / (stale_after - 1)),
        "relevance": 1.0 if core else (0.5 if related else 0.0),
        "significance": priority / 5,
    }
    score = int(round(sum(weights[name] * components[name] for name in COMPONENTS)))
    features = story_features(record)
    novelty, related_id, similarity = classify_novelty(record, features, history, profile, now)

    reasons = []
    texts = [record["candidate"]["title"], *(c["text"] for c in claims)]
    if any(INSTRUCTION_TEXT.search(t) for t in texts):
        reasons.append("instruction_like_text")
    if not verified:
        reasons.append("corroborated_only" if summary["corroborated"] else "no_verified_claims")
    if summary["rejected"]:
        reasons.append("contradicted_claims")
    if summary["disputed"]:
        reasons.append("disputed_claims")
    if rumor and not primary:
        reasons.append("unconfirmed_rumor")
    if not core and not related:
        reasons.append("not_relevant")
    if age < -1:
        reasons.append("future_date")
    elif age > stale_after:
        reasons.append("stale")
    elif age > thresholds["select_max_age_days"]:
        reasons.append("not_recent")
    if record["candidate"]["published_at"] is None:
        reasons.append("publish_date_unknown")
    if novelty == "duplicate":
        reasons.append("duplicate_of_previous")
    elif novelty == "near_duplicate":
        reasons.append("near_duplicate_of_previous")
    if score < thresholds["hold_min_score"]:
        reasons.append("below_hold_score")
    elif score < thresholds["select_min_score"]:
        reasons.append("below_select_score")
    if verified and primary:
        reasons.append("primary_verified")
    if origins >= 2:
        reasons.append("independent_corroboration")
    if 0 <= age <= 2:
        reasons.append("recent")
    if priority >= 4:
        reasons.append("high_priority_topic")
    if any("older_official_contradiction_superseded" in c["rationale_codes"] for c in verified):
        reasons.append("supersedes_older_official")
    if novelty == "update":
        reasons.append("update_to_previous")
    elif novelty == "new":
        reasons.append("new_story")

    entry = {
        "rank": 1, "record_id": record["record_id"], "candidate_id": record["candidate"]["candidate_id"],
        "source_id": record["candidate"]["source_id"], "title": record["candidate"]["title"],
        "url": record["candidate"]["url"], "published_at": record["candidate"]["published_at"],
        "disposition": "select", "score": max(0, min(100, score)),
        "components": {name: round(components[name], 3) for name in COMPONENTS},
        "audience_interest": dict(UNAVAILABLE),
        "topics": [t["topic_id"] for t in topics],
        "novelty": {"status": novelty, "related_selection_id": related_id, "similarity": min(1.0, similarity)},
        "verification": {**{k: summary[k] for k in ("verified", "corroborated", "disputed", "insufficient_evidence", "rejected")},
                         "verified_claim_ids": [c["claim_id"] for c in verified],
                         "primary_support": primary, "independent_origins": origins,
                         "verified_at": record["verified_at"]},
        "reasons": reasons, "rationale": "",
    }
    _finalize(entry)
    return entry, features


def _finalize(entry):
    reasons = entry["reasons"]
    if any(r in REJECT_REASONS for r in reasons):
        entry["disposition"] = "reject"
    elif any(r in HOLD_REASONS for r in reasons):
        entry["disposition"] = "hold"
    else:
        entry["disposition"] = "select"
    ordered = [r for r in reasons if r in REJECT_REASONS] + [r for r in reasons if r in HOLD_REASONS] + \
              [r for r in reasons if r not in REJECT_REASONS and r not in HOLD_REASONS]
    entry["reasons"] = list(dict.fromkeys(ordered))
    entry["rationale"] = entry["disposition"].upper() + ": " + "; ".join(PHRASES[r] for r in entry["reasons"]) + "."


def _sort_key(entry):
    """Disposition, then score (high first), then newest publication, then record ID."""
    published = _parse(entry["published_at"]).timestamp() if entry["published_at"] else 0
    return (DISPOSITION_ORDER[entry["disposition"]], -entry["score"], -published, entry["record_id"])


def rank_stories(records, profile, profile_sha256, policy_sha256, history, now):
    """Rank validated Verification Records into a Selection Report (pure, deterministic)."""
    validate_history(history, profile)
    latest, superseded = {}, 0
    for record in records:
        if record["content_profile"] != profile["profile"]:
            raise NetworkError("profile_mismatch", "Records and editorial profile use different profiles.")
        key = record["candidate"]["candidate_id"]
        if key in latest:
            superseded += 1
            if (record["verified_at"], record["record_id"]) <= (latest[key]["verified_at"], latest[key]["record_id"]):
                continue
        latest[key] = record
    chosen = sorted(latest.values(), key=lambda r: (r["verified_at"], r["record_id"]), reverse=True)
    over_limit = max(0, len(chosen) - profile["limits"]["max_records"])
    chosen = chosen[:profile["limits"]["max_records"]]

    evaluated = [evaluate_story(record, profile, history, now) for record in chosen]
    evaluated.sort(key=lambda pair: _sort_key(pair[0]))
    picked, selections = [], 0
    for entry, features in evaluated:
        if entry["disposition"] != "select":
            continue
        for _, other in picked:
            if features["claim_fingerprints"] and set(features["claim_fingerprints"]) <= set(other["claim_fingerprints"]):
                entry["reasons"].insert(0, "duplicate_in_batch")
                break
            if _jaccard(features["story_tokens"], other["story_tokens"]) >= profile["history"]["similar_threshold"]:
                entry["reasons"].insert(0, "similar_to_higher_ranked")
                break
        else:
            if selections >= profile["thresholds"]["max_selections_per_run"]:
                entry["reasons"].insert(0, "selection_limit")
            else:
                selections += 1
                picked.append((entry, features))
        _finalize(entry)
    entries = sorted((entry for entry, _ in evaluated), key=_sort_key)
    for rank, entry in enumerate(entries, 1):
        entry["rank"] = rank
    history_sha = history_digest(history)
    run_id = "sel-" + _sha256("\n".join([profile_sha256, policy_sha256, history_sha, now,
                                         *sorted(e["record_id"] for e in entries)]))[:24]
    report = {
        "contract": "selection_report", "version": "1.0", "selection_run_id": run_id,
        "profile": profile["profile"], "editorial_profile_sha256": profile_sha256,
        "policy_sha256": policy_sha256, "history_sha256": history_sha, "evaluated_at": now,
        "audience_signals": dict(UNAVAILABLE), "entries": entries,
        "summary": {d: sum(e["disposition"] == d for e in entries) for d in ("select", "hold", "reject")},
        "skipped": {"superseded_records": superseded, "invalid_records": 0, "over_limit": over_limit},
    }
    validate_report(report)
    return report


def validate_report(report):
    reject_secrets(report)
    _schema_check(report, REPORT_SCHEMA, "invalid_selection_report", "Selection Report")
    entries = report["entries"]
    if [e["rank"] for e in entries] != list(range(1, len(entries) + 1)):
        raise NetworkError("invalid_selection_report", "Selection Report ranks are not consecutive.")
    summary = {d: sum(e["disposition"] == d for e in entries) for d in ("select", "hold", "reject")}
    if summary != report["summary"]:
        raise NetworkError("invalid_selection_report", "Selection Report summary does not match its entries.")


# ---------------------------------------------------------------- handoff

def select_brief(record, report, *, profile, profile_sha256, policy, policy_sha256, history, now, brief_id=None):
    """Re-check a selected story against current history, then build its Story Brief.

    Returns (brief, history_entry). The report is only an index: the record is re-scored
    now, against the current history, so a stale or edited report cannot force a story
    through and the same story cannot be made twice.
    """
    validate_report(report)
    if report["editorial_profile_sha256"] != profile_sha256 or report["policy_sha256"] != policy_sha256:
        raise NetworkError("configuration_mismatch", "Profile or policy changed since this report; select again.")
    age = _parse(now) - _parse(report["evaluated_at"])
    if age > REPORT_MAX_AGE or age < timedelta(minutes=-5):
        raise NetworkError("stale_selection", "This Selection Report is too old; run select-stories again.")
    listed = next((e for e in report["entries"] if e["record_id"] == record["record_id"]), None)
    if listed is None or listed["disposition"] != "select":
        raise NetworkError("not_selected", "This record is not a select entry in the report.")
    entry, features = evaluate_story(record, profile, history, now)
    if entry["disposition"] != "select":
        raise NetworkError("selection_changed", "The story no longer qualifies (for example, it was already made).")
    topics = {t["topic_id"]: t for t in profile["topics"]}
    angle = topics[entry["topics"][0]]["angle"] if entry["topics"] else None
    brief = build_verified_brief([record], policy, policy_sha256, topic=record["candidate"]["title"],
                                 angle=angle, brief_id=brief_id, clock=lambda: now)
    selection_id = "pick-" + _sha256(f"{report['selection_run_id']}\n{record['record_id']}")[:24]
    brief["editorial"] = {"selection_id": selection_id, "selection_run_id": report["selection_run_id"],
                          "record_id": record["record_id"], "editorial_profile_sha256": profile_sha256,
                          "score": entry["score"], "topics": entry["topics"],
                          "novelty": entry["novelty"]["status"], "reasons": entry["reasons"]}
    validate_story_brief(brief)
    history_entry = {"selection_id": selection_id, "record_id": record["record_id"],
                     "candidate_id": record["candidate"]["candidate_id"],
                     "url_sha256": record["candidate"]["url_sha256"], "brief_id": brief["brief_id"],
                     "topics": entry["topics"], **features, "selected_at": now}
    return brief, history_entry
