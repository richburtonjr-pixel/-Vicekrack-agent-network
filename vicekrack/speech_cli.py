"""Step 46 commands: Grok-generated narration (xAI text to speech). Only speech-submit spends money."""

import argparse
import json
from pathlib import Path

from . import speech_jobs as speech
from .errors import NetworkError

COMMANDS = {
    "speech-prepare": "Prepare a narration request from a ShortScript or a completed production (offline, no cost)",
    "speech-inspect": "Show a speech job: exact text, voice, settings, consent phrase and state (read-only)",
    "speech-list": "List speech jobs (read-only)",
    "speech-submit": "PAID: send one prepared speech request to xAI (needs --consent paid-speech:JOB_ID)",
    "speech-recover": "Offline recovery: mark an interrupted submission uncertain, or convert saved audio locally",
}


def parser():
    root = argparse.ArgumentParser(description="Grok-generated narration; no automatic paid requests")
    commands = root.add_subparsers(dest="command", required=True)
    for name, text in COMMANDS.items():
        command = commands.add_parser(name, help=text, description=text)
        if name == "speech-prepare":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--script", type=Path, help="a validated ShortScript JSON file")
            source.add_argument("--production", help="a completed production ID (uses its saved script)")
            command.add_argument("--voice", help="stock voice (default eve; see config/speech.json)")
            command.add_argument("--language", help="language code (default en)")
            command.add_argument("--codec", choices=["wav", "mp3"])
            command.add_argument("--sample-rate", type=int)
            command.add_argument("--bit-rate", type=int, help="mp3 only")
        elif name != "speech-list":
            command.add_argument("job_id")
        if name == "speech-submit":
            command.add_argument("--consent", required=True, help="exactly paid-speech:JOB_ID")
            command.add_argument("--allow-network", action="store_true")
            command.add_argument("--retry-uncertain", action="store_true")
            command.add_argument("--acknowledge-duplicate-billing", action="store_true",
                                 help="required with --retry-uncertain: the earlier request may already have been billed")
    return root


def run(args):
    if args.command == "speech-prepare":
        if args.production:
            script = speech.production_script(args.production)
            source, production_id = "production", args.production
        else:
            script, source, production_id = speech.read_script_file(args.script), "script_file", None
        return speech.view(speech.prepare(script, source=source, production_id=production_id, voice=args.voice,
                                          language=args.language, codec=args.codec, sample_rate=args.sample_rate,
                                          bit_rate=args.bit_rate))
    if args.command == "speech-list":
        return {"speech_jobs": speech.list_jobs()}
    if args.command == "speech-inspect":
        return speech.view(speech.inspect(args.job_id))
    if args.command == "speech-recover":
        return speech.view(speech.recover(args.job_id))
    return speech.view(speech.submit(args.job_id, consent=args.consent, allow_network=args.allow_network,
                                     retry_uncertain=args.retry_uncertain,
                                     acknowledge_duplicate_billing=args.acknowledge_duplicate_billing))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        code = 0
        if args.command in ("speech-submit", "speech-recover") and result["status"] != "completed":
            code = 1                                   # too long, invalid, rejected: a clear non-success result
    except NetworkError as error:
        result, code = {"error": {"code": error.code, "message": error.message}}, 1
    except (OSError, ValueError, UnicodeError):
        result, code = {"error": {"code": "speech_storage_error", "message": "Cannot read input or local speech storage."}}, 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code
