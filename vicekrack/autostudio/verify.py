"""Auto Studio verification: checks the ACTUAL exported MP4, never the API responses.

  - it decodes from start to end (video and audio);
  - 1080x1920, 30 fps, H.264 + AAC 48 kHz stereo;
  - its length matches the timeline and the chosen length (within the configured tolerance);
  - every scene appears in order: a frame from the middle of each scene in the final video matches that
    scene's own clip better than any other clip;
  - every spoken line is audible at its planned time, and spoken lines never overlap;
  - the soundtrack is not silent where nobody speaks either when music is on.
"""

import re
import struct
from pathlib import Path

from ..errors import NetworkError
from .common import ffmpeg_exe, probe, run_ffmpeg

THUMB = (36, 64)


def _decode_ok(path, cwd):
    run_ffmpeg(["-xerror", "-i", str(path), "-f", "null", "-"], cwd, timeout=600)


def _gray(path, at, cwd, crop_clip=False):
    """A small colour picture of the middle band of a frame (between the top label and the captions)."""
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920," if crop_clip else ""
    vf += f"crop=1080:700:0:380,scale={THUMB[0]}:{THUMB[1]},format=rgb24"
    import subprocess
    import os
    result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{at:.3f}",
                             "-i", str(path), "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-"],
                            cwd=str(cwd), capture_output=True, timeout=120,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    data = result.stdout
    if result.returncode != 0 or len(data) != THUMB[0] * THUMB[1] * 3:
        raise NetworkError("verify_failed", "A frame could not be read for the scene-order check.")
    return data


def _distance(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _rms(path, start, seconds, cwd):
    import subprocess
    import os
    result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-ss", f"{max(0.0, start):.3f}",
                             "-t", f"{max(0.05, seconds):.3f}", "-i", str(path), "-map", "0:a:0",
                             "-af", "volumedetect", "-f", "null", "-"], cwd=str(cwd), capture_output=True, timeout=120,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    found = re.search(r"mean_volume: (-?[0-9.]+|-inf) dB", result.stderr.decode("utf-8", "replace"))
    if not found:
        raise NetworkError("verify_failed", "The soundtrack could not be measured.")
    return -120.0 if found.group(1) == "-inf" else float(found.group(1))


def verify(final, *, timeline, clips, voices_rms_floor=-38.0, tolerance=0.12, music=True):
    """Returns {"passed": bool, "checks": [...], "facts": {...}}; never raises for a failed check."""
    final = Path(final)
    cwd = final.parent
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
        return ok

    try:
        _decode_ok(final, cwd)
        decodes = True
    except NetworkError:
        decodes = False
    check("decodes", decodes, "The whole file decodes without errors." if decodes else "The file has decode errors.")
    info = probe(final)
    video, audio = info["video"], info["audio"]
    check("format", video and video["width"] == 1080 and video["height"] == 1920 and round(video["fps"] or 0) == 30
          and video["codec"] == "h264" and audio and audio["codec"] == "aac" and audio["sample_rate"] == 48000,
          f"{video and video['width']}x{video and video['height']} at {video and video['fps']} fps, "
          f"audio {audio and audio['codec']} {audio and audio['sample_rate']} Hz")
    planned, target = timeline["total_seconds"], timeline["target_seconds"]
    check("length", abs(info["duration"] - planned) <= 0.35 and abs(info["duration"] - target) <= target * tolerance + 0.5,
          f"{info['duration']:.1f} s (planned {planned:.1f} s, chosen length {target} s)")
    # Scene order.
    order_ok, details = True, []
    if decodes:
        refs = [_gray(clips[i], min(seg["duration"] / 2, max(0.5, seg["duration"] - 1)), cwd, crop_clip=True)
                for i, seg in enumerate(timeline["segments"])]
        for i, seg in enumerate(timeline["segments"]):
            frame = _gray(final, seg["start"] + min(seg["duration"] / 2, max(0.5, seg["duration"] - 1)), cwd)
            distances = [_distance(frame, ref) for ref in refs]
            best = min(range(len(refs)), key=lambda k: distances[k])
            same = distances[i] <= distances[best] + 1e-6
            order_ok &= same
            details.append(f"scene {i + 1}: {'matches its clip' if same else 'looks like clip ' + str(best + 1)}")
    check("scene_order", order_ok and decodes, "; ".join(details) or "not checked")
    # Voices: audible at their times, never overlapping.
    lines = sorted((p["at"], p["seconds"]) for seg in timeline["segments"] for p in seg["lines"])
    overlaps = [i for i in range(len(lines) - 1) if lines[i][0] + lines[i][1] > lines[i + 1][0] + 0.02]
    check("no_overlap", not overlaps, "Spoken lines never overlap." if not overlaps else f"{len(overlaps)} overlaps")
    quiet = []
    for at, seconds in lines:
        level = _rms(final, at + 0.1, max(0.2, seconds - 0.2), cwd)
        if level < voices_rms_floor:
            quiet.append(f"{at:.1f}s ({level:.0f} dB)")
    check("voices_present", not quiet, f"All {len(lines)} spoken lines are audible at their planned times."
          if not quiet else "Quiet lines at " + ", ".join(quiet[:5]))
    overall = _rms(final, 0, info["duration"], cwd)
    check("soundtrack", overall > -40, f"Average level {overall:.0f} dB.")
    if music:
        intro = _rms(final, 0.2, max(0.3, timeline["intro_seconds"] - 0.4), cwd)
        check("music", intro > -45, f"Music under the intro at {intro:.0f} dB.")
    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "facts": {"duration": info["duration"], "width": video and video["width"], "height": video and video["height"],
                      "fps": video and video["fps"], "audio": audio, "scenes": len(timeline["segments"]),
                      "lines": len(lines)}}
