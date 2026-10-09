"""Bounded local previews, silent by default with optional local narration.

No external assets, provider clients or shell commands.

Step 36: the manifest is `preview_render` 1.1. Every scene poster carries `poster_sha256`
and `poster_bytes`, and the video carries `video_bytes` beside `video_sha256`. Before the
package is published (renamed into place), every named file is re-read and must exist
inside the package, be a regular file within its size limit and match its recorded hash
(`artifact_binding.check_package`); otherwise nothing is published."""
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from .errors import NetworkError
from .narration import load_narration
from .orchestrator import ROOT, read_json
from .artifact_binding import check_package, sha256_bytes
from .persistence import reject_secrets
from .scene_plan import validate_scene_plan

FPS = 24
LOCAL = {"text_card", "motion_graphics"}


def dependencies():
    try:
        from PIL import Image, ImageDraw, ImageFont
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return Image, ImageDraw, ImageFont, exe
    except (ImportError, RuntimeError, OSError):
        raise NetworkError("renderer_unavailable", "Install requirements-render.txt to enable local previews.") from None


def preview_text(value):
    # Built-in font: English preview only. Never silently replace unsupported glyphs.
    text = value.translate(str.maketrans({"\u2018":"'", "\u2019":"'", "\u201c":'"', "\u201d":'"',
                                         "\u2013":"-", "\u2014":"-", "\u2026":"...", "\u00a0":" "}))
    if any(ord(c) < 32 and c not in "\n\t" or ord(c) > 126 for c in text):
        raise NetworkError("unsupported_preview_text", "Preview font currently supports English printable ASCII and common smart punctuation.")
    return " ".join(text.split())


def preflight(plan, allow_draft=False):
    validate_scene_plan(plan)
    if type(allow_draft) is not bool:
        raise NetworkError("invalid_preview_option", "Draft preview choice must be boolean.")
    if plan["blocked_for_production"] and not allow_draft:
        raise NetworkError("draft_preview_required", "Explicitly allow a watermarked draft preview.")
    if any(scene["selected_method"] not in LOCAL for scene in plan["scenes"]):
        raise NetworkError("unsupported_render_method", "Replan with local text_card/motion_graphics capabilities.")
    if plan["script"]["language"].split("-")[0] != "en":
        raise NetworkError("unsupported_preview_language", "This preview renderer currently supports English.")
    for text in [plan["script"]["title"], *plan["script"].get("disclosures", [])]:
        preview_text(text)
    for scene in plan["scenes"]:
        preview_text(scene["beat"]["narration"])
        preview_text(scene["beat"]["on_screen_text"] or "")


def draw_text(draw, font_module, text, box, size, color):
    text = preview_text(text)
    x, y, width, height = box
    for font_size in range(size, 19, -2):
        font = font_module.load_default(size=font_size)
        lines, line = [], ""
        for word in text.split():
            candidate = (line + " " + word).strip()
            if draw.textlength(candidate, font=font) <= width:
                line = candidate
            else:
                if line: lines.append(line)
                line = word
        if line: lines.append(line)
        spacing = int(font_size * 1.35)
        if len(lines)*spacing <= height and all(draw.textlength(line,font=font) <= width for line in lines):
            for line in lines:
                draw.text((x,y),line,font=font,fill=color)
                y += spacing
            return
    raise NetworkError("preview_text_overflow", "Text does not fit the preview layout; shorten the source script.")


def make_card(plan, scene, path, modules, narrated=False):
    Image, ImageDraw, ImageFont, _ = modules
    image = Image.new("RGB", (1080,1920), "#0c1425")
    draw = ImageDraw.Draw(image)
    accent = "#ffbe55" if plan["blocked_for_production"] else "#59d6c1"
    draw.rounded_rectangle((60,60,1020,168),radius=20,fill=accent)
    label = "DRAFT - NOT FOR PRODUCTION" if plan["blocked_for_production"] else "LOCAL PREVIEW - NOT FOR PUBLISHING"
    draw_text(draw,ImageFont,label,(88,88,905,70),36,"#0c1425")
    subtitle = "VICEKRACK / LOCAL NARRATION STORYBOARD" if narrated else "VICEKRACK / SILENT STORYBOARD"
    draw_text(draw,ImageFont,subtitle,(76,222,928,70),30,"#b9c7db")
    beat = scene["beat"]
    draw_text(draw,ImageFont,f"{scene['index']:02d} / {beat['beat'].upper()}",(76,330,928,90),44,accent)
    draw_text(draw,ImageFont,beat["on_screen_text"] or plan["script"]["title"],(76,465,928,400),82,"#ffffff")
    draw.line((76,925,1004,925),fill=accent,width=5)
    draw_text(draw,ImageFont,"NARRATION TEXT / NO GENERATED AUDIO",(76,990,928,70),28,"#b9c7db")
    draw_text(draw,ImageFont,beat["narration"],(76,1090,928,365),58,"#ffffff")
    disclosures = " | ".join(plan["script"].get("disclosures", []))
    if disclosures:
        draw_text(draw,ImageFont,disclosures,(76,1480,928,285),28,"#b9c7db")
    draw_text(draw,ImageFont,f"{beat['start_seconds']:g}-{beat['end_seconds']:g}s / {scene['selected_method']}",(76,1830,928,65),26,"#b9c7db")
    image.save(path,format="PNG")


def invoke(exe, arguments, cwd):
    try:
        subprocess.run([exe,"-hide_banner","-loglevel","error","-nostdin", *arguments], cwd=cwd,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=90, shell=False,
                       env={key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG") if key in os.environ},
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise NetworkError("render_timeout", "Local rendering timed out; no output was published.") from None
    except (OSError, subprocess.CalledProcessError):
        raise NetworkError("render_failed", "Local encoding failed; no output was published.") from None


def render_preview(plan, *, allow_draft=False, directory=None, narration=None, media=None, media_root=None):
    preflight(plan,allow_draft)
    from .media_render import prepare_media, render_scene
    media_assets = prepare_media(plan, media, media_root) if media is not None else None
    # Validate optional audio before any encoder launch, folder creation or staging.
    audio, wav = load_narration(narration) if narration is not None else (None, None)
    modules = dependencies()
    exe = modules[3]
    folder = (Path(directory) if directory is not None else ROOT / "runtime/previews").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    name = plan["plan_id"] + "-" + uuid4().hex
    target = folder / name
    reservation = folder / (name + ".lock")
    # Exclusive reservation serializes identical output names without overwriting artifacts.
    try:
        lock = reservation.open("x")
    except OSError:
        raise NetworkError("preview_exists", "Preview output name is already reserved.") from None
    try:
        if target.exists():
            raise NetworkError("preview_exists", "Preview output already exists.")
        with tempfile.TemporaryDirectory(prefix=".render-",dir=folder) as temp:
            work = Path(temp).resolve()
            if not work.is_relative_to(folder):
                raise NetworkError("render_failed", "Invalid local staging location.")
            for scene in plan["scenes"]:
                number = scene["index"]
                if media_assets is not None:
                    row = media["assignments"][number - 1]
                    render_scene(plan, scene, row, media_assets[row["asset_id"]], work, modules)
                    continue
                if audio is None:
                    make_card(plan,scene,work / f"scene-{number}.png",modules)
                else:
                    make_card(plan,scene,work / f"scene-{number}.png",modules,narrated=True)
                duration = scene["beat"]["end_seconds"] - scene["beat"]["start_seconds"]
                filters = "format=yuv420p"
                if scene["selected_method"] == "motion_graphics":
                    filters += f",fade=t=in:st=0:d=0.25,fade=t=out:st={duration-0.25}:d=0.25"
                invoke(exe,["-loop","1","-framerate",str(FPS),"-i",f"scene-{number}.png",
                            "-frames:v",str(int(duration*FPS)),"-vf",filters,"-an","-c:v","libx264",
                            "-preset","ultrafast","-crf","23","-threads","2",f"scene-{number}.mp4"],work)
            (work / "segments.txt").write_text("".join(f"file 'scene-{i}.mp4'\n" for i in range(1,5)),encoding="ascii")
            silent = "preview.mp4" if audio is None else "silent.mp4"
            invoke(exe,["-f","concat","-safe","1","-i","segments.txt","-c","copy","-an","-movflags","+faststart",silent],work)
            if audio is not None:
                # Metadata-free, silence-padded WAV staged locally; container metadata is also dropped.
                (work / "narration.wav").write_bytes(wav)
                invoke(exe,["-i",silent,"-i","narration.wav","-map","0:v:0","-map","1:a:0",
                            "-map_metadata","-1","-map_chapters","-1","-c:v","copy",
                            "-c:a","aac","-b:a","128k","-ar","48000","-movflags","+faststart","preview.mp4"],work)
            # Decode every frame before publishing, instead of trusting an encoder exit alone.
            invoke(exe,["-xerror","-i","preview.mp4","-f","null","-"],work)
            video = work / "preview.mp4"
            if not video.is_file() or video.stat().st_size < 100:
                raise NetworkError("invalid_render_output", "Encoder produced no usable video.")
            video_bytes = video.read_bytes()
            manifest = {"contract":"preview_render","version":"1.1","plan_id":plan["plan_id"],
                        "input_sha256":plan["input_sha256"],"preview_only":True,"publishable":False,
                        "source_blocked_for_production":plan["blocked_for_production"],
                        "width":1080,"height":1920,"fps":FPS,"duration_seconds":15,"audio_present":audio is not None,
                        "video":"preview.mp4","video_sha256":sha256_bytes(video_bytes),"video_bytes":len(video_bytes),
                        "limitations":(["silent storyboard"] if audio is None else
                                       ["user-supplied local narration padded with silence to 15 seconds",
                                        "no voice consent, rights or content verification of narration"])
                                      + ["narration displayed as text", "no word timing or lip sync",
                                         "no sourced or generated media", "no fact or rights verification"],
                        "scenes":[{"index":s["index"],"method":s["selected_method"],
                                   "start_seconds":s["beat"]["start_seconds"],"end_seconds":s["beat"]["end_seconds"],
                                   "poster":f"scene-{s['index']}.png",
                                   "poster_sha256":sha256_bytes((work/f"scene-{s['index']}.png").read_bytes()),
                                   "poster_bytes":(work/f"scene-{s['index']}.png").stat().st_size} for s in plan["scenes"]]}
            if audio is not None:
                manifest["audio"] = dict(audio)
            if media is not None:
                manifest["limitations"] = [item for item in manifest["limitations"] if item not in
                                            {"no sourced or generated media", "silent storyboard", "narration displayed as text"}]
                manifest["limitations"] += ["illustrative media; source audio muted; rights declared, not verified",
                    "media_manifest_sha256:" + sha256_bytes(json.dumps(media, sort_keys=True, allow_nan=False).encode())]
            reject_secrets(manifest)
            (work / "manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
            for path in [video,work/"manifest.json"]:
                with path.open("r+b") as stream: os.fsync(stream.fileno())
            for path in [work/"segments.txt", *work.glob("scene-*.mp4"), work/"silent.mp4", work/"narration.wav"]:
                path.unlink(missing_ok=True)
            # Every file the manifest names must be present, in bounds and match its hash before publication.
            if check_package(work, json.loads((work / "manifest.json").read_text(encoding="utf-8"))) is not None:
                raise NetworkError("invalid_render_output", "The preview package did not verify; nothing was published.")
            os.rename(work,target)
        return {"preview_file":str(target/"preview.mp4"),"manifest_file":str(target/"manifest.json"),
                "preview_only":True,"publishable":False,"source_blocked_for_production":plan["blocked_for_production"],
                "audio_present":audio is not None}
    except OSError:
        raise NetworkError("preview_storage_error", "Could not save the complete preview.") from None
    finally:
        lock.close()
        reservation.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Render a local watermarked storyboard preview (silent unless --narration is given)")
    parser.add_argument("command",choices=["render-preview"])
    parser.add_argument("plan",type=Path)
    parser.add_argument("--allow-draft-preview",action="store_true")
    parser.add_argument("--narration",type=Path,default=None,
                        help="Optional local 16-bit PCM WAV (mono/stereo, 8-48 kHz, max 15 s and 12 MB)")
    parser.add_argument("--media", type=Path)
    parser.add_argument("--media-root", type=Path)
    args=parser.parse_args()
    try:
        result=render_preview(read_json(args.plan),allow_draft=args.allow_draft_preview,narration=args.narration,
                              media=read_json(args.media) if args.media else None, media_root=args.media_root)
        print(json.dumps(result,indent=2))
        return 0
    except NetworkError as error:
        print(json.dumps({"error":{"code":error.code}}))
    except (OSError,ValueError,UnicodeError):
        print('{"error":{"code":"invalid_input_or_storage"}}')
    return 1
