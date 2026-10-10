"""Step 47: narration-aligned captions.

A caption track is built ONLY from the validated ShortScript behind a completed Grok speech job
(Step 46): the same narration beats, words, order and qualifiers that were spoken. Nothing is
rewritten, summarised or added. Cues are short phrases (at most `max_lines` lines of at most
`max_chars_per_line` characters) that never span two beats.

Timing, chosen explicitly:

  provider   "provider_character_timestamps": the xAI character timestamps saved with the speech
             job (requested with `speech-prepare --with-timestamps`). They are validated against
             the spoken text character by character (order, bounds, overlaps, duration) before a
             cue start/end is taken from the first/last character of its words.
  estimated  "estimated_phrase": cue times spread over the measured narration duration in
             proportion to each phrase's length. Clearly labelled, never called synchronized,
             burned in with a visible "timing estimated" label, and the quality report marks it
             needs_review so approval requires an explicit acknowledgment.

No new paid request, no transcription service and no alignment model is ever used.

Storage: runtime/captions/<caption_id>/ (ignored by Git): track.json (contract `caption_track`
1.0), captions.srt and captions.vtt, each written once atomically; the track records the
sidecars' hashes and every load re-checks all three (captions_tampered). The caption ID is
derived from the track's content, so a different text, timing or style is a different track.
"""

import hashlib
import json
import math
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import NetworkError
from .orchestrator import ROOT
from .persistence import reject_secrets

CONTRACT, VERSION = "caption_track", "1.0"
CONFIG_PATH = "config/captions.json"
CAPTION_ID = re.compile(r"^cap-[0-9a-f]{24}$")
METHODS = {"provider": "provider_character_timestamps", "estimated": "estimated_phrase"}
MAX_PREVIEW_MS = 15000
SENTENCE_END = (".", "!", "?", ";", ":")
FILES = ("track.json", "captions.srt", "captions.vtt")
NOTICE = ("Captions repeat the approved narration text exactly. They are part of a draft preview; publishable stays "
          "false. Burned-in captions are part of the reviewed video; the SRT/WebVTT sidecars are optional copies.")
# Reserved by the existing media overlay (preview.py / media_render.py): the top warning band and the
# bottom title/disclosure box. Captions must never cover either.
RESERVED = ((60, 60, 1020, 168), (45, 1420, 1035, 1880))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@lru_cache(maxsize=None)
def _validator():
    schema = json.loads((ROOT / "schemas/caption-track.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def load_config(root=ROOT):
    try:
        config = json.loads((Path(root) / CONFIG_PATH).read_text(encoding="utf-8"))
        style, timing = config["style"], config["timing"]
        ints = ("font_size", "max_chars_per_line", "max_lines", "max_line_pixels", "box_left", "box_right",
                "box_bottom", "box_padding", "estimated_label_size")
        if (config.get("config_version") != "1.0" or not all(type(style[k]) is int and style[k] > 0 for k in ints)
                or not 1 <= style["max_lines"] <= 2 or not 28 <= style["font_size"] <= 120
                or not all(type(timing[k]) is int and timing[k] >= 0 for k in timing)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise NetworkError("invalid_caption_config", "config/captions.json is invalid.") from None
    return config


def box_height(style, estimated):
    spacing = int(style["font_size"] * 1.35)
    label = int(style["estimated_label_size"] * 1.5) if estimated else 0
    return 2 * style["box_padding"] + label + style["max_lines"] * spacing


def caption_box(style, estimated):
    """The (left, top, right, bottom) area a cue may occupy (its tallest form)."""
    return (style["box_left"], style["box_bottom"] - box_height(style, estimated), style["box_right"], style["box_bottom"])


def check_layout(style, estimated):
    left, top, right, bottom = caption_box(style, estimated)
    if left < 40 or right > 1040 or top < 200 or right - left < style["max_line_pixels"]:
        raise NetworkError("caption_layout_unsafe", "The caption box leaves the safe area.")
    for x0, y0, x1, y1 in RESERVED:
        if left < x1 and x0 < right and top < y1 and y0 < bottom:
            raise NetworkError("caption_layout_collision", "The caption box would cover a preview warning or overlay.")


# ---------------------------------------------------------------- text: exactly the spoken narration

def spoken_words(script):
    """[(beat_index, word, first_char, last_char)] with offsets into the Step 46 spoken text."""
    from .speech_jobs import narration_text
    text = narration_text(script)                    # validates the script; refuses markup
    words, position = [], 0
    beats = [" ".join(beat["narration"].split()) for beat in script["beats"]]
    for beat_index, beat in enumerate(beats, start=1):
        if not beat:
            continue
        for word in beat.split(" "):
            start = text.index(word, position)
            if start != position:
                raise NetworkError("caption_text_mismatch", "Caption words do not follow the spoken text.")
            words.append((beat_index, word, start, start + len(word) - 1))
            position = start + len(word) + 1
    return text, words


def check_characters(text):
    """Burned-in captions use the preview font: refuse (never drop) characters it cannot draw."""
    from .preview import preview_text
    text = text.replace("\n", " ")                  # beat separators in the spoken text
    if any(c in text for c in "<>") or "-->" in text or any(ord(c) < 32 for c in text):
        raise NetworkError("caption_unsafe_text", "Caption text contains characters that are unsafe in caption files.")
    try:
        preview_text(text)
    except NetworkError:
        raise NetworkError("caption_unsupported_character", "A narration character cannot be drawn by the caption "
                           "font. Edit the script; captions never drop or replace text silently.") from None


def wrap(words, max_chars, max_lines):
    """Greedy lines of at most max_chars; None if the words do not fit in max_lines."""
    lines, line = [], ""
    for word in words:
        if len(word) > max_chars:
            raise NetworkError("caption_word_too_long", "A narration word is longer than one caption line.")
        candidate = (line + " " + word).strip()
        if len(candidate) <= max_chars:
            line = candidate
        else:
            lines.append(line)
            line = word
    lines.append(line)
    return lines if len(lines) <= max_lines else None


def segment(words, style, timing):
    """Phrase cues: as many words as fit the box, never across beats, breaking after sentences."""
    cues, current = [], []
    for position, item in enumerate(words):
        if current and (item[0] != current[-1][0] or wrap([w[1] for w in current + [item]],
                                                          style["max_chars_per_line"], style["max_lines"]) is None):
            cues.append(current)
            current = []
        current.append(item)
        last = position == len(words) - 1
        if not last and item[1].endswith(SENTENCE_END) and len(current) >= timing["sentence_break_min_words"]:
            cues.append(current)
            current = []
    if current:
        cues.append(current)
    out = []
    for index, group in enumerate(cues, start=1):
        lines = wrap([w[1] for w in group], style["max_chars_per_line"], style["max_lines"])
        if lines is None:
            raise NetworkError("caption_overflow", "A caption phrase does not fit the caption box.")
        out.append({"index": index, "beat": group[0][0], "text": " ".join(w[1] for w in group), "lines": lines,
                    "first_char": group[0][2], "last_char": group[-1][3]})
    return out


# ---------------------------------------------------------------- timing

def validate_timestamps(timestamps, text, duration_ms, timing):
    """Provider character timings must describe exactly the spoken text, in order, within the audio."""
    chars, times = timestamps.get("graph_chars"), timestamps.get("graph_times")
    if not isinstance(chars, list) or not isinstance(times, list) or len(chars) != len(times) or not chars:
        raise NetworkError("caption_timing_invalid", "Provider timestamps are malformed.")
    if not all(isinstance(c, str) for c in chars) or "".join(chars) != text:
        raise NetworkError("caption_timing_text_mismatch", "Provider timestamps do not describe the spoken narration text.")
    limit = (duration_ms + timing["provider_end_tolerance_ms"]) / 1000
    provider = timestamps.get("duration")
    if isinstance(provider, (int, float)) and abs(provider * 1000 - duration_ms) > timing["provider_duration_tolerance_ms"]:
        raise NetworkError("caption_timing_out_of_range", "Provider timing duration differs from the measured audio.")
    previous_start = previous_end = 0.0
    for pair in times:
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in pair)):
            raise NetworkError("caption_timing_invalid", "Provider timestamps are malformed.")
        start, end = pair
        if start < 0 or end < 0:
            raise NetworkError("caption_timing_negative", "Provider timestamps contain negative times.")
        if end < start:
            raise NetworkError("caption_timing_invalid", "A provider timestamp ends before it starts.")
        if end > limit or start > limit:
            raise NetworkError("caption_timing_out_of_range", "Provider timestamps extend past the narration audio.")
        if start < previous_start or end < previous_end:
            raise NetworkError("caption_timing_out_of_order", "Provider timestamps are out of order.")
        previous_start, previous_end = start, end
    return [(round(s * 1000), round(e * 1000)) for s, e in times]


def time_provider(cues, timestamps, text, duration_ms, timing):
    times = validate_timestamps(timestamps, text, duration_ms, timing)
    raw = [(times[c["first_char"]][0], times[c["last_char"]][1]) for c in cues]
    end_limit = min(duration_ms, MAX_PREVIEW_MS)
    for i, cue in enumerate(cues):
        start, end = raw[i]
        start = min(start, end_limit)
        following = raw[i + 1][0] if i + 1 < len(raw) else end_limit
        # Hold a phrase on screen a little after it is spoken, never past the next phrase or the audio.
        display_end = min(max(end, start + timing["min_cue_ms"]), following, end_limit)
        display_end = max(display_end, min(end + timing["hold_ms"], following, end_limit))
        cue["start_ms"], cue["end_ms"] = start, display_end
    return cues


def time_estimated(cues, duration_ms, timing):
    """Spread over the measured narration in proportion to phrase length. An ESTIMATE, labelled as one."""
    weights = [len(c["text"].replace(" ", "")) + 2 for c in cues]
    total, elapsed = sum(weights), 0
    end_limit = min(duration_ms, MAX_PREVIEW_MS)
    for cue, weight in zip(cues, weights):
        start = round(end_limit * elapsed / total)
        elapsed += weight
        cue["start_ms"], cue["end_ms"] = start, round(end_limit * elapsed / total)
    return cues


def validate_cues(cues, duration_ms, timing, text=None):
    if not cues:
        raise NetworkError("caption_cues_empty", "A caption track needs at least one cue.")
    limit = min(duration_ms, MAX_PREVIEW_MS)
    previous_end = 0
    for number, cue in enumerate(cues, start=1):
        start, end = cue["start_ms"], cue["end_ms"]
        if cue["index"] != number:
            raise NetworkError("caption_cues_out_of_order", "Caption cues are not numbered in order.")
        if start < 0 or end < 0:
            raise NetworkError("caption_cue_negative", "A caption cue has a negative time.")
        if end > limit:
            raise NetworkError("caption_cue_out_of_range", "A caption cue extends past the narration or the 15-second video.")
        if start < previous_end:
            raise NetworkError("caption_cues_overlap" if number > 1 and start >= cues[number - 2]["start_ms"]
                               else "caption_cues_out_of_order", "Caption cues overlap or are out of order.")
        if end - start < timing["min_cue_ms"]:
            raise NetworkError("caption_cue_too_short", "A caption cue is too short to read.")
        previous_end = end
    if text is not None and " ".join(c["text"] for c in cues).split() != text.split():
        raise NetworkError("caption_text_mismatch", "Caption text is not exactly the spoken narration.")


# ---------------------------------------------------------------- sidecars

def _clock(ms, separator):
    hours, rest = divmod(ms, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def srt(track):
    out = []
    for cue in track["cues"]:
        out += [str(cue["index"]), f"{_clock(cue['start_ms'], ',')} --> {_clock(cue['end_ms'], ',')}", *cue["lines"], ""]
    return ("\n".join(out) + "\n").encode("utf-8")


def _vtt_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def vtt(track):
    out = ["WEBVTT", "", "NOTE " + ("estimated phrase timing; check before approval" if
                                    track["timing"]["requires_manual_timing_review"] else
                                    "phrase cues timed from provider character timestamps"), ""]
    for cue in track["cues"]:
        out += [str(cue["index"]), f"{_clock(cue['start_ms'], '.')} --> {_clock(cue['end_ms'], '.')}",
                *[_vtt_escape(line) for line in cue["lines"]], ""]
    return ("\n".join(out) + "\n").encode("utf-8")


# ---------------------------------------------------------------- the track

def build_track(script, speech_record, narration_wav, *, method, timestamps=None, config=None):
    """Pure: a validated track from a script, its completed speech job and the job's managed WAV."""
    from .narration import parse_wav
    from .speech_jobs import script_digest
    config = config or load_config()
    style, timing = config["style"], config["timing"]
    if method not in METHODS:
        raise NetworkError("caption_timing_choice_required", "Choose --timing provider or --timing estimated.")
    if script_digest(script) != speech_record["script"]["script_sha256"]:
        raise NetworkError("caption_script_mismatch", "The script is not the one this narration was generated from.")
    text, words = spoken_words(script)
    if text != speech_record["text"] or sha256_bytes(text.encode()) != speech_record["text_sha256"]:
        raise NetworkError("caption_script_mismatch", "The script's narration differs from the spoken text.")
    if sha256_bytes(narration_wav) != speech_record["audio"]["sha256"]:
        raise NetworkError("caption_narration_mismatch", "The narration audio is not this speech job's audio.")
    channels, rate, pcm = parse_wav(narration_wav)
    duration_ms = round(len(pcm) / (channels * 2) / rate * 1000)
    if style["burn_in"]:
        check_characters(text)
        check_layout(style, method == "estimated")
    cues = segment(words, style, timing)
    if method == "provider":
        if timestamps is None:
            raise NetworkError("provider_timing_unavailable", "This speech job has no saved provider timestamps. Use "
                               "--timing estimated (labelled for review), or prepare a NEW speech job with "
                               "--with-timestamps (a separate paid request you must approve).")
        cues = time_provider(cues, timestamps["data"], text, duration_ms, timing)
    else:
        cues = time_estimated(cues, duration_ms, timing)
    for cue in cues:
        cue.pop("first_char"), cue.pop("last_char")
    validate_cues(cues, duration_ms, timing, text)
    estimated = method == "estimated"
    track = {"contract": CONTRACT, "version": VERSION, "caption_id": None, "publishable": False, "notice": NOTICE,
             "language": speech_record["settings"]["language"] if speech_record["settings"]["language"] != "auto"
             else "en",
             "source": {"speech_job_id": speech_record["job_id"], "script_id": speech_record["script"]["script_id"],
                        "script_sha256": speech_record["script"]["script_sha256"],
                        "text_sha256": speech_record["text_sha256"], "narration_sha256": speech_record["audio"]["sha256"],
                        "narration_duration_ms": duration_ms, "voice": speech_record["settings"]["voice"]},
             "timing": {"method": METHODS[method], "word_synchronized": False,
                        "cue_boundaries": "estimated_from_text_length" if estimated else "provider_word_boundaries",
                        "requires_manual_timing_review": estimated,
                        "timestamps_sha256": timestamps["sha256"] if not estimated else None,
                        "note": ("ESTIMATED phrase timing spread over the measured narration duration; not synchronized "
                                 "to the voice. A person must check it before approval.") if estimated else
                                ("Phrase cues whose start and end come from xAI character timestamps saved with the "
                                 "speech job, validated against the spoken text.")},
             "style": dict(style), "style_sha256": sha256_bytes(canonical(style)),
             "cues": cues, "sidecars": {"srt_sha256": None, "vtt_sha256": None}}
    track["sidecars"] = {"srt_sha256": sha256_bytes(srt(track)), "vtt_sha256": sha256_bytes(vtt(track))}
    identity = {k: v for k, v in track.items() if k != "caption_id"}
    track["caption_id"] = "cap-" + sha256_bytes(canonical(identity))[:24]
    validate_track(track)
    return track


def validate_track(track):
    """Schema, cue rules, sidecar hashes and the content-derived ID of a stored track."""
    try:
        reject_secrets(track)
    except NetworkError:
        raise NetworkError("captions_corrupt", "The caption track contains credential-like data.") from None
    if not isinstance(track, dict) or next(_validator().iter_errors(track), None) is not None:
        raise NetworkError("captions_corrupt", "The caption track does not match its contract.")
    timing = load_config()["timing"]
    validate_cues(track["cues"], track["source"]["narration_duration_ms"], timing)
    for cue in track["cues"]:
        if " ".join(cue["lines"]) != cue["text"] or len(cue["lines"]) > track["style"]["max_lines"] \
                or any(len(line) > track["style"]["max_chars_per_line"] for line in cue["lines"]):
            raise NetworkError("captions_corrupt", "A caption cue's lines do not match its text or the style.")
        if track["style"]["burn_in"]:
            check_characters(cue["text"])
    estimated = track["timing"]["method"] == METHODS["estimated"]
    if estimated != track["timing"]["requires_manual_timing_review"] or track["timing"]["word_synchronized"]:
        raise NetworkError("captions_corrupt", "Estimated timing must be labelled for review and never synchronized.")
    if sha256_bytes(canonical(track["style"])) != track["style_sha256"]:
        raise NetworkError("captions_corrupt", "The caption style hash does not match.")
    if (sha256_bytes(srt(track)) != track["sidecars"]["srt_sha256"]
            or sha256_bytes(vtt(track)) != track["sidecars"]["vtt_sha256"]):
        raise NetworkError("captions_corrupt", "Caption sidecar hashes do not match the cues.")
    identity = {k: v for k, v in track.items() if k != "caption_id"}
    if track["caption_id"] != "cap-" + sha256_bytes(canonical(identity))[:24]:
        raise NetworkError("captions_corrupt", "The caption ID does not match its content.")
    return track


def files_for(track):
    """{name: bytes} of the three stored files, deterministic from the track."""
    return {"track.json": canonical(track) + b"\n", "captions.srt": srt(track), "captions.vtt": vtt(track)}


def track_from_bytes(data):
    try:
        track = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError, AttributeError):
        raise NetworkError("captions_corrupt", "The caption track is unreadable.") from None
    validate_track(track)
    if files_for(track)["track.json"] != data:
        raise NetworkError("captions_corrupt", "The caption track is not in its canonical form.")
    return track


# ---------------------------------------------------------------- managed storage

def base(root=None):
    return Path(root if root is not None else ROOT) / "runtime" / "captions"


def folder(caption_id, root=None):
    if not isinstance(caption_id, str) or not CAPTION_ID.match(caption_id):
        raise NetworkError("invalid_caption_id", "Caption IDs look like cap- followed by 24 hex characters.")
    return base(root) / caption_id


def _write_once(target, data):
    if target.exists():
        if target.is_symlink() or target.read_bytes() != data:
            raise NetworkError("captions_tampered", "A stored caption file differs from its track.")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except FileExistsError:
        raise NetworkError("captions_tampered", "A caption file appeared while it was being written.") from None
    except OSError:
        raise NetworkError("captions_storage_failed", "Could not save the caption files.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save(track, root=None):
    """Write the three files once (sidecars first, track last: the track names verified files)."""
    validate_track(track)
    files = files_for(track)
    target = folder(track["caption_id"], root)
    for name in ("captions.srt", "captions.vtt", "track.json"):
        _write_once(target / name, files[name])
    return target


def load(caption_id, root=None):
    """(track, {name: bytes}) after re-checking every stored file against the track."""
    path = folder(caption_id, root)
    data = {}
    for name in FILES:
        file = path / name
        try:
            data[name] = file.read_bytes() if file.is_file() and not file.is_symlink() else None
        except OSError:
            data[name] = None
    if data["track.json"] is None:
        raise NetworkError("captions_not_found", "No caption track with this ID.")
    try:
        track = track_from_bytes(data["track.json"])
    except NetworkError:
        raise NetworkError("captions_tampered", "The stored caption track changed or is invalid.") from None
    if track["caption_id"] != caption_id:
        raise NetworkError("captions_tampered", "The stored caption track belongs to another ID.")
    expected = files_for(track)
    for name in ("captions.srt", "captions.vtt"):
        if data[name] != expected[name]:
            raise NetworkError("captions_tampered", f"{name} is missing or differs from the caption track.")
    return track, data


def list_tracks(root=None):
    rows = []
    if base(root).is_dir():
        for path in sorted(base(root).iterdir())[:500]:
            if CAPTION_ID.match(path.name):
                try:
                    track, _ = load(path.name, root)
                    rows.append({"caption_id": track["caption_id"], "speech_job_id": track["source"]["speech_job_id"],
                                 "timing": track["timing"]["method"], "cues": len(track["cues"]),
                                 "requires_manual_timing_review": track["timing"]["requires_manual_timing_review"]})
                except NetworkError as error:
                    rows.append({"caption_id": path.name, "error": error.code})
    return rows


def prepare(speech_job_id, *, timing, script=None, root=None, config_root=None):
    """Offline. Builds, validates and stores a track; returns it (an identical track is simply reused)."""
    from . import speech_jobs
    wav, record = speech_jobs.managed_audio(speech_job_id, root)        # completed + hash-checked
    if script is None:
        if record["script"]["source"] != "production":
            raise NetworkError("caption_script_required", "This speech job was made from a script file; pass the same "
                               "file with --script.")
        script = speech_jobs.production_script(record["script"]["production_id"], root)
    timestamps = speech_jobs.timestamps(speech_job_id, root) if timing == "provider" else None
    track = build_track(script, record, wav, method=timing, timestamps=timestamps,
                        config=load_config(config_root or ROOT))
    save(track, root)
    return track


def view(track):
    out = {k: track[k] for k in ("caption_id", "language", "source", "timing", "style_sha256", "sidecars", "notice",
                                 "publishable")}
    out["cues"] = [{"index": c["index"], "start": c["start_ms"] / 1000, "end": c["end_ms"] / 1000, "lines": c["lines"]}
                   for c in track["cues"]]
    out["next"] = ["python -m vicekrack video-production-start --production PRODUCTION_ID --speech "
                   f"{track['source']['speech_job_id']} --captions {track['caption_id']}"]
    if track["timing"]["requires_manual_timing_review"]:
        out["warning"] = ("ESTIMATED timing: not synchronized to the voice. Watch the preview and check every cue "
                          "before approving; approval requires acknowledging the needs_review result.")
    return out


# ---------------------------------------------------------------- burned-in rendering (Pillow)

def cue_images(track, work, modules):
    """One transparent 1080x1920 PNG per cue: [(file name, start_ms, end_ms)]. Text is measured, never shrunk
    below the style size, and overflow or unsupported characters stop the render."""
    from .preview import preview_text
    Image, ImageDraw, ImageFont, _ = modules
    style = track["style"]
    estimated = track["timing"]["requires_manual_timing_review"]
    check_layout(style, estimated)
    font = ImageFont.load_default(size=style["font_size"])
    label_font = ImageFont.load_default(size=style["estimated_label_size"])
    spacing = int(style["font_size"] * 1.35)
    label_height = int(style["estimated_label_size"] * 1.5) if estimated else 0
    out = []
    for cue in track["cues"]:
        lines = [preview_text(line) for line in cue["lines"]]        # raises on unsupported characters
        image = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        if any(draw.textlength(line, font=font) > style["max_line_pixels"] for line in lines):
            raise NetworkError("caption_overflow", "A caption line is wider than the caption box.")
        height = 2 * style["box_padding"] + label_height + len(lines) * spacing
        top = style["box_bottom"] - height
        draw.rounded_rectangle((style["box_left"], top, style["box_right"], style["box_bottom"]), radius=18,
                               fill=tuple(style["box_rgba"]))
        y = top + style["box_padding"]
        if estimated:
            label = preview_text(style["estimated_label"])
            width = draw.textlength(label, font=label_font)
            draw.text(((1080 - width) / 2, y), label, font=label_font, fill=style["estimated_label_color"])
            y += label_height
        for line in lines:
            width = draw.textlength(line, font=font)
            draw.text(((1080 - width) / 2, y), line, font=font, fill=style["text_color"])
            y += spacing
        name = f"caption-{cue['index']:03d}.png"
        image.save(Path(work) / name)
        out.append((name, cue["start_ms"], cue["end_ms"]))
    return out


def overlay_filter(images):
    """ffmpeg filter graph: each cue image shown only while start <= t < end (no two cues share a frame)."""
    parts, label = [], "0:v"
    for number, (_, start, end) in enumerate(images, start=1):
        target = "v%d" % number
        parts.append(f"[{label}][{number}:v]overlay=0:0:enable='gte(t,{start / 1000:.3f})*lt(t,{end / 1000:.3f})'[{target}]")
        label = target
    parts.append(f"[{label}]format=yuv420p[out]")
    return ";".join(parts)
