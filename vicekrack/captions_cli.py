"""Step 47 commands: narration-aligned caption tracks. All offline; nothing here makes a request."""

import argparse
import json
from pathlib import Path

from . import captions
from .errors import NetworkError

COMMANDS = {
    "captions-prepare": "Build and store a caption track from a COMPLETED speech job (offline, no cost)",
    "captions-inspect": "Show a caption track: cues, timing method, bindings, style and sidecar hashes (read-only)",
    "captions-list": "List stored caption tracks (read-only)",
}


def parser():
    root = argparse.ArgumentParser(description="Narration-aligned captions; offline, never a paid request")
    commands = root.add_subparsers(dest="command", required=True)
    for name, text in COMMANDS.items():
        command = commands.add_parser(name, help=text, description=text)
        if name == "captions-prepare":
            command.add_argument("--speech", required=True, metavar="SPEECH_JOB_ID")
            command.add_argument("--timing", required=True, choices=["provider", "estimated"],
                                 help="provider: the xAI character timestamps saved with the job (speech-prepare "
                                      "--with-timestamps); estimated: spread by phrase length, labelled for review")
            command.add_argument("--script", type=Path,
                                 help="the ShortScript file the speech job was prepared from (production jobs use "
                                      "the production's saved script)")
        elif name == "captions-inspect":
            command.add_argument("caption_id")
            command.add_argument("--sidecar", choices=["srt", "vtt"], help="also print that sidecar's text")
    return root


def run(args):
    if args.command == "captions-prepare":
        from .speech_jobs import read_script_file
        script = read_script_file(args.script) if args.script else None
        return captions.view(captions.prepare(args.speech, timing=args.timing, script=script))
    if args.command == "captions-list":
        return {"captions": captions.list_tracks()}
    track, files = captions.load(args.caption_id)
    out = captions.view(track)
    if args.sidecar:
        out["sidecar_text"] = files[f"captions.{args.sidecar}"].decode("utf-8")
    return out


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result, code = run(args), 0
    except NetworkError as error:
        result, code = {"error": {"code": error.code, "message": error.message}}, 1
    except (OSError, ValueError, UnicodeError):
        result, code = {"error": {"code": "captions_storage_error", "message": "Cannot read input or caption storage."}}, 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code
