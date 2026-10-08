"""Step 43 Grok video jobs. One explicit request, no automatic resubmit.

Official flow (docs.x.ai, October 2026): POST https://api.x.ai/v1/videos/generations,
then GET https://api.x.ai/v1/videos/{request_id}. Completed clips are temporary HTTPS
URLs. This adapter does not poll in a loop and does not spend credits unless submit
is called with consent and --allow-network.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from .artifact_binding import sha256_bytes
from .errors import NetworkError
from .orchestrator import ROOT
from .scene_plan import validate_scene_plan

GENERATE_URL = "https://api.x.ai/v1/videos/generations"
STATUS_URL = "https://api.x.ai/v1/videos/"
DOWNLOAD_HOSTS = {"vidgen.x.ai"}
DEFAULT_MODEL = "grok-imagine-video-1.5"
MAX_DOWNLOAD = 80 * 1024 * 1024


def jobs_dir():
    path = ROOT / "runtime" / "video-jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare(plan, scene_index, *, model=DEFAULT_MODEL, audio_mode="none", resolution="720p"):
    validate_scene_plan(plan)
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
    job_id = f"vid-{plan['plan_id'][:12]}-{scene_index}"
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
    }
    _save(record)
    return record


def submit(job_id, *, consent, allow_network, transport):
    record = _load(job_id)
    if consent != f"paid-generate:{job_id}":
        raise NetworkError("video_consent_required", "Submit needs --consent paid-generate:JOB_ID.")
    if not allow_network:
        raise NetworkError("network_not_allowed", "Submission needs --allow-network.")
    if record["status"] not in {"prepared", "retry_authorized"}:
        raise NetworkError("video_submit_refused", "This job is not waiting for a new submission.")
    if not os.environ.get("XAI_API_KEY"):
        raise NetworkError("missing_video_credential", "XAI_API_KEY is not set.")
    record["status"] = "submitting"
    _save(record)
    try:
        body = transport("POST", GENERATE_URL, record["request"], credential=True)
    except TimeoutError:
        record["status"] = "uncertain"
        record["uncertain"] = True
        _save(record)
        raise NetworkError("video_submit_uncertain", "Submission timed out. It was not retried.") from None
    except OSError:
        record["status"] = "uncertain"
        record["uncertain"] = True
        _save(record)
        raise NetworkError("video_submit_uncertain", "Submission result is unknown. It was not retried.") from None
    request_id = body.get("request_id") if isinstance(body, dict) else None
    if not request_id:
        record["status"] = "failed"
        _save(record)
        raise NetworkError("video_provider_rejected", "The provider did not return a request id.")
    record["request_id"] = request_id
    record["status"] = "submitted"
    record["uncertain"] = False
    _save(record)
    return record


def status(job_id, *, allow_network, transport):
    record = _load(job_id)
    if not allow_network:
        raise NetworkError("network_not_allowed", "Status checks need --allow-network.")
    if not record.get("request_id"):
        raise NetworkError("video_not_submitted", "There is no provider job id to check.")
    body = transport("GET", STATUS_URL + record["request_id"], None, credential=True)
    state = body.get("status") if isinstance(body, dict) else None
    if state == "done":
        url = ((body.get("video") or {}).get("url"))
        _check_download_url(url)
        record["status"] = "done"
        record["download_url"] = url
    elif state == "failed":
        record["status"] = "failed"
    elif state == "pending":
        record["status"] = "pending"
    else:
        record["status"] = "uncertain"
        record["uncertain"] = True
    _save(record)
    return record


def download(job_id, *, allow_network, transport, media_root):
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
    (root / name).write_bytes(data)
    record["status"] = "downloaded"
    record["media"] = {"relative_path": name, "sha256": sha256_bytes(data), "bytes": len(data)}
    _save(record)
    return record


def authorize_retry(job_id):
    record = _load(job_id)
    if not record.get("uncertain"):
        raise NetworkError("video_retry_refused", "Only an uncertain submission can be retried.")
    record["status"] = "retry_authorized"
    _save(record)
    return record


def _check_download_url(url):
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS or parsed.username:
        raise NetworkError("video_download_refused", "Download host is not an allowed xAI video host.")


def _path(job_id):
    if not job_id.startswith("vid-") or "/" in job_id:
        raise NetworkError("video_job_not_found", "Unknown video job.")
    return jobs_dir() / f"{job_id}.json"


def _save(record):
    path = _path(record["job_id"])
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def _load(job_id):
    path = _path(job_id)
    if not path.is_file():
        raise NetworkError("video_job_not_found", "Unknown video job.")
    return json.loads(path.read_text(encoding="utf-8"))
