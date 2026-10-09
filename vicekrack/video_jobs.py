"""Step 43 Grok video jobs. One explicit request, no automatic resubmit.

Official flow (docs.x.ai, October 2026): POST https://api.x.ai/v1/videos/generations,
then GET https://api.x.ai/v1/videos/{request_id}. Completed clips are temporary HTTPS
URLs. This adapter does not poll in a loop and does not spend credits unless submit
is called with consent and --allow-network.
"""
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from .artifact_binding import sha256_bytes
from .errors import NetworkError
from .orchestrator import ROOT
from .scene_plan import validate_scene_plan
from .persistence import reject_secrets
from .events.store import _Lock
from .video_transport import transport as http_transport, check_download_url

GENERATE_URL = "https://api.x.ai/v1/videos/generations"
STATUS_URL = "https://api.x.ai/v1/videos/"
DOWNLOAD_HOSTS = {"vidgen.x.ai"}
DEFAULT_MODEL = "grok-imagine-video-1.5"
MAX_DOWNLOAD = 80 * 1024 * 1024


def locked(function):
    @wraps(function)
    def call(*args, **kwargs):
        lock = _Lock(jobs_dir() / "operation.lock")
        if not lock.acquire(create=True):
            raise NetworkError("video_busy", "Another video operation is running.")
        try:
            return function(*args, **kwargs)
        finally:
            lock.release()
    return call


def jobs_dir():
    path = ROOT / "runtime" / "video-jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


@locked
def prepare(plan, scene_index, *, model=DEFAULT_MODEL, audio_mode="none", resolution="720p"):
    validate_scene_plan(plan)
    if resolution not in {"480p", "720p", "1080p"} or type(scene_index) is not int:
        raise NetworkError("invalid_video_job", "Invalid resolution or scene index.")
    if audio_mode not in {"none", "generated"}:
        raise NetworkError("invalid_video_job", "Audio mode must be none or generated.")
    if model not in {"grok-imagine-video-1.5", "grok-imagine-video-1.5-lite"}:
        raise NetworkError("invalid_video_job", "Model is not an allowed Grok video model.")
    scene = next((item for item in plan["scenes"] if item["index"] == scene_index), None)
    if scene is None:
        raise NetworkError("invalid_video_job", "Scene index is not in the plan.")
    duration = int(scene["beat"]["end_seconds"] - scene["beat"]["start_seconds"])
    if duration < 1 or duration > 15:
        raise NetworkError("invalid_video_job", "Scene duration is outside the 1-15 second API range.")
    prompt = (
        f"Illustrative vertical scene for a draft preview, not evidence. "
        f"Beat {scene['beat']['beat']}: {scene['beat']['narration']} "
        f"On-screen idea: {scene['beat']['on_screen_text'] or plan['script']['title']}"
    )[:500]
    request = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": "9:16",
        "resolution": resolution,
        "generate_audio": audio_mode == "generated",
    }
    identity = json.dumps([plan['plan_id'], scene_index, request], sort_keys=True).encode()
    job_id = "vid-" + sha256_bytes(identity)[:24]
    if _path(job_id).exists():
        return _load(job_id)
    record = {
        "contract": "grok_video_job",
        "version": "1.0",
        "job_id": job_id,
        "status": "prepared",
        "plan_id": plan["plan_id"],
        "scene_index": scene_index,
        "audio_mode": audio_mode,
        "illustrative_only": True,
        "publishable": False,
        "request": request,
        "request_id": None,
        "uncertain": False,
        "attempts": [],
    }
    _save(record)
    return record


@locked
def submit(job_id, *, consent, allow_network, transport=http_transport, retry_uncertain=False):
    record = _load(job_id)
    if consent != f"paid-generate:{job_id}":
        raise NetworkError("video_consent_required", "Submit needs --consent paid-generate:JOB_ID.")
    if not allow_network:
        raise NetworkError("network_not_allowed", "Submission needs --allow-network.")
    if record["status"] != "prepared" and not (record["status"] == "uncertain" and retry_uncertain is True and not record["request_id"]):
        raise NetworkError("video_submit_refused", "This job is not waiting for a new submission.")
    if not os.environ.get("XAI_API_KEY"):
        raise NetworkError("missing_video_credential", "XAI_API_KEY is not set.")
    record["status"] = "submitting"
    if len(record["attempts"]) >= 10:
        raise NetworkError("video_attempt_limit", "Maximum explicit attempts reached.")
    record["attempts"].append({"outcome": "uncertain"})
    _save(record)
    try:
        body = transport("POST", GENERATE_URL, record["request"], credential=True)
        request_id = body.get("request_id") if isinstance(body, dict) else None
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", request_id):
            raise ValueError
        reject_secrets(request_id)
    except Exception:
        record["status"] = "uncertain"
        record["uncertain"] = True
        _save(record)
        raise NetworkError("video_submit_uncertain", "Submission outcome is unknown. It was not retried.") from None
    record["request_id"] = request_id
    record["status"] = "submitted"
    record["uncertain"] = False
    record["attempts"][-1] = {"outcome": "submitted", "request_id": request_id}
    _save(record)
    return record


@locked
def status(job_id, *, allow_network, transport=http_transport):
    record = _load(job_id)
    if not allow_network:
        raise NetworkError("network_not_allowed", "Status checks need --allow-network.")
    if not record.get("request_id"):
        raise NetworkError("video_not_submitted", "There is no provider job id to check.")
    if record["status"] in {"done", "downloaded", "failed", "expired"}:
        return record
    try:
        body = transport("GET", STATUS_URL + record["request_id"], None, credential=True)
    except Exception:
        raise NetworkError("video_status_failed", "Status unavailable; check the saved job again without resubmitting.") from None
    state = body.get("status") if isinstance(body, dict) else None
    if state == "done":
        video = body.get("video")
        if not isinstance(video, dict):
            raise NetworkError("video_status_invalid", "Provider returned malformed media metadata.")
        url = video.get("url")
        _check_download_url(url)
        record["status"] = "done"
        record["download_url"] = url
    elif state in {"failed", "expired"}:
        record["status"] = state
    elif state == "pending":
        record["status"] = "pending"
    else:
        raise NetworkError("video_status_invalid", "Unrecognized provider response; the saved job ID was preserved.")
    _save(record)
    return record


@locked
def download(job_id, *, allow_network, media_root, transport=http_transport):
    record = _load(job_id)
    if record.get("status") != "done" or not record.get("download_url"):
        raise NetworkError("video_not_ready", "Download needs a done job with a saved URL.")
    if not allow_network:
        raise NetworkError("network_not_allowed", "Download needs --allow-network.")
    _check_download_url(record["download_url"])
    data = transport("DOWNLOAD", record["download_url"], None, credential=False)
    if not isinstance(data, (bytes, bytearray)) or len(data) < 32 or len(data) > MAX_DOWNLOAD or b"ftyp" not in data[:64]:
        raise NetworkError("video_download_invalid", "Downloaded media was empty, oversized, or not an MP4.")
    root = Path(media_root)
    root.mkdir(parents=True, exist_ok=True)
    name = f"{job_id}.mp4"
    if (root / name).exists():
        raise NetworkError("video_media_exists", "Media already exists; no overwrite occurred.")
    from .media_render import inspect_video
    with tempfile.TemporaryDirectory(dir=root) as temp:
        staged = Path(temp) / "clip.mp4"
        staged.write_bytes(data)
        info = inspect_video(staged)
        if abs(info["duration"] - record["request"]["duration"]) > .25 or abs(info["width"] / info["height"] - 9/16) > .03:
            raise NetworkError("video_download_invalid", "Generated duration or aspect ratio differs from the request.")
        os.rename(staged, root / name)
    record["status"] = "downloaded"
    record["media"] = {"relative_path": name, "sha256": sha256_bytes(data), "bytes": len(data)}
    _save(record)
    return record


def _check_download_url(url):
    check_download_url(url)


def _path(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"vid-[0-9a-f]{24}", job_id):
        raise NetworkError("video_job_not_found", "Unknown video job.")
    return jobs_dir() / f"{job_id}.json"


def _save(record):
    path = _path(record["job_id"])
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _validate(record)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise NetworkError("video_storage_failed", "Could not save video state atomically.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load(job_id):
    path = _path(job_id)
    if not path.is_file():
        raise NetworkError("video_job_not_found", "Unknown video job.")
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError
        record = json.loads(path.read_text(encoding="utf-8"))
        _validate(record)
        if record["job_id"] != job_id:
            raise ValueError
    except (OSError, ValueError, TypeError, UnicodeError):
        raise NetworkError("invalid_video_job", "Saved video state is corrupt.") from None
    if record["status"] == "submitting":
        record.update(status="uncertain", uncertain=True)
    return record


def _validate(record):
    try:
        reject_secrets(record)
        required = {"contract", "version", "job_id", "status", "plan_id", "scene_index", "audio_mode",
                    "illustrative_only", "publishable", "request", "request_id", "uncertain", "attempts", "updated_at"}
        if set(record) - required - {"download_url", "media"} or not required <= set(record):
            raise ValueError
        request = record["request"]
        if set(request) != {"model", "prompt", "duration", "aspect_ratio", "resolution", "generate_audio"}:
            raise ValueError
        identity = json.dumps([record["plan_id"], record["scene_index"], request], sort_keys=True).encode()
        if (record["job_id"] != "vid-" + sha256_bytes(identity)[:24]
                or record["contract"] != "grok_video_job" or record["version"] != "1.0"
                or record["publishable"] is not False or record["illustrative_only"] is not True
                or record["audio_mode"] not in {"none", "generated"}
                or type(record["uncertain"]) is not bool
                or request["generate_audio"] is not (record["audio_mode"] == "generated")
                or type(record["scene_index"]) is not int or record["scene_index"] not in range(1, 5)
                or not re.fullmatch(r"[0-9a-f]{64}", record["plan_id"])
                or record["status"] not in {"prepared", "submitting", "uncertain", "submitted", "pending", "done", "failed", "expired", "downloaded"}
                or not isinstance(record["attempts"], list) or len(record["attempts"]) > 10
                or request["model"] not in {DEFAULT_MODEL, "grok-imagine-video-1.5-lite"}
                or request["resolution"] not in {"480p", "720p", "1080p"}
                or request["aspect_ratio"] != "9:16" or type(request["duration"]) is not int or not 1 <= request["duration"] <= 15
                or not isinstance(request["prompt"], str) or not 1 <= len(request["prompt"]) <= 500):
            raise ValueError
        if record["request_id"] is not None and not re.fullmatch(r"[A-Za-z0-9-]{1,100}", record["request_id"]):
            raise ValueError
        if "download_url" in record:
            check_download_url(record["download_url"])
        if record["status"] in {"submitted", "pending", "done", "downloaded"} and not record["request_id"]:
            raise ValueError
        if record["status"] in {"done", "downloaded"} and "download_url" not in record:
            raise ValueError
        for attempt in record["attempts"]:
            if set(attempt) not in ({"outcome"}, {"outcome", "request_id"}) or attempt["outcome"] not in {"uncertain", "submitted"}:
                raise ValueError
        if "media" in record:
            media = record["media"]
            if (set(media) != {"relative_path", "sha256", "bytes"} or media["relative_path"] != record["job_id"] + ".mp4"
                    or not re.fullmatch(r"[0-9a-f]{64}", media["sha256"])
                    or type(media["bytes"]) is not int or not 32 <= media["bytes"] <= MAX_DOWNLOAD):
                raise ValueError
        datetime.fromisoformat(record["updated_at"])
    except (KeyError, TypeError, ValueError):
        raise NetworkError("invalid_video_job", "Saved video state failed validation.") from None


@locked
def inspect(job_id):
    return _load(job_id)


@locked
def media_manifest(plan, job_ids, media_root):
    """Four completed scenes become the existing local renderer's media contract."""
    from .media_manifest import load_asset, validate_media_manifest
    validate_scene_plan(plan)
    records = [_load(job_id) for job_id in job_ids]
    if len(records) != 4 or sorted(row["scene_index"] for row in records) != [1, 2, 3, 4]:
        raise NetworkError("video_scene_mismatch", "Supply exactly one completed job for each scene.")
    records.sort(key=lambda row: row["scene_index"])
    assets, assignments = [], []
    for record in records:
        if record["status"] != "downloaded" or record["plan_id"] != plan["plan_id"]:
            raise NetworkError("video_scene_mismatch", "All downloaded jobs must belong to this plan.")
        index = record["scene_index"]
        asset = {"asset_id": record["job_id"], **record["media"], "kind": "video",
                 "provenance": {"label": "xai_generated_illustration", "note": "Generated footage is not factual evidence."},
                 "rights": {"status": "declared_not_verified", "declared_by": "local-operator",
                            "declaration": "Provider output supplied for local review; usage rights are not verified."}}
        load_asset(media_root, asset)
        assets.append(asset)
        assignments.append({"scene_index": index, "asset_id": record["job_id"], "fit": "cover", "anchor": "center",
                            "trim": {"start_seconds": 0, "end_seconds": record["request"]["duration"]}})
    manifest = {"contract": "media_manifest", "version": "1.0", "manifest_id": "grok-" + plan["plan_id"][:24],
                "plan_id": plan["plan_id"], "mute_source_audio": True, "assets": assets, "assignments": assignments}
    return validate_media_manifest(manifest)
