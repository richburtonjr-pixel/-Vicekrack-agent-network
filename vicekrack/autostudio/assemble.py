"""Auto Studio assembly with FFmpeg: one finished 1080x1920, 30 fps MP4 from the scene clips, voices and music.

  intro card -> scene clips (scaled and cropped to 9:16, frame rate normalized, trimmed or held to their
  planned length) joined with cross-fades -> outro card with sources;
  one overlay track for captions (word-timed), scene titles and the on-screen labels;
  voices placed at their planned times, original music ducked under speech, loudness normalized.
"""

import json
from pathlib import Path

from ..errors import NetworkError
from .common import run_ffmpeg

W, H, FPS = 1080, 1920, 30
INK = (14, 16, 23)
CROWN = (182, 156, 255)
AMBER = (255, 190, 85)


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
        return Image, ImageDraw, ImageFont
    except ImportError:
        raise NetworkError("renderer_unavailable", "Install requirements-render.txt (Pillow) to assemble videos.") from None


def _font(ImageFont, size):
    return ImageFont.load_default(size=size)


def _wrap(draw, font, text, width, max_lines):
    lines, line = [], ""
    for word in text.split():
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=font) <= width or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines[:max_lines] if len(lines) <= max_lines else None


def _fit(draw, ImageFont, text, width, max_lines, size, minimum=24):
    for s in range(size, minimum - 1, -2):
        font = _font(ImageFont, s)
        lines = _wrap(draw, font, text, width, max_lines)
        if lines and all(draw.textlength(l, font=font) <= width for l in lines):
            return font, lines, s
    font = _font(ImageFont, minimum)
    return font, (_wrap(draw, font, text, width, 99) or [text])[:max_lines], minimum


def _crown(draw, cx, cy, scale, color, width=10):
    pts = [(-1.0, 0.55), (-1.1, -0.45), (-0.45, 0.05), (0, -0.75), (0.45, 0.05), (1.1, -0.45), (1.0, 0.55)]
    pts = [(cx + x * scale, cy + y * scale) for x, y in pts]
    draw.line(pts + [pts[0]], fill=color, width=width, joint="curve")


def card(path, *, kind, title, subtitle, lines, band):
    Image, ImageDraw, ImageFont = _pil()
    image = Image.new("RGB", (W, H), INK)
    draw = ImageDraw.Draw(image)
    for y in range(H):                                            # a soft violet glow from the top
        shade = max(0.0, 1 - y / 1100)
        draw.line([(0, y), (W, y)], fill=(int(14 + 30 * shade), int(16 + 16 * shade), int(23 + 60 * shade)))
    _crown(draw, W // 2, 520 if kind == "intro" else 360, 110, CROWN, 14)
    font = _font(ImageFont, 66)
    word = "VICEKRACK"
    draw.text(((W - draw.textlength(word, font=font)) / 2, 650 if kind == "intro" else 490), word, font=font,
              fill="white", stroke_width=2, stroke_fill="white")
    if kind == "intro":
        f, wrapped, size = _fit(draw, ImageFont, title, 900, 4, 76, 40)
        y = 820
        for line in wrapped:
            draw.text(((W - draw.textlength(line, font=f)) / 2, y), line, font=f, fill="white", stroke_width=1,
                      stroke_fill="white")
            y += int(size * 1.25)
        sub = _font(ImageFont, 34)
        draw.text(((W - draw.textlength(subtitle, font=sub)) / 2, y + 40), subtitle, font=sub, fill=CROWN)
    else:
        sub = _font(ImageFont, 38)
        draw.text(((W - draw.textlength(subtitle, font=sub)) / 2, 620), subtitle, font=sub, fill=CROWN)
        y = 740
        for text in lines:
            f, wrapped, size = _fit(draw, ImageFont, text, 900, 3, 34, 24)
            for line in wrapped:
                draw.text(((W - draw.textlength(line, font=f)) / 2, y), line, font=f, fill=(220, 224, 232))
                y += int(size * 1.3)
            y += 22
    if band:
        _band(draw, ImageFont, band)
    image.save(path)


def _band(draw, ImageFont, text):
    draw.rounded_rectangle((50, 70, W - 50, 170), radius=22, fill=AMBER)
    f, wrapped, _ = _fit(draw, ImageFont, text, W - 160, 1, 40, 22)
    draw.text(((W - draw.textlength(wrapped[0], font=f)) / 2, 98), wrapped[0], font=f, fill=INK, stroke_width=1,
              stroke_fill=INK)


def overlay_frames(folder, timeline, script, *, band, total, intro, outro_start, speaker_labels):
    """Render the overlay track: one transparent PNG per interval in which nothing on top changes."""
    Image, ImageDraw, ImageFont = _pil()
    cues = caption_cues(timeline, script)
    titles = [(seg["start"] + 0.3, min(seg["start"] + 3.0, seg["start"] + seg["duration"] - 0.5),
               script["scenes"][seg["scene"]]["on_screen_text"])
              for i, seg in enumerate(timeline["segments"])
              if i == 0 or timeline["segments"][i - 1]["scene"] != seg["scene"]]
    points = {0.0, intro, outro_start, total}
    for start, end, _ in titles:
        points.update((start, end))
    for cue in cues:
        points.update((cue["start"], cue["end"]))
    points = sorted(p for p in points if 0 <= p <= total)
    entries, names = [], {}
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        if b - a < 0.01:
            continue
        mid = (a + b) / 2
        inside = intro <= mid < outro_start
        cue = next((c for c in cues if c["start"] <= mid < c["end"]), None) if inside else None
        title = next((t for t in titles if t[0] <= mid < t[1]), None) if inside else None
        key = (inside, cue["index"] if cue else None, title[2] if title else None)
        fresh = key not in names
        name = names.setdefault(key, "ov-%04d.png" % len(names))
        if fresh:
            image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            if inside:
                if band:
                    _band(draw, ImageFont, band)
                small = _font(ImageFont, 24)
                label = "AI-GENERATED VISUALS"
                tw = draw.textlength(label, font=small)
                draw.rounded_rectangle((W - tw - 90, H - 120, W - 50, H - 72), radius=14, fill=(0, 0, 0, 150))
                draw.text((W - tw - 70, H - 110), label, font=small, fill=(235, 235, 240))
            if title:
                f, wrapped, size = _fit(draw, ImageFont, title[2].upper(), 880, 2, 64, 36)
                y = 230 if band else 160
                for line in wrapped:
                    tw = draw.textlength(line, font=f)
                    draw.rounded_rectangle(((W - tw) / 2 - 26, y - 10, (W + tw) / 2 + 26, y + size + 14), radius=14,
                                           fill=(14, 16, 23, 200))
                    draw.text(((W - tw) / 2, y), line, font=f, fill="white", stroke_width=1, stroke_fill="white")
                    y += size + 34
            if cue:
                _caption(draw, ImageFont, cue, speaker_labels)
            image.save(Path(folder) / name)
        entries.append((name, b - a, key))
    listing = ["ffconcat version 1.0"]
    for name, seconds, _ in entries:
        listing += [f"file '{name}'", f"duration {seconds:.4f}"]
    listing.append(f"file '{entries[-1][0]}'")
    (Path(folder) / "overlay.ffconcat").write_text("\n".join(listing) + "\n", encoding="utf-8")
    return cues


def _caption(draw, ImageFont, cue, speaker_labels):
    f, wrapped, size = _fit(draw, ImageFont, cue["text"], 900, 2, 60, 40)
    heights = (size + 18) * len(wrapped) + (44 if speaker_labels and cue["speaker"] else 0)
    top = 1400 - heights
    widest = max(draw.textlength(l, font=f) for l in wrapped)
    draw.rounded_rectangle(((W - widest) / 2 - 34, top - 26, (W + widest) / 2 + 34, 1400 + 22), radius=24,
                           fill=(8, 10, 16, 215))
    y = top
    if speaker_labels and cue["speaker"]:
        small = _font(ImageFont, 30)
        name = cue["speaker"].upper()
        draw.text(((W - draw.textlength(name, font=small)) / 2, y - 6), name, font=small, fill=CROWN)
        y += 44
    for line in wrapped:
        draw.text(((W - draw.textlength(line, font=f)) / 2, y), line, font=f, fill="white", stroke_width=2,
                  stroke_fill=(0, 0, 0))
        y += size + 18


def caption_cues(timeline, script, max_chars=34, max_words=7):
    """Phrase cues from each spoken line's word times, in absolute video time."""
    cues = []
    for seg in timeline["segments"]:
        scene = script["scenes"][seg["scene"]]
        for placed in seg["lines"]:
            line = scene["lines"][placed["line"]]
            words = line["timing"]["words"]
            group = []
            for i, (word, start, end) in enumerate(words):
                group.append((word, start, end))
                text = " ".join(w for w, _, _ in group)
                last = i == len(words) - 1
                if last or len(group) >= max_words or len(text) >= max_chars or word.endswith((".", "?", "!", ",")) \
                        and len(group) >= 3:
                    cues.append({"text": text, "start": round(placed["at"] + group[0][1], 3),
                                 "end": round(placed["at"] + group[-1][2], 3), "speaker": line["speaker"]})
                    group = []
    for i, cue in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else cue["end"] + 0.6
        cue["end"] = round(min(max(cue["end"] + 0.35, cue["start"] + 0.5), max(nxt, cue["start"] + 0.2)), 3)
        cue["index"] = i
    return cues


def assemble(work, *, timeline, script, clips, voices, music, title, subtitle, band, sources_lines, mix,
             speaker_labels, native_audio=False):
    """Build work/final.mp4. clips: segment index -> clip path; voices: (scene, line) -> wav path."""
    work = Path(work)
    t = timeline
    intro, outro = t["intro_seconds"], t["outro_seconds"]
    total = t["total_seconds"]
    outro_start = round(total - outro, 3)
    card(work / "intro.png", kind="intro", title=title, subtitle=subtitle, lines=[], band=band)
    card(work / "outro.png", kind="outro", title=title, subtitle="Sources", lines=sources_lines, band=band)
    # 1. Normalized pieces: intro, each clip at its planned length, outro. Every piece fades through dark
    #    at its edges (a "dip" transition); pieces are then joined back to back, which keeps memory flat
    #    however many scenes there are.
    fade = t.get("fade_seconds", 0.25)
    norm = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1,format=yuv420p"
    encode = ["-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-an"]

    def edges(seconds, fade_in=True, fade_out=True):
        parts = []
        if fade_in:
            parts.append(f"fade=t=in:st=0:d={fade:.3f}")
        if fade_out:
            parts.append(f"fade=t=out:st={max(0.0, seconds - fade):.3f}:d={fade:.3f}")
        return ("," + ",".join(parts)) if parts else ""

    run_ffmpeg(["-y", "-loop", "1", "-i", "intro.png", "-t", f"{intro:.3f}", "-vf",
                norm + edges(intro, fade_in=False), *encode, "p-intro.mp4"], work)
    run_ffmpeg(["-y", "-loop", "1", "-i", "outro.png", "-t", f"{outro:.3f}", "-vf",
                norm + edges(outro, fade_out=False), *encode, "p-outro.mp4"], work)
    pieces = ["p-intro.mp4"]
    for i, seg in enumerate(t["segments"]):
        name = f"p-{i:03d}.mp4"
        run_ffmpeg(["-y", "-i", str(clips[i]), "-vf",
                    f"{norm},tpad=stop_mode=clone:stop_duration=3,trim=duration={seg['duration']:.3f},"
                    f"setpts=PTS-STARTPTS{edges(seg['duration'])}", *encode, name], work)
        pieces.append(name)
    pieces.append("p-outro.mp4")
    (work / "pieces.ffconcat").write_text("ffconcat version 1.0\n" + "".join(f"file '{p}'\n" for p in pieces),
                                          encoding="utf-8")
    run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", "pieces.ffconcat", "-c", "copy", "joined.mp4"], work)
    # 2. The overlay track (captions, scene titles, labels) on top, in one pass.
    cues = overlay_frames(work, t, script, band=band, total=total, intro=intro, outro_start=outro_start,
                          speaker_labels=speaker_labels)
    run_ffmpeg(["-y", "-i", "joined.mp4", "-f", "concat", "-safe", "0", "-i", "overlay.ffconcat", "-filter_complex",
                f"[0:v]trim=duration={total:.3f},setpts=PTS-STARTPTS[main];[1:v]fps=30,format=rgba[ov];"
                "[main][ov]overlay=0:0:format=auto:eof_action=pass:shortest=1,format=yuv420p[vout]",
                "-map", "[vout]", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-an", "picture.mp4"], work)
    # 3. Audio: voices at their times; music ducked under them; one loudness-normalized stereo mix.
    a_inputs, a_graph, labels = [], [], []
    n = 0
    for seg in t["segments"]:
        for placed in seg["lines"]:
            a_inputs += ["-i", str(voices[(seg["scene"], placed["line"])])]
            delay = int(round(placed["at"] * 1000))
            a_graph.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={delay}|{delay}[v{n}]")
            labels.append(f"[v{n}]")
            n += 1
    a_graph.append("".join(labels) + f"amix=inputs={n}:normalize=0:dropout_transition=0,apad,"
                   f"atrim=duration={total:.3f}[voice]")
    if music:
        a_inputs += ["-i", str(music)]
        a_graph.append("[voice]asplit=2[voice1][key]")
        a_graph.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration={total:.3f},"
                       f"volume={mix['music_db']}dB[bed]")
        a_graph.append("[bed][key]sidechaincompress=threshold=0.015:ratio=10:attack=15:release=350:makeup=1[ducked]")
        a_graph.append("[voice1][ducked]amix=inputs=2:normalize=0[mixed]")
        last_a = "mixed"
    else:
        last_a = "voice"
    a_graph.append(f"[{last_a}]loudnorm=I={mix['loudness_lufs']}:TP={mix['true_peak_db']}:LRA=11,"
                   f"aresample=48000,atrim=duration={total:.3f}[aout]")
    run_ffmpeg(["-y", *a_inputs, "-filter_complex", ";".join(a_graph), "-map", "[aout]", "-c:a", "pcm_s16le",
                "-ar", "48000", "mix.wav"], work)
    # 4. Mux, thumbnail and scene stills.
    run_ffmpeg(["-y", "-i", "picture.mp4", "-i", "mix.wav", "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{total:.3f}", "-movflags", "+faststart",
                "-metadata", f"title={title}", "final.mp4"], work)
    first = t["segments"][0]
    run_ffmpeg(["-y", "-ss", f"{first['start'] + min(2.0, first['duration'] / 2):.3f}", "-i", "final.mp4",
                "-frames:v", "1", "-vf", "scale=540:960", "-q:v", "3", "thumb.jpg"], work)
    for i, seg in enumerate(t["segments"]):
        run_ffmpeg(["-y", "-ss", f"{min(seg['duration'] / 2, 2.0):.3f}", "-i", str(clips[i]), "-frames:v", "1",
                    "-vf", "scale=270:480:force_original_aspect_ratio=increase,crop=270:480", "-q:v", "4",
                    f"scene-{i:03d}.jpg"], work)
    (work / "captions.json").write_text(json.dumps(cues, indent=1), encoding="utf-8")
    return work / "final.mp4"
