"""HQ scenes (Step 32): Step 31 timelines turned into per-event frames for the Living HQ.

A scene is display data. For every event, in recorded order, it holds:
- the component and display states after that event, using Step 31's own transition rules
  (an invalid transition ends the frames there);
- "waiting": a member of a fixed workflow (research stages; content-production stages)
  that is still idle while that workflow is actively working: its controller is working,
  or a member is working and an earlier member has already started. It is derived only
  from recorded states;
- a handoff (from room -> to room) ONLY when the recorded order supports it:
    stage B started right after the previous member A was recorded `completed`;
    the first research stage started while the research controller was working;
    the research controller completed right after the last stage completed.

`current` holds Step 31's honest display states for the timeline as loaded (liveness and
partial rules applied). Historical frame states are "as recorded at that event", never a
claim about now. Modes: demo, recorded_replay, reconstructed, observed (recorded and its
writer is alive now).
"""

import json
from functools import lru_cache

from jsonschema import Draft202012Validator

from ..errors import NetworkError
from ..events.contract import ROOT, apply
from .layout import COMPONENT_ROOM, GROUPS, ROOMS

PRIORITY = ("failed", "blocked", "working", "unknown", "waiting", "completed", "idle")
EVENT_FIELDS = ("sequence", "event_type", "status", "component", "stage", "department", "sim_time_utc", "recorded_at",
                "reason_codes", "refs", "details", "run_id")
MODES = {
    "demo": "Demo data: synthetic and deterministic, not a real run",
    "recorded_replay": "Recorded replay: captured while a command ran; not running now",
    "reconstructed": "Reconstructed history: rebuilt from a saved record; no times invented",
    "observed": "Live observed: the recording process is running now",
}
NOTICE = ("Read-only display. Bots and rooms show only recorded or reconstructed events (or clearly labelled demo "
          "data). Idle wandering is decoration and triggers nothing.")
BOT_ROOMS = {room["room"]: room["bot"] for room in ROOMS}


@lru_cache(maxsize=None)
def _validator():
    return Draft202012Validator(json.loads((ROOT / "schemas/hq-scene.schema.json").read_text(encoding="utf-8")))


def with_waiting(states):
    """Display states with `waiting` derived inside the fixed workflow orders."""
    shown = dict(states)
    for group in GROUPS:
        members, controller = group["members"], group["controller"]
        controller_working = controller is not None and states.get(controller) == "working"
        if not (controller_working or any(states.get(m) == "working" for m in members)):
            continue
        for index, member in enumerate(members):
            if states.get(member) != "idle":
                continue
            earlier = any(states.get(members[j], "idle") != "idle" for j in range(index))
            if earlier or controller_working:
                shown[member] = "waiting"
    return shown


def room_states(component_states):
    rows = {}
    for room in ROOMS:
        values = [component_states.get(c, "idle") for c in room["components"]]
        rows[room["room"]] = min(values, key=PRIORITY.index) if values else "idle"
    return rows


def handoff(event, before):
    component, kind = event["component"], event["event_type"]
    for group in GROUPS:
        members, controller = group["members"], group["controller"]
        if kind == "stage_started" and component in members:
            index = members.index(component)
            if index > 0 and before.get(members[index - 1]) == "completed":
                source = members[index - 1]
            elif index == 0 and controller and before.get(controller) == "working":
                source = controller
            else:
                return None
            return _pair(source, component)
        if kind == "stage_completed" and component == controller and before.get(members[-1]) == "completed":
            return _pair(members[-1], component)
    return None


def _pair(source, target):
    a, b = COMPONENT_ROOM.get(source), COMPONENT_ROOM.get(target)
    return {"from": a, "to": b} if a and b and a != b else None


def build_frames(events, components):
    """Frames in recorded order; stops at the first invalid transition."""
    states = {name: "idle" for name in list(COMPONENT_ROOM) + list(components)}
    frames, stopped = [], None
    for index, event in enumerate(events):
        before = dict(states)
        try:
            apply(states, event)
        except NetworkError:
            stopped = "frames_stop_at_invalid_transition"
            break
        shown = with_waiting(states)
        room = COMPONENT_ROOM.get(event["component"])
        frames.append({"index": index, "event": {k: event.get(k) for k in EVENT_FIELDS}, "room": room,
                       "bot": BOT_ROOMS.get(room) if room else None, "component_states": shown,
                       "room_states": room_states(shown), "handoff": handoff(event, before)})
    return frames, stopped


def _rooms(events):
    active = {COMPONENT_ROOM.get(e["component"]) for e in events}
    return [{**{k: room[k] for k in ("room", "label", "floor", "department", "bot", "components", "mapping", "role",
                                      "inputs", "decisions", "outputs")},
             "has_activity": room["room"] in active,
             "event_count": sum(1 for e in events if COMPONENT_ROOM.get(e["component"]) == room["room"])}
            for room in ROOMS]


def assemble(*, mode, timeline, components, events, current_rows=None, issues=()):
    frames, stopped = build_frames(events, components)
    issues = list(dict.fromkeys(list(issues) + ([stopped] if stopped else [])))
    current = None
    if current_rows is not None:
        states = {name: "idle" for name in COMPONENT_ROOM}
        notes = {}
        for row in current_rows:
            states[row["component"]] = row["display_state"]
            notes[row["component"]] = row["note"]
        shown = with_waiting(states) if timeline["live"] else states
        current = {"component_states": shown, "room_states": room_states(shown),
                   "notes": {k: v for k, v in notes.items() if v}}
    scene = {"contract": "hq_scene", "version": "1.0", "mode": mode, "mode_label": MODES[mode], "timeline": timeline,
             "rooms": _rooms(events[:len(frames)]), "frames": frames, "current": current,
             "unmapped_components": sorted(c for c in components if c not in COMPONENT_ROOM),
             "issues": issues[:20], "notice": NOTICE}
    if next(_validator().iter_errors(scene), None) is not None:
        raise NetworkError("invalid_scene", "The HQ scene does not match its contract.")
    return scene


def scene_from_view(view):
    """A scene from a Step 31 timeline view (already validated by the Step 31 loaders)."""
    if view["origin"] == "reconstructed":
        mode = "reconstructed"
    else:
        mode = "observed" if view["live"] else "recorded_replay"
    timeline = {k: view[k] for k in ("timeline_id", "origin", "department", "kind", "completeness", "outcome", "live",
                                      "time_basis", "started_at", "event_count", "run_id", "source")}
    timeline["issues"] = view["issues"]
    components = [row["component"] for row in view["components"]]
    return assemble(mode=mode, timeline=timeline, components=components, events=view["events"],
                    current_rows=view["components"], issues=view["issues"])
