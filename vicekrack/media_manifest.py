"""Step 42 local media manifest: explicit files, hashes, and declared-not-verified rights.

No URL fetching. Paths stay inside a caller-supplied media root.
"""
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .artifact_binding import sha256_bytes
from .errors import NetworkError
from .orchestrator import ROOT

MAX_ASSET_BYTES = 80 * 1024 * 1024
_VALIDATOR = None


def validator():
    global _VALIDATOR
    if _VALIDATOR is None:
        schema = json.loads((ROOT / "schemas/media-manifest.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        _VALIDATOR = Draft202012Validator(schema)
    return _VALIDATOR


def validate_media_manifest(document):
    if not isinstance(document, dict) or next(validator().iter_errors(document), None) is not None:
        raise NetworkError("invalid_media_manifest", "The media manifest does not match its contract.")
    ids = [asset["asset_id"] for asset in document["assets"]]
    if len(set(ids)) != len(ids):
        raise NetworkError("invalid_media_manifest", "Asset ids must be unique.")
    indexes = [row["scene_index"] for row in document["assignments"]]
    if indexes != [1, 2, 3, 4]:
        raise NetworkError("invalid_media_manifest", "Assignments must cover scenes 1 through 4 in order.")
    known = set(ids)
    for row in document["assignments"]:
        if row["asset_id"] not in known:
            raise NetworkError("invalid_media_manifest", "An assignment names an unknown asset.")
        asset = next(item for item in document["assets"] if item["asset_id"] == row["asset_id"])
        if asset["kind"] == "video" and "trim" not in row:
            raise NetworkError("invalid_media_manifest", "A video assignment needs an explicit trim range.")
        if asset["kind"] == "image" and "image_duration_seconds" not in row:
            raise NetworkError("invalid_media_manifest", "An image assignment needs an explicit duration.")
        if asset["kind"] == "video" and row["trim"]["end_seconds"] <= row["trim"]["start_seconds"]:
            raise NetworkError("invalid_media_manifest", "A trim range must be positive.")
        if "://" in asset["relative_path"] or asset["relative_path"].startswith(("/", "\\")):
            raise NetworkError("invalid_media_manifest", "Asset paths must be relative local files, not URLs.")
    if document["mute_source_audio"] is not True:
        raise NetworkError("invalid_media_manifest", "Source-clip audio must stay muted.")
    return document


def confine(root, relative):
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base) or candidate == base:
        raise NetworkError("media_path_rejected", "An asset path escaped the supplied media root.")
    return candidate


def load_asset(root, asset):
    path = confine(root, asset["relative_path"])
    if not path.is_file():
        raise NetworkError("media_missing", "A declared media file is missing.")
    try:
        data = path.read_bytes()
    except OSError:
        raise NetworkError("media_unreadable", "A declared media file could not be read.") from None
    if not data or len(data) > MAX_ASSET_BYTES or len(data) != asset["bytes"]:
        raise NetworkError("media_size_mismatch", "A declared media file has an unexpected size.")
    if sha256_bytes(data) != asset["sha256"]:
        raise NetworkError("media_hash_mismatch", "A declared media file changed since the manifest was written.")
    kind = asset["kind"]
    if kind == "image" and not (data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8")):
        raise NetworkError("media_unsupported", "Image assets must be PNG or JPEG.")
    if kind == "video" and b"ftyp" not in data[:64]:
        raise NetworkError("media_unsupported", "Video assets must be MP4.")
    return path, data
