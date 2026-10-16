"""Auto Studio planning: how many scenes a length needs, the word budget, the timeline built from the real
voice lengths, and the cost estimate shown before approval. Pure functions."""

import math

from ..errors import NetworkError
from .common import tts_cost, video_price


def scene_count(config, length):
    t = config["timing"]
    content = length - t["intro_seconds"] - t["outro_seconds"]
    return max(2, math.ceil(content / t["target_clip_seconds"]))


def budget(config, length, scenes=None, shrink=1.0):
    """Seconds of clip per scene and the words of speech each scene should carry."""
    t = config["timing"]
    n = scenes or scene_count(config, length)
    clips = length - t["intro_seconds"] - t["outro_seconds"] + t["transition_seconds"] * (n + 1)
    per_scene = clips / n
    speech = max(1.5, (per_scene - t["lead_in_seconds"] - t["tail_seconds"]) * t["speech_fill"])
    words = max(5, int(speech * t["words_per_second"] * shrink))
    return {"scenes": n, "clip_seconds": round(per_scene, 2), "words_per_scene": words,
            "total_words": words * n, "characters": int(words * n * 6.2)}


def estimate(config, length, model, resolution):
    """Expected cost and the spending cap (the most the approval allows, retries included)."""
    b = budget(config, length)
    t = config["timing"]
    clip_seconds = math.ceil(b["clip_seconds"]) * b["scenes"]           # xAI bills whole seconds per clip
    video = clip_seconds * video_price(config, model, resolution)
    voice = tts_cost(config, b["characters"])
    script = config["prices"]["script_estimate_usd"]
    expected = round(video + voice + script, 2)
    retry_room = config["limits"]["cap_multiplier"]
    cap = round(max(expected * retry_room, expected + t["max_clip_seconds"] * video_price(config, model, resolution)), 2)
    return {"expected_usd": expected, "cap_usd": cap, "video_usd": round(video, 2), "voice_usd": round(voice, 4),
            "script_usd": script, "clip_seconds": clip_seconds, "scenes": b["scenes"]}


def timeline(config, length, scene_lines):
    """Turn real voice lengths into clips and absolute times.

    scene_lines: per script scene, a list of line seconds. Returns segments (one xAI clip each, at most
    max_clip_seconds; a scene whose speech is longer is split into several clips) with every line's
    absolute start time, so captions and voices line up with the picture exactly.
    """
    t = config["timing"]
    lead, gap, tail, x = t["lead_in_seconds"], t["line_gap_seconds"], t["tail_seconds"], t["transition_seconds"]
    lo, hi = t["min_clip_seconds"], t["max_clip_seconds"]
    segments = []
    for scene_index, lines in enumerate(scene_lines):
        group, used = [], lead
        for line_index, seconds in enumerate(lines):
            if seconds + lead + tail > hi:
                raise NetworkError("voice_line_too_long", "One spoken line is longer than a single clip can hold.")
            need = (gap if group else 0) + seconds
            if group and used + need + tail > hi:
                segments.append({"scene": scene_index, "lines": group, "speech_end": used})
                group, used = [], lead
                need = seconds
            group.append({"line": line_index, "offset": used + (gap if group else 0), "seconds": seconds})
            used += need
        segments.append({"scene": scene_index, "lines": group, "speech_end": used})
    for seg in segments:
        seg["duration"] = int(min(hi, max(lo, math.ceil(seg["speech_end"] + tail - 1e-9))))
    total = lambda: t["intro_seconds"] + t["outro_seconds"] + sum(s["duration"] for s in segments) \
        - x * (len(segments) + 1)
    # Too short: lengthen clips (still at most 15 s each) until the video reaches its length.
    while total() < length - 0.5 and any(s["duration"] < hi for s in segments):
        shortest = min((s for s in segments if s["duration"] < hi), key=lambda s: s["duration"] - s["speech_end"])
        shortest["duration"] += 1
    start = t["intro_seconds"] - x
    for seg in segments:
        seg["start"] = round(start, 3)
        for line in seg["lines"]:
            line["at"] = round(start + line["offset"], 3)
        start += seg["duration"] - x
    end = round(start + t["outro_seconds"], 3)
    return {"segments": segments, "total_seconds": end, "target_seconds": length, "transition_seconds": x, "fade_seconds": t.get("fade_seconds", 0.25),
            "intro_seconds": t["intro_seconds"], "outro_seconds": t["outro_seconds"],
            "within_tolerance": abs(end - length) <= length * t["length_tolerance"] + 0.5}
