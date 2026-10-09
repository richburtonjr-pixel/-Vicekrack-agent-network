"""Verifiable content artifacts (Step 36): one implementation shared by the renderer, the
production pipeline, the quality report and the Living HQ.

Preview manifest versions (`preview_render`, schemas/preview-manifest.schema.json):
  1.0  legacy (Steps 13-35): the video is hashed; scene posters are listed by name only.
       It stays readable as-is: its posters are `not_hash_bound` and are never upgraded.
  1.1  every scene poster has `poster_sha256` and `poster_bytes`, and the video has
       `video_bytes`. The renderer checks every file, path, size and hash before the
       preview is published.

Quality report binding (`production_quality_report` 1.1, `binding`): the exact bytes the
checks read, recorded as role + safe reference + SHA-256 + size: production state, brief,
script, scene plan, preview manifest, video, each poster, the verification record and the
narration input (hash only: its path is the user's and is never stored). Configuration
files are recorded by their project-relative path and hash. Every file is read once into a
snapshot; the checks use those bytes (media tools get the path, and the file is re-hashed
after inspection). Anything that changed or disappeared while the checks ran makes the
binding `changed_during_inspection`, and it is never reported as bound.

No circularity: manifests never refer to reports, and a report never hashes itself or any
other report (runtime/quality is not part of a production snapshot).

Binding is byte identity only. Matching hashes never mean the facts are true, the evidence
is fresh, rights are cleared or publishing is allowed.
"""

import hashlib
import json
import os
import re
import stat
from functools import lru_cache
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator

from .errors import NetworkError

ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_POSTER_BYTES = 8 * 1024 * 1024
MIN_VIDEO_BYTES = 100
PNG = b"\x89PNG\r\n\x1a\n"
BINDING_VERSION = "1.0"
ROLES = ("production_state", "brief", "script", "scene_plan", "preview_manifest", "media_manifest", "video", "poster",
         "verification_record", "narration")
CONFIG_NAMES = ("verification_policy", "editorial_profile", "creator", "capabilities")
STATE_CONFIG = {"verification_policy": "policy", "editorial_profile": "editorial_profile", "creator": "creator",
                "capabilities": "capabilities"}
BINDING_NOTE = ("Binding proves byte identity only: these exact files were inspected. It does not verify facts, evidence "
                "freshness, rights or permission to publish.")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- safe paths
def norm(relative):
    """Saved relative paths use the separator of the computer that saved them (Windows writes
    backslashes); both are treated as separators, and every part is then checked."""
    return relative.replace("\\", "/") if isinstance(relative, str) else relative


def safe_file(folder, relative, max_bytes):
    """(path, None) for a regular file inside `folder` reached without any symbolic link, else (None, code)."""
    relative = norm(relative)
    if not isinstance(relative, str) or not relative or len(relative) > 300 or "\x00" in relative or ":" in relative:
        return None, "path_invalid"
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or any(p in ("..", ".", "") for p in parts):
        return None, "path_invalid"
    current = Path(folder)
    try:
        if stat.S_ISLNK(os.lstat(current).st_mode):
            return None, "path_invalid"
        mode = None
        for part in parts:
            current = current / part
            mode = os.lstat(current).st_mode
            if stat.S_ISLNK(mode):
                return None, "path_invalid"
        if mode is None or not stat.S_ISREG(mode):
            return None, "path_invalid"
        if not current.resolve().is_relative_to(Path(folder).resolve()):
            return None, "path_invalid"
        if os.lstat(current).st_size > max_bytes:
            return None, "file_too_large"
    except OSError:
        return None, "missing"
    return current, None


# ---------------------------------------------------------------- preview manifests
@lru_cache(maxsize=1)
def _manifest_validator():
    schema = json.loads((ROOT / "schemas/preview-manifest.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_manifest(manifest):
    """Schema check for preview manifests 1.0 (legacy) and 1.1 (poster hashes). Raises invalid_preview_manifest."""
    if not isinstance(manifest, dict) or next(_manifest_validator().iter_errors(manifest), None) is not None:
        raise NetworkError("invalid_preview_manifest", "The preview manifest does not match its contract.")
    if manifest["version"] == "1.1":
        names = [s["poster"] for s in manifest["scenes"]]
        if len(set(names)) != len(names) or [s["index"] for s in manifest["scenes"]] != list(range(1, len(names) + 1)):
            raise NetworkError("invalid_preview_manifest", "Preview posters must be unique and in scene order.")
    return manifest


def posters_bound(manifest):
    return isinstance(manifest, dict) and manifest.get("version") == "1.1"


def check_package(folder, manifest):
    """Before publication (and on resume): every file a 1.1 manifest names exists inside `folder`,
    has the recorded size and SHA-256, and posters are PNG files. Returns None or a reason code."""
    try:
        validate_manifest(manifest)
    except NetworkError:
        return "manifest_invalid"
    if not posters_bound(manifest):
        return "manifest_not_bound"
    video, problem = safe_file(folder, manifest["video"], MAX_VIDEO_BYTES)
    if video is None:
        return "video_" + problem
    data = video.read_bytes()
    if len(data) < MIN_VIDEO_BYTES or len(data) != manifest["video_bytes"] or sha256_bytes(data) != manifest["video_sha256"]:
        return "video_hash_mismatch"
    for scene in manifest["scenes"]:
        poster, problem = safe_file(folder, scene["poster"], MAX_POSTER_BYTES)
        if poster is None:
            return "poster_" + problem
        data = poster.read_bytes()
        if not data.startswith(PNG):
            return "poster_not_png"
        if len(data) != scene["poster_bytes"] or sha256_bytes(data) != scene["poster_sha256"]:
            return "poster_hash_mismatch"
    return None


# ---------------------------------------------------------------- snapshots (quality report side)
class Snapshot:
    """Every file a quality check relies on, read once. `recheck()` re-hashes them afterwards."""

    def __init__(self):
        self.entries, self.order = {}, []

    def read(self, key, path, *, role, ref=None, ident=None, index=None, max_bytes=MAX_JSON_BYTES, keep=False):
        """Bytes (if keep) of `path`, read once per key; None if it could not be read."""
        if key in self.entries:
            entry = self.entries[key]
            if keep and "data" not in entry and entry["status"] == "read":
                raise RuntimeError("snapshot entry was read without keeping its bytes")   # programming error
            return entry.get("data") if keep else entry["sha256"]
        entry = {"role": role, "ref": ref, "id": ident, "index": index, "sha256": None, "bytes": None,
                 "status": "read", "path": path, "max": max_bytes}
        try:
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                entry["status"] = "unreadable"
            elif info.st_size > max_bytes:
                entry["status"] = "too_large"
            else:
                data = Path(path).read_bytes()
                entry.update(sha256=sha256_bytes(data), bytes=len(data))
                if keep:
                    entry["data"] = data
        except FileNotFoundError:
            entry["status"] = "missing"
        except OSError:
            entry["status"] = "unreadable"
        self.entries[key] = entry
        self.order.append(key)
        return entry.get("data") if keep else entry["sha256"]

    def recheck(self):
        """Keys whose file changed (or appeared/disappeared) since it was read."""
        changed = []
        for key in self.order:
            entry = self.entries[key]
            try:
                info = os.lstat(entry["path"])
                now = (None if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > entry["max"]
                       else sha256_file(entry["path"]))
                present = True
            except OSError:
                now, present = None, False
            if (entry["status"] == "read") != (present and now is not None) or (now is not None and now != entry["sha256"]):
                changed.append(key)
        return changed

    def artifacts(self):
        rows = []
        for key in self.order:
            entry = self.entries[key]
            if entry["role"] not in ROLES:
                continue
            rows.append({k: entry[k] for k in ("role", "ref", "id", "index", "sha256", "bytes", "status")})
        return rows

    def configuration(self):
        rows = []
        for key in self.order:
            entry = self.entries[key]
            if entry["role"] in CONFIG_NAMES:
                rows.append({"name": entry["role"], "path": entry["ref"], "sha256": entry["sha256"], "status": entry["status"]})
        return rows


# ---------------------------------------------------------------- re-validating a saved binding
def current_hash(row, *, folder, state, root, project):
    """Current SHA-256 of what a binding row describes, read-only; None if it cannot be read safely."""
    role = row["role"]
    if role == "narration":
        narration = state["config"]["narration"]
        return narration["sha256"] if narration else None       # the user's file is never re-read here
    if role == "verification_record":
        records = Path(root if root is not None else ROOT) / "runtime/verification/records"
        path, _ = safe_file(records, f"{row['id']}.json", MAX_JSON_BYTES) if isinstance(row.get("id"), str) \
            and re.fullmatch(r"ver-[0-9a-f]{24}", row["id"]) else (None, None)
    else:
        limit = MAX_VIDEO_BYTES if role == "video" else MAX_POSTER_BYTES if role == "poster" else MAX_JSON_BYTES
        ref = "state.json" if role == "production_state" else row.get("ref")
        path, _ = safe_file(folder, ref, limit)
    return sha256_file(path) if path is not None else None


def verify_binding(report, state, folder, root=None, project=ROOT):
    """Artifact binding of a saved quality report against the files as they are now.

    status: matching | changed | legacy_unverified | unavailable, plus reasons and the roles
    that differ. Read-only: nothing is checked again, nothing is written."""
    if report.get("version") != "1.1" or "binding" not in report:
        return {"status": "legacy_unverified", "reasons": ["report_records_no_artifact_hashes"], "changed": []}
    binding = report["binding"]
    if binding["status"] != "bound":
        return {"status": "unavailable", "reasons": ["not_bound_" + binding["status"]], "changed": []}
    if report["production_id"] != state["production_id"]:
        return {"status": "unavailable", "reasons": ["report_for_another_production"], "changed": []}
    changed = []
    stages = {s["name"]: s["artifacts"] for s in state["stages"]}
    expected_refs = {"brief": ("brief", "brief_path"), "script": ("creator", "script_path"),
                     "scene_plan": ("plan", "plan_path"), "preview_manifest": ("preview", "manifest_file"),
                     "video": ("preview", "preview_file"), "media_manifest": ("preview", "media_path")}
    for row in binding["artifacts"]:
        label = row["role"] if row["index"] is None else f"{row['role']}_{row['index']}"
        if row["role"] in expected_refs:
            stage, key = expected_refs[row["role"]]
            if norm(stages.get(stage, {}).get(key)) != row["ref"]:
                changed.append(label)                            # the production now names a different file
                continue
        try:
            now = current_hash(row, folder=folder, state=state, root=root, project=project)
        except OSError:
            now = None
        if now is None or now != row["sha256"]:
            changed.append(label)
    for row in binding["configuration"]:
        path, _ = safe_file(project, row["path"], MAX_JSON_BYTES)
        try:
            now = sha256_file(path) if path is not None else None
        except OSError:
            now = None
        if now is None or now != row["sha256"]:
            changed.append("config_" + row["name"])
    if changed:
        return {"status": "changed", "reasons": ["artifacts_changed_since_check"], "changed": changed[:40]}
    return {"status": "matching", "reasons": [], "changed": []}
