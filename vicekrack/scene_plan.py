"""Deterministic offline scene decisions; no asset production or provider calls."""
import hashlib
import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from jsonschema import Draft202012Validator
from .errors import NetworkError
from .persistence import reject_secrets
from .short_script import VISUAL_METHODS, validate_short_script

DEFAULT_CAPABILITIES = {"available_methods": ["motion_graphics", "text_card"]}
ROOT = Path(__file__).resolve().parent.parent


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def capabilities(value):
    if not isinstance(value, dict) or set(value) != {"available_methods"}:
        raise NetworkError("invalid_capabilities", "Supply only available_methods.")
    methods = value["available_methods"]
    if (not isinstance(methods, list) or not all(isinstance(m, str) and m in VISUAL_METHODS for m in methods)
            or len(set(methods)) != len(methods)):
        raise NetworkError("invalid_capabilities", "Methods must be unique supported names.")
    return {"available_methods": sorted(methods)}


def build_scene_plan(script, capability_config=None, *, draft=False):
    if type(draft) is not bool:
        raise NetworkError("invalid_plan_mode", "draft must be a boolean.")
    validate_short_script(script, require_verified_claims=not draft)
    config = capabilities(DEFAULT_CAPABILITIES if capability_config is None else capability_config)
    unverified = [claim["claim_id"] for claim in script["claims"] if claim["status"] != "verified"]
    scenes = []
    for index, beat in enumerate(script["beats"], 1):
        considered = []
        for method in [beat["visual"]["preferred_method"], *beat["visual"]["fallback_methods"]]:
            supported = method in config["available_methods"]
            considered.append({"method": method, "status": "selected" if supported else "skipped",
                               "reason": "configured_available" if supported else "not_configured_available"})
            if supported:
                scenes.append({"index": index, "beat": deepcopy(beat), "selected_method": method,
                               "considered_methods": considered, "assets_produced": False})
                break
        else:
            raise NetworkError("no_visual_method", f"No configured method can plan scene {index}.")
    plan = {"contract": "scene_plan", "version": "1.0",
            "script_id": script["script_id"], "input_sha256": hashlib.sha256(canonical(script)).hexdigest(),
            "script": deepcopy(script), "capabilities": config,
            "frame": {"width": 1080, "height": 1920, "aspect_ratio": "9:16"},
            "duration_seconds": script["duration_seconds"], "mode": "draft" if draft else "production",
            "blocked_for_production": draft, "unverified_claim_ids": unverified,
            "assets_produced": False, "scenes": scenes}
    if "parent_task_id" in script:
        plan["parent_task_id"] = script["parent_task_id"]
    plan["plan_id"] = hashlib.sha256(canonical(plan)).hexdigest()
    reject_secrets(plan)
    return plan


@lru_cache(maxsize=1)
def validator():
    schema = json.loads((ROOT / "schemas/scene-plan.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_scene_plan(plan):
    try:
        reject_secrets(plan)
        if not validator().is_valid(plan):
            raise ValueError
        expected = build_scene_plan(plan["script"], plan["capabilities"], draft=plan["mode"] == "draft")
        if canonical(expected) != canonical(plan):
            raise ValueError
    except NetworkError as error:
        if error.code == "sensitive_state":
            raise
        raise NetworkError("invalid_scene_plan", "Scene plan is inconsistent or invalid.") from None
    except (ValueError, TypeError, KeyError):
        raise NetworkError("invalid_scene_plan", "Scene plan is inconsistent or invalid.") from None
