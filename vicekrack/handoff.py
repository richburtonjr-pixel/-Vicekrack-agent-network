"""Validate the explicit, versioned information exchanged between workflow stages."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import NetworkError


def validate_handoff(task, recipient):
    handoff = task.get("context", {}).get("handoff")
    schema = json.loads((Path(__file__).resolve().parent.parent / "schemas/handoff.schema.json").read_text())
    if not Draft202012Validator(schema).is_valid(handoff):
        raise NetworkError("invalid_handoff", "The stage requires a valid structured handoff.")
    expected = {"analyst": ["researcher"], "reviewer": ["researcher", "analyst"]}[recipient]
    if (handoff["stage_task_id"] != task["task_id"]
            or handoff["task_id"] != task.get("parent_task_id")
            or handoff["original_request"] != {
                "instructions": task.get("instructions"),
                "design_notes": task.get("context", {}).get("design_notes")}
            or handoff["recipient"] != recipient
            or handoff["metadata"]["step"] != len(expected) + 1
            or [entry["agent"] for entry in handoff["history"]] != expected
            or handoff["previous_output"] != handoff["history"][-1]["result"]
            or handoff["provider_used"] != handoff["history"][-1]["provider"]):
        raise NetworkError("invalid_handoff", "Handoff IDs, order, or previous output do not match the stage.")
    return handoff
