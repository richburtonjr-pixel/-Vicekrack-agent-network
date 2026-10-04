"""Explicit Creator commands: validate a Story Brief, or draft and save a Short Script.

Neither command plans scenes, renders media, or publishes anything. draft-short makes at
most one provider request, and only when the selected configuration names OpenAI or
Anthropic. Output reports locations and counts, never brief or script text.
"""
import argparse
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from .creator import draft_short_script, drafter_for, load_creator_config
from .errors import NetworkError
from .orchestrator import ROOT, read_json
from .short_script import validate_short_script
from .story_brief import count_unverified, validate_story_brief


def save_script(script, directory=None):
    """Atomically publish a validated script under runtime/scripts without overwriting."""
    validate_short_script(script)
    folder = Path(directory) if directory is not None else ROOT / "runtime/scripts"
    temporary = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (script["script_id"] + "-" + uuid4().hex + ".json")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(script, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except OSError:
        raise NetworkError("script_write_failed", "Could not publish a complete local script without overwriting.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Never promote an orphan temporary file.
    return target.resolve()


def main():
    parser = argparse.ArgumentParser(description="Validate Story Briefs or draft Short Scripts; never creates media or publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate-brief")
    check.add_argument("file", type=Path)
    check.add_argument("--require-verified", action="store_true", help="Require declared verified claims")
    draft = commands.add_parser("draft-short")
    draft.add_argument("file", type=Path)
    draft.add_argument("--config", default="config/creator.json",
                       help="Creator configuration relative to the project root (default: offline mock)")
    args = parser.parse_args()
    try:
        brief = read_json(args.file)
        if args.command == "validate-brief":
            validate_story_brief(brief, require_verified_claims=args.require_verified)
            result = {"valid": True, "claims_total": len(brief["claims"]),
                      "claims_unverified": count_unverified(brief),
                      "declared_verification_required": args.require_verified,
                      "externally_fact_checked": False}
        else:
            config = load_creator_config(args.config)
            adapter, model = config["execution"]["adapter"], config["execution"]["model"]
            script = draft_short_script(brief, adapter=adapter, model=model, drafter=drafter_for(adapter))
            target = save_script(script)
            unverified = count_unverified(script)
            result = {"script_file": str(target), "script_id": script["script_id"],
                      "provider": adapter, "model": model,
                      "claims_total": len(script["claims"]), "claims_unverified": unverified,
                      "production_gate": "blocked_unverified_claims" if unverified else "declared_verified",
                      "next_command": "plan-short SCRIPT_FILE" + (" --draft" if unverified else ""),
                      "assets_produced": False, "published": False}
        print(json.dumps(result, indent=2))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
