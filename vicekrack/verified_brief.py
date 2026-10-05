"""Verified Story Brief handoff (Step 17): Verification Records -> Story Brief.

Only `verified` record claims become `verified` brief claims. With include_corroborated,
`corroborated` claims are added as `unverified` (draft-only: the existing production gate
blocks them). Disputed, rejected and insufficient claims never enter the brief. Every
record is re-validated by decision replay against the current policy and must be recent.
Each brief claim cites only the sources whose evidence supports it, and the brief's
optional `verification` block links every claim to its record so later stages (and the
Story Brief validator) can check that no status was upgraded.
"""

import hashlib
import re
from datetime import datetime, timedelta, timezone

from .errors import NetworkError
from .story_brief import validate_story_brief
from .verification import utc_now, validate_record

DEFAULT_ANGLE = "What has been confirmed so far, and where it comes from."
MAX_BRIEF_CLAIMS = 8


def _parse(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_verified_brief(records, policy, policy_sha256, *, topic=None, angle=None, brief_id=None,
                         include_corroborated=False, clock=utc_now):
    """Return a validated Story Brief built only from sufficiently supported claims."""
    if not records:
        raise NetworkError("no_verification_records", "Select at least one Verification Record.")
    if len(records) > MAX_BRIEF_CLAIMS:
        raise NetworkError("brief_too_large", "Select at most 8 Verification Records for one brief.")
    now = _parse(clock())
    max_age = timedelta(days=policy["rules"]["max_record_age_days"])
    seen_records = set()
    for record in records:
        validate_record(record, policy, policy_sha256)
        if record["record_id"] in seen_records:
            raise NetworkError("duplicate_record", "The same Verification Record was selected twice.")
        seen_records.add(record["record_id"])
        age = now - _parse(record["verified_at"])
        if age > max_age or age < timedelta(minutes=-5):
            raise NetworkError("stale_verification", "A Verification Record is too old (or from the future); verify again.")

    accepted = {"verified"} | ({"corroborated"} if include_corroborated else set())
    sources, claims, links, texts = {}, [], [], set()
    for record in records:
        for claim in record["claims"]:
            if claim["status"] not in accepted:
                continue
            key = " ".join(re.findall(r"[a-z0-9]+", claim["text"].lower()))
            if key in texts:
                continue  # The same claim confirmed via two candidates appears once.
            texts.add(key)
            if claim["status"] == "verified":
                backing = [e for e in claim["evidence"]
                           if e["relation"] == "supports" and e["tier"] == "primary" and e["first_hand"]]
            else:
                backing = [e for e in claim["evidence"]
                           if e["relation"] == "supports" and e["tier"] in ("primary", "secondary")]
            # Step 19: never cite a statement that a newer official statement superseded.
            current = [e for e in backing if "superseded_by" not in e]
            backing = current or backing
            source_ids = []
            for item in backing:
                source_id = "src-" + item["candidate_id"][5:17]
                if source_id not in sources:
                    sources[source_id] = {
                        "source_id": source_id, "title": item["title"], "publisher": item["publisher"],
                        "kind": "official" if item["tier"] == "primary" else "press",
                        "url": item["url"], "accessed_at": item["retrieved_at"]}
                if source_id not in source_ids:
                    source_ids.append(source_id)
            claim_id = f"c{len(claims) + 1}"
            claims.append({"claim_id": claim_id, "text": claim["text"],
                           "status": "verified" if claim["status"] == "verified" else "unverified",
                           "source_ids": source_ids[:10]})
            links.append({"claim_id": claim_id, "record_id": record["record_id"],
                          "record_claim_id": claim["claim_id"], "verification_status": claim["status"]})
    if not claims:
        raise NetworkError("no_verified_claims", "None of the selected records has a claim eligible for a brief.")
    if len(claims) > MAX_BRIEF_CLAIMS:
        raise NetworkError("brief_too_large", "A Story Brief may contain at most 8 claims; select fewer records.")

    record_ids = [record["record_id"] for record in records]
    digest = hashlib.sha256(("\n".join(sorted(record_ids)) + f"\n{include_corroborated}").encode()).hexdigest()
    first = records[0]["candidate"]
    brief = {
        "contract": "story_brief", "version": "1.0",
        "brief_id": brief_id or f"verified-{digest[:16]}",
        "content_profile": records[0]["content_profile"],
        "format": "vertical_short_15s", "language": "en",
        "topic": (topic or first["title"])[:200], "angle": angle or DEFAULT_ANGLE,
        "sources": sorted(sources.values(), key=lambda s: s["source_id"]),
        "claims": claims,
        "constraints": {"avoid": list(policy["brief_defaults"]["avoid"]),
                        "disclosures": list(policy["brief_defaults"]["disclosures"]), "tone": None},
        "provenance": {"created_by": "verifier", "provider": "local", "model": None, "created_at": clock()},
        "verification": {"policy_sha256": policy_sha256, "record_ids": record_ids, "claims": links},
    }
    if any(record["content_profile"] != brief["content_profile"] for record in records):
        raise NetworkError("profile_mismatch", "All selected records must use the same profile.")
    validate_story_brief(brief)
    return brief
