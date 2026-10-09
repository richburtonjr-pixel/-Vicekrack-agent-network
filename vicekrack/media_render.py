"""Local, hash-checked media rendering. Source sound is always discarded."""
from pathlib import Path

from .errors import NetworkError
from .media_manifest import validate_media_manifest, load_asset


def inspect_video(path):
    from .quality import probe_media, ProbeFailed, ProbeUnavailable
    try:
        info = probe_media(path)
        if not (16 <= info["width"] <= 4096 and 16 <= info["height"] <= 4096
                and 0 < info["duration"] <= 120 and 0 < info["fps"] <= 120):
            raise ValueError
        return info
    except (ProbeFailed, ProbeUnavailable, ValueError):
        raise NetworkError("media_probe_failed", "Media could not be decoded within supported limits.") from None


def prepare_media(plan, manifest, root):
    validate_media_manifest(manifest)
    if root is None or manifest["plan_id"] != plan["plan_id"]:
        raise NetworkError("media_plan_mismatch", "Media requires its matching plan and a local root.")
    assets = {asset["asset_id"]: (asset, load_asset(root, asset)[1]) for asset in manifest["assets"]}
    for scene, row in zip(plan["scenes"], manifest["assignments"]):
        duration = scene["beat"]["end_seconds"] - scene["beat"]["start_seconds"]
        supplied = row.get("image_duration_seconds") if assets[row["asset_id"]][0]["kind"] == "image" else row["trim"]["end_seconds"] - row["trim"]["start_seconds"]
        if abs(duration - supplied) > .001:
            raise NetworkError("media_duration_mismatch", "Each assignment must exactly cover its scene duration.")
    return assets


def render_scene(plan, scene, row, asset_and_data, work, modules):
    from .preview import invoke, draw_text, FPS
    Image, ImageDraw, ImageFont, exe = modules
    asset, data = asset_and_data
    number = scene["index"]
    source = work / f"source-{number}.{'mp4' if asset['kind'] == 'video' else 'img'}"
    source.write_bytes(data)  # Encoder reads the already hashed snapshot, not a mutable user path.
    duration = scene["beat"]["end_seconds"] - scene["beat"]["start_seconds"]
    if asset["kind"] == "video":
        info = inspect_video(source)
        if info["duration"] + .04 < row["trim"]["end_seconds"]:
            raise NetworkError("media_trim_invalid", "Trim extends beyond the available video.")
        inputs = ["-ss", str(row["trim"]["start_seconds"]), "-i", source.name]
    else:
        try:
            with Image.open(source) as image:
                if image.width * image.height > 20000000:
                    raise ValueError
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise NetworkError("media_image_invalid", "Image cannot be decoded within supported limits.") from None
        inputs = ["-loop", "1", "-framerate", str(FPS), "-i", source.name]
    y = {"top": "0", "center": "(ih-oh)/2", "bottom": "ih-oh"}[row["anchor"]]
    if row["fit"] == "cover":
        fit = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:(iw-ow)/2:{y}"
    else:
        y = {"top": "0", "center": "(oh-ih)/2", "bottom": "oh-ih"}[row["anchor"]]
        fit = f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:{y}:black"
    overlay = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    accent = "#ffbe55" if plan["blocked_for_production"] else "#59d6c1"
    draw.rounded_rectangle((60, 60, 1020, 168), radius=20, fill=accent)
    label = "DRAFT - NOT FOR PRODUCTION" if plan["blocked_for_production"] else "LOCAL PREVIEW - NOT FOR PUBLISHING"
    draw_text(draw, ImageFont, label, (88, 88, 905, 70), 36, "#0c1425")
    draw.rectangle((45, 1420, 1035, 1880), fill=(5, 12, 25, 225))
    draw_text(draw, ImageFont, scene["beat"]["on_screen_text"] or plan["script"]["title"], (75, 1450, 930, 180), 54, "white")
    draw_text(draw, ImageFont, "ILLUSTRATIVE MEDIA / RIGHTS NOT VERIFIED", (75, 1650, 930, 70), 28, accent)
    draw_text(draw, ImageFont, " | ".join(plan["script"].get("disclosures", [])), (75, 1740, 930, 120), 26, "white")
    overlay.save(work / f"overlay-{number}.png")
    invoke(exe, [*inputs, "-loop", "1", "-i", f"overlay-{number}.png", "-filter_complex",
                 f"[0:v]{fit},setsar=1,fps={FPS}[base];[base][1:v]overlay=0:0,format=yuv420p[out]",
                 "-map", "[out]", "-frames:v", str(int(duration * FPS)), "-an", "-map_metadata", "-1",
                 "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2", f"scene-{number}.mp4"], work)
    invoke(exe, ["-i", f"scene-{number}.mp4", "-frames:v", "1", f"scene-{number}.png"], work)
    source.unlink()
    (work / f"overlay-{number}.png").unlink()
