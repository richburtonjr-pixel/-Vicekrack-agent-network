"""Explicit offline validation and scene-plan publication."""
import argparse
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4
from .errors import NetworkError
from .orchestrator import ROOT, read_json
from .scene_plan import build_scene_plan, validate_scene_plan
from .short_script import validate_short_script


def save_plan(plan, directory=None):
    validate_scene_plan(plan)
    folder = Path(directory) if directory is not None else ROOT / "runtime/plans"
    temporary = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (plan["plan_id"] + "-" + uuid4().hex + ".json")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(plan, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except OSError:
        raise NetworkError("plan_write_failed", "Could not publish a complete local plan without overwriting.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Never promote an orphan temporary file.
    return target.resolve()


def main():
    parser = argparse.ArgumentParser(description="Validate scripts or plan scenes offline; never creates media")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate-short-script")
    check.add_argument("file", type=Path)
    check.add_argument("--require-verified", action="store_true", help="Require declared verified claims and source references")
    plan = commands.add_parser("plan-short")
    plan.add_argument("file", type=Path)
    plan.add_argument("--draft", action="store_true", help="Allow an explicitly production-blocked preview")
    plan.add_argument("--capabilities", type=Path, default=ROOT / "config/visual-capabilities.json")
    args = parser.parse_args()
    try:
        script = read_json(args.file)
        if args.command == "validate-short-script":
            validate_short_script(script, require_verified_claims=args.require_verified)
            result = {"valid": True, "declared_verification_required": args.require_verified,
                      "externally_fact_checked": False}
        else:
            prepared = build_scene_plan(script, read_json(args.capabilities), draft=args.draft)
            target = save_plan(prepared)
            result = {"plan_file": str(target), "plan_id": prepared["plan_id"], "mode": prepared["mode"],
                      "blocked_for_production": prepared["blocked_for_production"],
                      "scene_count": len(prepared["scenes"]), "assets_produced": False}
        print(json.dumps(result, indent=2))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": {"code": error.code}}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
