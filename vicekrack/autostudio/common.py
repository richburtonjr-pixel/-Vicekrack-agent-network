"""Auto Studio shared pieces: configuration, storage, costs and the FFmpeg runner."""

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..errors import NetworkError
from ..orchestrator import ROOT as PROJECT_ROOT
from ..persistence import reject_secrets

CONFIG = "config/auto-studio.json"
PRODUCTION_ID = re.compile(r"^asp-[0-9a-f]{24}$")
CONTRACT, VERSION = "auto_production", "1.0"
ACTIVE = ("approved", "scripting", "voicing", "planning", "generating", "assembling", "verifying")
FINAL = ("completed", "failed", "stopped")
STEPS = ("script", "voices", "timing", "scenes", "music", "assembly", "verify")
_LOCK = threading.RLock()


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def load_config(root=PROJECT_ROOT):
    try:
        config = json.loads((Path(root) / CONFIG).read_text(encoding="utf-8"))
        assert config["config_version"] == "1.0"
        assert all(n in (15, 30, 60, 120) for n in config["lengths"])
        assert config["limits"]["max_scene_retries"] in range(0, 4) and config["limits"]["cap_multiplier"] >= 1
        assert config["timing"]["max_clip_seconds"] <= 15 and config["timing"]["min_clip_seconds"] >= 1
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        raise NetworkError("invalid_auto_studio_config", "config/auto-studio.json is missing or invalid.") from None
    return config


def base(root=None):
    path = Path(root if root is not None else PROJECT_ROOT) / "runtime" / "auto-studio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def folder(production_id, root=None):
    if not isinstance(production_id, str) or not PRODUCTION_ID.match(production_id):
        raise NetworkError("auto_production_not_found", "No such video.")
    return base(root) / production_id


def write_json(path, document):
    reject_secrets(document)
    data = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix=".tmp") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temp = Path(handle.name)
    os.replace(temp, path)


def read_state(production_id, root=None):
    path = folder(production_id, root) / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("contract") != CONTRACT or state.get("production_id") != production_id:
            raise ValueError
    except FileNotFoundError:
        raise NetworkError("auto_production_not_found", "No such video.") from None
    except (OSError, ValueError):
        raise NetworkError("invalid_auto_production", "This video's saved state is unreadable.") from None
    return state


def save_state(state, root=None, clock=utc_now):
    with _LOCK:
        state["updated_at"] = clock()
        write_json(folder(state["production_id"], root) / "state.json", state)
    return state


def list_ids(root=None):
    folder_ = base(root)
    return sorted((p.name for p in folder_.iterdir() if p.is_dir() and PRODUCTION_ID.match(p.name)), reverse=True)


# ---------------------------------------------------------------- money

def video_price(config, model, resolution):
    try:
        return config["prices"]["video_per_second"][model][resolution]
    except KeyError:
        raise NetworkError("invalid_auto_settings", "That model and resolution have no configured price.") from None


def tts_cost(config, characters):
    return characters / 1e6 * config["prices"]["tts_per_million_characters"]


def add_cost(state, kind, detail, usd, certain=True, clock=utc_now):
    usd = round(float(usd), 4)
    state["cost"]["items"].append({"kind": kind, "detail": detail[:120], "usd": usd, "certain": bool(certain), "at": clock()})
    state["cost"]["items"] = state["cost"]["items"][-400:]
    state["cost"]["spent_usd"] = round(state["cost"]["spent_usd"] + usd, 4)


def can_spend(state, usd):
    return state["cost"]["spent_usd"] + usd <= state["estimate"]["cap_usd"] + 1e-9


def require_budget(state, usd, what):
    if not can_spend(state, usd):
        raise NetworkError("spending_cap_reached",
                           f"Stopped before {what}: it would go over the approved cap of ${state['estimate']['cap_usd']:.2f}.")


# ---------------------------------------------------------------- FFmpeg

def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        raise NetworkError("renderer_unavailable", "Install requirements-render.txt (FFmpeg) to assemble videos.") from None


def run_ffmpeg(arguments, cwd, timeout=600, capture=False):
    """Run the bundled FFmpeg with a clean environment. Returns stderr text when capture=True."""
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG")
           if key in os.environ}
    try:
        result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", *arguments], cwd=str(cwd),
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise NetworkError("assembly_timeout", "FFmpeg took too long.") from None
    except OSError:
        raise NetworkError("assembly_failed", "FFmpeg could not start.") from None
    text = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        tail = " ".join(text.strip().splitlines()[-3:])[-300:]
        raise NetworkError("assembly_failed", "FFmpeg failed: " + re.sub(r"[A-Za-z]:\\[^ ]+|/[^ ]+/", "", tail))
    return text if capture else None


def probe(path):
    """Duration, size, fps and streams of a media file, read with the bundled FFmpeg."""
    env_cwd = Path(path).parent
    try:
        result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)], cwd=str(env_cwd),
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        raise NetworkError("media_probe_failed", "The media file could not be read.") from None
    header = result.stderr.decode("utf-8", errors="replace")
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", header)
    video = re.search(r"Stream #0:\d+[^\n]*Video: (\w+)[^\n]*?(\d{2,5})x(\d{2,5})(?:[^\n]*?(\d+(?:\.\d+)?) fps)?", header)
    audio = re.search(r"Stream #0:\d+[^\n]*Audio: (\w+)[^\n]*?(\d+) Hz", header)
    if duration is None:
        raise NetworkError("media_probe_failed", "The media file could not be read.")
    h, m, s = duration.groups()
    return {"duration": round(int(h) * 3600 + int(m) * 60 + float(s), 3),
            "video": None if not video else {"codec": video.group(1), "width": int(video.group(2)),
                                             "height": int(video.group(3)),
                                             "fps": float(video.group(4)) if video.group(4) else None},
            "audio": None if not audio else {"codec": audio.group(1), "sample_rate": int(audio.group(2))}}


def ceil_to(value, step=1):
    return math.ceil(value / step - 1e-9) * step
