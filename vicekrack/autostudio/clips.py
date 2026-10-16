"""Auto Studio scene clips: xAI video generation (POST /v1/videos/generations, GET /v1/videos/{id}, then the
temporary media URL on vidgen.x.ai). Up to 15 s per clip; each clip is sized to its scene's voiceover."""

import re
from pathlib import Path

from ..errors import NetworkError
from ..persistence import reject_secrets
from ..video_transport import GENERATE_URL, STATUS_URL, check_download_url, transport as http_transport
from .common import probe, sha256_bytes

STYLE = ("Vertical 9:16 cinematic shot. Original, illustrative AI-generated scene, not real footage. "
         "No readable text, no logos or brand marks, no real people's likenesses. ")


def request_for(segment, scene, settings, part, parts):
    prompt = STYLE + scene["visual_prompt"]
    if parts > 1:
        prompt += f" (Part {part} of {parts}: continue the same scene with a new camera angle.)"
    return {"model": settings["model"], "prompt": prompt[:1000], "duration": int(segment["duration"]),
            "aspect_ratio": "9:16", "resolution": settings["resolution"], "generate_audio": bool(settings["native_audio"])}


def submit(request, *, transport=None):
    """Returns the provider request ID. Raises NetworkError clip_submit_uncertain (may be billed)."""
    try:
        body = (transport or http_transport)("POST", GENERATE_URL, request, credential=True)
        request_id = body.get("request_id") if isinstance(body, dict) else None
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", request_id):
            raise ValueError
        reject_secrets(request_id)
        return request_id
    except NetworkError as error:
        if error.code == "missing_video_credential":
            raise
        raise NetworkError("clip_submit_uncertain", "A clip request failed; it may have been billed.") from None
    except Exception:
        raise NetworkError("clip_submit_uncertain", "A clip request failed; it may have been billed.") from None


def status(request_id, *, transport=None):
    """("pending" | "done" | "failed" | "expired", download URL or None). Raises clip_status_unavailable."""
    try:
        body = (transport or http_transport)("GET", STATUS_URL + request_id, None, credential=True)
    except Exception:
        raise NetworkError("clip_status_unavailable", "Could not check a clip right now.") from None
    state = body.get("status") if isinstance(body, dict) else None
    if state == "done":
        url = (body.get("video") or {}).get("url") if isinstance(body.get("video"), dict) else None
        check_download_url(url)
        return "done", url
    if state in ("failed", "expired", "pending"):
        return state, None
    if state in ("queued", "processing", "in_progress", "running"):
        return "pending", None
    raise NetworkError("clip_status_unavailable", "xAI sent an unexpected clip status.")


def download(url, target, expected_seconds, *, transport=None):
    """Download, then check it really is a playable vertical video of about the right length."""
    check_download_url(url)
    try:
        data = (transport or http_transport)("DOWNLOAD", url, None, credential=False)
    except Exception:
        raise NetworkError("clip_download_failed", "A finished clip could not be downloaded yet.") from None
    if not isinstance(data, (bytes, bytearray)) or len(data) < 1024 or b"ftyp" not in data[:64]:
        raise NetworkError("clip_invalid", "A downloaded clip was not a video.")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    partial.write_bytes(data)
    info = probe(partial)
    video = info["video"]
    if not video or abs(info["duration"] - expected_seconds) > 1.0 or abs(video["width"] / video["height"] - 9 / 16) > 0.05:
        partial.unlink(missing_ok=True)
        raise NetworkError("clip_invalid", "A downloaded clip has the wrong length or shape.")
    partial.replace(target)
    return {"sha256": sha256_bytes(bytes(data)), "seconds": info["duration"], "width": video["width"],
            "height": video["height"], "has_audio": info["audio"] is not None}
