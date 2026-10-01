"""Subject-neutral Short Script contract for short vertical videos.

Validation only: no provider clients, network access, rendering, or persistence.
Error messages name a field location and a fixed reason, never script content.
"""

import json
import math
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .errors import NetworkError
from .orchestrator import timestamp
from .persistence import reject_secrets


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas/short-script.schema.json"

# Each format fixes its duration, aspect ratio, and ordered beat windows (seconds).
FORMATS = {
    "vertical_short_15s": {
        "duration_seconds": 15,
        "aspect_ratio": "9:16",
        "beats": (("hook", 0, 3), ("context", 3, 7), ("key_info", 7, 12), ("payoff", 12, 15)),
    },
}

VISUAL_METHODS = ("ai_video_clip", "sourced_media", "generated_image",
                  "animated_image", "motion_graphics", "text_card")
# Methods that can be produced locally without any external service.
LOCAL_METHODS = frozenset({"motion_graphics", "text_card"})
# Methods that need a generation prompt.
GENERATIVE_METHODS = frozenset({"ai_video_clip", "generated_image"})
MAX_WORDS_PER_SECOND = 3.5


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _fail(location, reason):
    raise NetworkError("invalid_short_script", f"Short Script validation failed at {location}: {reason}.")


def max_words(start_seconds, end_seconds):
    """Narration word budget for a beat window."""
    return math.floor((end_seconds - start_seconds) * MAX_WORDS_PER_SECOND + 1e-9)


def _check_timestamp(value, location):
    try:
        timestamp(value)
    except (ValueError, TypeError):
        _fail(location, "must be a real UTC date-time")


def _check_url(value, location):
    if any(c.isspace() or not c.isprintable() for c in value):
        _fail(location, "must be an https URL without spaces")
    try:
        parts = urlsplit(value)
    except ValueError:
        _fail(location, "must be an https URL")
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        _fail(location, "must be an https URL with a host and no embedded credentials")


def _check_optional_text(value, location):
    if value is not None and not value.strip():
        _fail(location, "must be null or nonblank text")


def validate_short_script(script, *, require_verified_claims=False):
    """Raise NetworkError unless script is a valid Short Script; never modifies script."""
    try:
        json.dumps(script, allow_nan=False)
    except (TypeError, ValueError):
        _fail("$", "content must be finite JSON values")
    # Reject credentials before any diagnostic is produced.
    reject_secrets(script)

    error = next(_validator().iter_errors(script), None)
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        _fail(location, f"schema rule '{error.validator}'")

    template = FORMATS[script["format"]]
    if script["duration_seconds"] != template["duration_seconds"]:
        _fail("duration_seconds", "does not match the format")
    if script["aspect_ratio"] != template["aspect_ratio"]:
        _fail("aspect_ratio", "does not match the format")

    beats = script["beats"]
    if len(beats) != len(template["beats"]):
        _fail("beats", f"the format requires exactly {len(template['beats'])} beats")
    for index, (beat, (name, start, end)) in enumerate(zip(beats, template["beats"])):
        where = f"beats[{index}]"
        if beat["beat"] != name:
            _fail(f"{where}.beat", f"expected '{name}' at this position")
        if beat["start_seconds"] != start or beat["end_seconds"] != end:
            _fail(where, f"timing must be {start}-{end} seconds")
        if len(beat["narration"].split()) > max_words(start, end):
            _fail(f"{where}.narration", f"exceeds {max_words(start, end)} words for this beat")
        _check_optional_text(beat["on_screen_text"], f"{where}.on_screen_text")
        _check_optional_text(beat["sound_cue"], f"{where}.sound_cue")

    _check_optional_text(script["audio"]["music_mood"], "audio.music_mood")
    _check_optional_text(script["provenance"]["model"], "provenance.model")
    _check_timestamp(script["provenance"]["created_at"], "provenance.created_at")

    source_ids = set()
    for index, source in enumerate(script["sources"]):
        where = f"sources[{index}]"
        if source["source_id"] in source_ids:
            _fail(f"{where}.source_id", "duplicate source_id")
        source_ids.add(source["source_id"])
        if "url" in source:
            _check_url(source["url"], f"{where}.url")
        if "accessed_at" in source:
            _check_timestamp(source["accessed_at"], f"{where}.accessed_at")

    claims = {}
    for index, claim in enumerate(script["claims"]):
        where = f"claims[{index}]"
        if claim["claim_id"] in claims:
            _fail(f"{where}.claim_id", "duplicate claim_id")
        claims[claim["claim_id"]] = claim
        if any(source_id not in source_ids for source_id in claim["source_ids"]):
            _fail(f"{where}.source_ids", "references an unknown source")
        if claim["status"] == "verified" and not claim["source_ids"]:
            _fail(f"{where}.source_ids", "a verified claim must cite at least one source")

    used_claims = set()
    for index, beat in enumerate(beats):
        where = f"beats[{index}]"
        if any(claim_id not in claims for claim_id in beat["claim_ids"]):
            _fail(f"{where}.claim_ids", "references an unknown claim")
        used_claims.update(beat["claim_ids"])

        visual = beat["visual"]
        preferred, fallbacks = visual["preferred_method"], visual["fallback_methods"]
        if preferred in fallbacks:
            _fail(f"{where}.visual.fallback_methods", "must not repeat the preferred method")
        if fallbacks[-1] not in LOCAL_METHODS:
            _fail(f"{where}.visual.fallback_methods", "the final fallback must be motion_graphics or text_card")
        methods = {preferred, *fallbacks}
        prompt = visual["generation_prompt"]
        has_prompt = prompt is not None and bool(prompt.strip())
        if prompt is not None and not has_prompt:
            _fail(f"{where}.visual.generation_prompt", "must be null or nonblank text")
        if methods & GENERATIVE_METHODS and not has_prompt:
            _fail(f"{where}.visual.generation_prompt", "required by a generative method")
        if any(source_id not in source_ids for source_id in visual["source_ids"]):
            _fail(f"{where}.visual.source_ids", "references an unknown source")
        if "sourced_media" in methods and not visual["source_ids"]:
            _fail(f"{where}.visual.source_ids", "sourced_media requires at least one source")
        if "animated_image" in methods and not (has_prompt or visual["source_ids"]):
            _fail(f"{where}.visual", "animated_image requires a generation prompt or a source")

    for index, claim in enumerate(script["claims"]):
        if claim["claim_id"] not in used_claims:
            _fail(f"claims[{index}]", "claim is not used by any beat")
    if not any(beat["beat"] == "key_info" and beat["claim_ids"] for beat in beats):
        _fail("beats", "the key_info beat must reference at least one claim")

    if require_verified_claims:
        unverified = sum(claim["status"] != "verified" for claim in script["claims"])
        if unverified:
            raise NetworkError("unverified_claims",
                               f"{unverified} claim(s) are not verified; production requires verified claims.")
