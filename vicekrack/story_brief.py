"""Story Brief contract: the Creator stage's only input (Step 15).

A brief names a topic and angle and lists the sources and the *only* claims a script may
state. Future Scout/Verification stages will produce briefs; today they are written by
hand. Validation only: no provider clients, network access, or persistence. Error
messages name a field location and a fixed reason, never brief content.
"""

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import NetworkError
from .persistence import reject_secrets
from .short_script import FORMATS, _check_optional_text, _check_timestamp, _check_url


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas/story-brief.schema.json"


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _fail(location, reason):
    raise NetworkError("invalid_story_brief", f"Story Brief validation failed at {location}: {reason}.")


def _relabel(check, *args):
    """Reuse Short Script field checks while reporting the Story Brief error code."""
    try:
        check(*args)
    except NetworkError as error:
        raise NetworkError("invalid_story_brief",
                           error.message.replace("Short Script", "Story Brief")) from None


def validate_story_brief(brief, *, require_verified_claims=False):
    """Raise NetworkError unless brief is a valid Story Brief; never modifies brief."""
    try:
        json.dumps(brief, allow_nan=False)
    except (TypeError, ValueError):
        _fail("$", "content must be finite JSON values")
    # Reject credentials before any diagnostic is produced.
    reject_secrets(brief)

    error = next(_validator().iter_errors(brief), None)
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        _fail(location, f"schema rule '{error.validator}'")
    if brief["format"] not in FORMATS:
        _fail("format", "is not a supported format")

    _relabel(_check_optional_text, brief["constraints"]["tone"], "constraints.tone")
    _relabel(_check_optional_text, brief["provenance"]["model"], "provenance.model")
    _relabel(_check_timestamp, brief["provenance"]["created_at"], "provenance.created_at")

    source_ids = set()
    for index, source in enumerate(brief["sources"]):
        where = f"sources[{index}]"
        if source["source_id"] in source_ids:
            _fail(f"{where}.source_id", "duplicate source_id")
        source_ids.add(source["source_id"])
        if "url" in source:
            _relabel(_check_url, source["url"], f"{where}.url")
        if "accessed_at" in source:
            _relabel(_check_timestamp, source["accessed_at"], f"{where}.accessed_at")

    claim_ids = set()
    for index, claim in enumerate(brief["claims"]):
        where = f"claims[{index}]"
        if claim["claim_id"] in claim_ids:
            _fail(f"{where}.claim_id", "duplicate claim_id")
        claim_ids.add(claim["claim_id"])
        if any(source_id not in source_ids for source_id in claim["source_ids"]):
            _fail(f"{where}.source_ids", "references an unknown source")
        if claim["status"] == "verified" and not claim["source_ids"]:
            _fail(f"{where}.source_ids", "a verified claim must cite at least one source")

    if "verification" in brief:
        _check_verification_links(brief)

    if require_verified_claims:
        unverified = count_unverified(brief)
        if unverified:
            raise NetworkError("unverified_claims",
                               f"{unverified} claim(s) are not verified; production requires verified claims.")


def _check_verification_links(brief):
    """Step 17: a brief built from Verification Records must agree with them.

    Every claim maps to exactly one record claim from a listed record. A claim marked
    verified must come from a `verified` record claim; a `corroborated` record claim may
    only appear as unverified (draft-only). This checks internal consistency; the
    records themselves are re-validated by the verified-brief builder.
    """
    links = brief["verification"]
    records = set(links["record_ids"])
    mapped = {}
    for index, link in enumerate(links["claims"]):
        where = f"verification.claims[{index}]"
        if link["claim_id"] in mapped:
            _fail(f"{where}.claim_id", "duplicate claim link")
        if link["record_id"] not in records:
            _fail(f"{where}.record_id", "references an unlisted record")
        mapped[link["claim_id"]] = link
    if set(mapped) != {claim["claim_id"] for claim in brief["claims"]}:
        _fail("verification.claims", "must link every claim exactly once")
    for index, claim in enumerate(brief["claims"]):
        expected = "verified" if mapped[claim["claim_id"]]["verification_status"] == "verified" else "unverified"
        if claim["status"] != expected:
            _fail(f"claims[{index}].status", "does not match its verification record")


def count_unverified(document):
    """Number of claims not declared verified (works for briefs and scripts)."""
    return sum(claim["status"] != "verified" for claim in document["claims"])
