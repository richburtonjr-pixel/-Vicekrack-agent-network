"""ViceKrack department registry: the one list of departments, agents and HQ rooms.

Every screen (the 3D headquarters, the classic house, the Trading terminal and, later, the phone)
reads its departments and agents from `config/departments.json` instead of keeping its own copy.
Adding a department or an agent means editing that one file.

The registry is display data only. It grants no permission, changes no limit, starts nothing and
says nothing about what an agent is doing right now: live status always comes from saved records.
Trading limits, the kill switch and every money calculation stay in `vicekrack/trading/`.
"""

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import NetworkError

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "config" / "departments.json"
SCHEMA = ROOT / "schemas" / "department_registry.schema.json"
MAX_BYTES = 64 * 1024


@lru_cache(maxsize=None)
def _validator():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _fail(message):
    raise NetworkError("invalid_department_registry", message)


def _unique(items, what):
    seen = set()
    for item in items:
        if item["id"] in seen:
            _fail(f"Duplicate {what} id: {item['id']}.")
        seen.add(item["id"])
    return seen


def validate(document):
    """Return the document if it is a valid registry, else raise NetworkError(invalid_department_registry)."""
    if not _validator().is_valid(document):
        _fail("The department registry does not match its schema.")
    departments = _unique(document["departments"], "department")
    agents = {agent["id"]: agent for agent in document["agents"]}
    _unique(document["agents"], "agent")
    _unique(document["rooms"], "room")
    for agent in document["agents"]:
        if agent["department"] not in departments:
            _fail(f"Agent {agent['id']} belongs to an unknown department.")
    for room in document["rooms"]:
        if room["department"] not in departments:
            _fail(f"Room {room['id']} belongs to an unknown department.")
        if room["agent"] is not None:
            agent = agents.get(room["agent"])
            if agent is None:
                _fail(f"Room {room['id']} names an unknown agent.")
            if agent["department"] != room["department"]:
                _fail(f"Room {room['id']} and its agent are in different departments.")
    return document


def load(path=None):
    """Read and validate the registry file. Fails closed with a fixed error code."""
    try:
        raw = Path(REGISTRY if path is None else path).read_bytes()
    except OSError:
        _fail("The department registry could not be read.")
    if len(raw) > MAX_BYTES:
        _fail("The department registry is too large.")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("The department registry is not valid JSON.")
    return validate(document)


def agents_in(document, department):
    """The agents of one department, in registry order."""
    return [agent for agent in document["agents"] if agent["department"] == department]
