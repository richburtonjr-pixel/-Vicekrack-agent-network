"""Explicit commands for reviewed, paid video requests and offline assembly."""
import argparse
import json
from pathlib import Path

from . import video_jobs as jobs
from .errors import NetworkError
from .orchestrator import read_json


def main(argv=None):
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
    _workflow_commands(commands)
    args = parser.parse_args(argv)
    try:
        if args.command.startswith("video-production-"):
            result = _run_workflow(args)
        elif args.command == "video-prepare":
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
        if args.command.startswith("video-production-"):          # fixed codes and messages only
            print(json.dumps({"error": {"code": error.code, "message": error.message}}, indent=2))
            return 1
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error":{"code":"invalid_video_input"}}')
    return 1


# ---------------------------------------------------------------- Step 44 workflow commands
WORKFLOW_HELP = {
    "video-production-start": "Start a workflow from an approved selection or a completed production (no paid request)",
    "video-production-inspect": "Show a workflow, its scenes, outputs and the next explicit actions",
    "video-production-list": "List saved workflows",
    "video-production-resume": "Continue from the first incomplete stage (status/downloads only with --allow-network)",
    "video-production-submit": "Submit ONE scene's paid Grok job; needs that job's own consent phrase",
    "video-production-retry-scene": "Replace a failed/expired scene job with a new prepared job (no request)",
    "video-production-export": "Export through the existing preview-export gates (publishable stays false)",
    "video-production-demo": "Run the whole workflow offline with mocked provider responses and synthetic media",
}


def _workflow_commands(commands):
    for name, text in WORKFLOW_HELP.items():
        command = commands.add_parser(name, help=text, description=text)
        if name not in ("video-production-start", "video-production-list", "video-production-demo"):
            command.add_argument("workflow_id")
        if name == "video-production-start":
            command.add_argument("--production")
            command.add_argument("--selection")
            command.add_argument("--record")
            command.add_argument("--model", default=jobs.DEFAULT_MODEL)
            command.add_argument("--resolution", choices=["480p", "720p", "1080p"], default="720p")
            command.add_argument("--audio-mode", choices=["none", "generated"], default="none")
            command.add_argument("--allow-draft-preview", action="store_true")
        if name == "video-production-resume":
            command.add_argument("--allow-network", action="store_true",
                                 help="allow bounded status checks and downloads (never a new paid submission)")
        if name == "video-production-submit":
            command.add_argument("--scene", type=int, required=True)
            command.add_argument("--consent", required=True, help="exactly paid-generate:JOB_ID for this scene's job")
            command.add_argument("--allow-network", action="store_true")
            command.add_argument("--retry-uncertain", action="store_true")
            command.add_argument("--acknowledge-duplicate-billing", action="store_true",
                                 help="required with --retry-uncertain: the first request may already have been billed")
        if name == "video-production-retry-scene":
            command.add_argument("--scene", type=int, required=True)
            command.add_argument("--model")
            command.add_argument("--resolution", choices=["480p", "720p", "1080p"])
            command.add_argument("--audio-mode", choices=["none", "generated"])
        if name == "video-production-export":
            command.add_argument("--purpose", required=True, choices=["review_copy", "approved_preview"])
        if name == "video-production-demo":
            command.add_argument("--output", type=Path, help="demo folder (default: runtime/video-production-demo/RUN)")


def _run_workflow(args):
    from .video_production import VideoProduction
    if args.command == "video-production-demo":
        from .video_production_demo import run_demo
        return run_demo(args.output)
    flow = VideoProduction()
    if args.command == "video-production-start":
        return flow.start(production_id=args.production, selection_run_id=args.selection, record_id=args.record,
                          model=args.model, resolution=args.resolution, audio_mode=args.audio_mode,
                          allow_draft_preview=args.allow_draft_preview)
    if args.command == "video-production-list":
        return {"workflows": flow.list()}
    if args.command == "video-production-inspect":
        return flow.inspect(args.workflow_id)
    if args.command == "video-production-resume":
        return flow.resume(args.workflow_id, allow_network=args.allow_network)
    if args.command == "video-production-submit":
        return flow.submit(args.workflow_id, args.scene, consent=args.consent, allow_network=args.allow_network,
                           retry_uncertain=args.retry_uncertain,
                           acknowledge_duplicate_billing=args.acknowledge_duplicate_billing)
    if args.command == "video-production-retry-scene":
        return flow.retry_scene(args.workflow_id, args.scene, model=args.model, resolution=args.resolution,
                                audio_mode=args.audio_mode)
    return flow.export(args.workflow_id, purpose=args.purpose)
