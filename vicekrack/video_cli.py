"""Explicit commands for reviewed, paid video requests and offline assembly."""
import argparse
import json
from pathlib import Path

from . import video_jobs as jobs
from .errors import NetworkError
from .orchestrator import read_json


def main():
    parser = argparse.ArgumentParser(description="Explicit Grok video jobs; no automatic paid requests")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("video-prepare")
    prepare.add_argument("plan", type=Path)
    prepare.add_argument("scene", type=int)
    prepare.add_argument("--model", default=jobs.DEFAULT_MODEL)
    prepare.add_argument("--resolution", choices=["480p", "720p", "1080p"], default="720p")
    prepare.add_argument("--audio-mode", choices=["none", "generated"], default="none")
    assemble = commands.add_parser("video-manifest")
    assemble.add_argument("plan", type=Path)
    assemble.add_argument("job_ids", nargs=4)
    assemble.add_argument("--media-root", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    revise = commands.add_parser("video-render-production")
    revise.add_argument("production_id")
    revise.add_argument("--media", type=Path, required=True)
    revise.add_argument("--media-root", type=Path, required=True)
    for name in ("video-inspect", "video-submit", "video-status", "video-download"):
        command = commands.add_parser(name)
        command.add_argument("job_id")
        if name != "video-inspect":
            command.add_argument("--allow-network", action="store_true")
        if name == "video-submit":
            command.add_argument("--consent", required=True)
            command.add_argument("--retry-uncertain", action="store_true")
        if name == "video-download":
            command.add_argument("--media-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "video-prepare":
            result = jobs.prepare(read_json(args.plan), args.scene, model=args.model,
                                  resolution=args.resolution, audio_mode=args.audio_mode)
        elif args.command == "video-manifest":
            result = jobs.media_manifest(read_json(args.plan), args.job_ids, args.media_root)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2)
            result = {"media_manifest": str(args.output), "publishable": False}
        elif args.command == "video-render-production":
            from .media_production import render_production
            result = render_production(args.production_id, read_json(args.media), args.media_root)
        elif args.command == "video-inspect":
            result = jobs.inspect(args.job_id)
        elif args.command == "video-submit":
            result = jobs.submit(args.job_id, consent=args.consent, allow_network=args.allow_network,
                                 retry_uncertain=args.retry_uncertain)
        elif args.command == "video-status":
            result = jobs.status(args.job_id, allow_network=args.allow_network)
        else:
            result = jobs.download(args.job_id, allow_network=args.allow_network, media_root=args.media_root)
        # Signed media URLs remain in local storage, never in CLI output.
        print(json.dumps({k: v for k, v in result.items() if k != "download_url"}, indent=2))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error":{"code":"invalid_video_input"}}')
    return 1
