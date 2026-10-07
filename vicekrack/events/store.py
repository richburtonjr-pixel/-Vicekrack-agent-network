"""Recorded-timeline storage (Step 31): runtime/events/<department>/timelines/<tl-id>/ (ignored by Git).

Layout per timeline
  manifest.json        written exclusively when the timeline opens
  writer.lock          held (OS lock) by the one writing process for the timeline's lifetime
  events/000001.json   one file per event, temp file + fsync + exclusive link: never partial,
                       never overwritten; a second file for a sequence is `event_duplicate`
  closed.json          written exclusively when the writer finishes (outcome + event count)

Ordering is the sequence number (emission order inside one process); events are numbered
1..N with no gaps. Concurrency: one writer per timeline (random IDs plus the lock; a second
writer gets `event_timeline_exists`); different timelines never share files. An
interrupted write leaves at most a `*.tmp` file, which is ignored and reported. Loading
re-validates every file and reports problems (`missing_events`, `corrupt_event`,
`event_count_mismatch`, `interrupted_write_debris`); a timeline with any problem is never
`complete`.

Retention: at most `max_timelines_per_department` timelines per department. Opening a new
one deletes the oldest *closed* timelines first; open or unreadable ones are never deleted,
and if nothing can be deleted the open fails with `event_storage_full`.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

from ..errors import NetworkError
from .contract import ROOT, build_view, utc_now, validate_event

TIMELINE_ID = re.compile(r"^tl-[0-9a-f]{24}$")
EVENT_FILE = re.compile(r"^([0-9]{6})\.json$")
DEPARTMENTS = ("trading", "content")
CODE = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"}
STAMP = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"}
MANIFEST = Draft202012Validator({
    "type": "object", "additionalProperties": False,
    "required": ["contract", "version", "timeline_id", "correlation_id", "department", "kind", "components", "run_id", "started_at"],
    "properties": {"contract": {"const": "execution_timeline_manifest"}, "version": {"const": "1.0"},
                   "timeline_id": {"type": "string", "pattern": TIMELINE_ID.pattern},
                   "correlation_id": {"type": "string", "pattern": "^cor-[0-9a-f]{24}$"},
                   "department": {"enum": list(DEPARTMENTS)},
                   "kind": {"enum": ["research_agent_workflow", "simulation", "content_production",
                                     "research_review_workflow", "content_quality"]},
                   "components": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True,
                                  "items": {"type": "string", "pattern": "^(trading|content)\\.[a-z0-9_.]{1,90}$"}},
                   "run_id": {"anyOf": [{"type": "string", "pattern": "^((rar|srun|prod)-[0-9a-f]{24}|wfr-[0-9a-f]{32})$"}, {"type": "null"}]},
                   "started_at": STAMP}})
CLOSE = Draft202012Validator({
    "type": "object", "additionalProperties": False,
    "required": ["contract", "version", "timeline_id", "outcome", "event_count", "reason_codes", "closed_at"],
    "properties": {"contract": {"const": "execution_timeline_close"}, "version": {"const": "1.0"},
                   "timeline_id": {"type": "string", "pattern": TIMELINE_ID.pattern},
                   "outcome": {"enum": ["completed", "failed", "aborted", "persistence_failed", "event_limit_reached"]},
                   "event_count": {"type": "integer", "minimum": 0}, "reason_codes": {"type": "array", "maxItems": 10, "items": CODE},
                   "closed_at": STAMP}})


def load_events_config(path="config/events.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    try:
        if not target.is_relative_to(root):
            raise ValueError
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        schema = json.loads((ROOT / "schemas/events-config.schema.json").read_text(encoding="utf-8"))
        if next(Draft202012Validator(schema).iter_errors(config), None) is not None:
            raise ValueError
        if config["replay"]["default_delay_ms"] > config["replay"]["max_delay_ms"]:
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        raise NetworkError("invalid_events_config", "Cannot read a valid events config.") from None
    return config


def _write_exclusive(target, document, duplicate_code):
    """Temp file + fsync + exclusive link. Never partial; never overwrites."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=1, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except FileExistsError:
        raise NetworkError(duplicate_code, "That record already exists; nothing was overwritten.") from None
    except (OSError, ValueError, TypeError):
        raise NetworkError("event_persistence_failed", "Could not save an execution event.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class _Lock:
    """Nonblocking OS lock on one byte of a file; released when the process exits."""

    def __init__(self, path):
        self.path, self.stream = path, None

    def acquire(self, create):
        if self.path.is_symlink():
            return False
        try:
            if create:
                stream = self.path.open("a+b")
                if stream.seek(0, 2) == 0:
                    stream.write(b"0")
                    stream.flush()
            else:
                stream = self.path.open("r+b")
        except OSError:
            return False
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def release(self):
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.stream.close()
            self.stream = None


class TimelineWriter:
    def __init__(self, folder, manifest, lock):
        self.folder, self.manifest, self.lock = folder, manifest, lock
        self.closed = False

    def append(self, event):
        if self.closed:
            raise NetworkError("event_persistence_failed", "The timeline is already closed.")
        _write_exclusive(self.folder / "events" / f"{event['sequence']:06d}.json", event, "event_duplicate")

    def close(self, outcome, event_count, reason_codes=(), closed_at=None):
        try:
            if not self.closed:
                self.closed = True
                document = {"contract": "execution_timeline_close", "version": "1.0",
                            "timeline_id": self.manifest["timeline_id"], "outcome": outcome, "event_count": event_count,
                            "reason_codes": list(reason_codes)[:10], "closed_at": closed_at or utc_now()}
                _write_exclusive(self.folder / "closed.json", document, "event_timeline_closed")
        finally:
            self.lock.release()

    def abandon(self):
        """Release the lock without closing (what a crashed process leaves behind)."""
        self.closed = True
        self.lock.release()


class EventStore:
    def __init__(self, root=None, config=None):
        self.config = config or load_events_config()
        self.base = Path(root if root is not None else ROOT) / "runtime/events"

    def timelines(self, department):
        if department not in DEPARTMENTS:
            raise NetworkError("invalid_department", "Departments are trading and content.")
        return self.base / department / "timelines"

    def folder(self, department, timeline_id):
        if not TIMELINE_ID.match(str(timeline_id)):
            raise NetworkError("invalid_timeline_id", "A recorded timeline ID looks like tl- followed by 24 hex characters.")
        folder = self.timelines(department) / timeline_id
        if folder.is_symlink():
            raise NetworkError("event_timeline_corrupt", "Timeline folders cannot be symbolic links.")
        return folder

    # ------------------------------------------------------------------ writing
    def open(self, manifest):
        if next(MANIFEST.iter_errors(manifest), None) is not None:
            raise NetworkError("invalid_event", "The timeline manifest is invalid.")
        parent = self.timelines(manifest["department"])
        try:
            parent.mkdir(parents=True, exist_ok=True)
            self._retain(manifest["department"])
            folder = self.folder(manifest["department"], manifest["timeline_id"])
            folder.mkdir()
            (folder / "events").mkdir()
        except FileExistsError:
            raise NetworkError("event_timeline_exists", "A timeline with this ID already exists.") from None
        except OSError:
            raise NetworkError("event_persistence_failed", "Could not create local event storage.") from None
        lock = _Lock(folder / "writer.lock")
        if not lock.acquire(create=True):
            raise NetworkError("event_timeline_exists", "Another process is writing this timeline.")
        try:
            _write_exclusive(folder / "manifest.json", manifest, "event_timeline_exists")
        except NetworkError:
            lock.release()
            raise
        return TimelineWriter(folder, manifest, lock)

    def _retain(self, department):
        limit = self.config["limits"]["max_timelines_per_department"]
        rows = []
        for folder in self._folders(department):
            manifest, closed = self._read_json(folder / "manifest.json", MANIFEST), self._read_json(folder / "closed.json", CLOSE)
            rows.append((folder, manifest, closed))
        if len(rows) < limit:
            return
        deletable = sorted((m["started_at"], f.name, f) for f, m, c in rows if m is not None and c is not None)
        remaining = len(rows)
        for _, _, folder in deletable:
            if remaining < limit:
                break
            try:
                shutil.rmtree(folder)
                remaining -= 1
            except OSError:
                continue
        if remaining >= limit:
            raise NetworkError("event_storage_full", "The timeline limit is reached and no closed timeline can be pruned.")

    # ------------------------------------------------------------------ reading
    def _folders(self, department):
        parent = self.timelines(department)
        if not parent.is_dir():
            return []
        return sorted(p for p in parent.iterdir() if TIMELINE_ID.match(p.name) and p.is_dir() and not p.is_symlink())

    @staticmethod
    def _read_json(path, validator):
        try:
            if path.is_symlink():
                return None
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return None
        return document if next(validator.iter_errors(document), None) is None else None

    def live(self, folder):
        """True only while a writer process holds the timeline's lock."""
        if (folder / "closed.json").exists() or not (folder / "writer.lock").is_file():
            return False
        probe = _Lock(folder / "writer.lock")
        if probe.acquire(create=False):
            probe.release()
            return False
        return True

    def find(self, timeline_id):
        for department in DEPARTMENTS:
            folder = self.folder(department, timeline_id)
            if folder.is_dir():
                return department, folder
        raise NetworkError("event_timeline_not_found", "No recorded timeline with this ID.")

    def load(self, timeline_id):
        department, folder = self.find(timeline_id)
        manifest = self._read_json(folder / "manifest.json", MANIFEST)
        if manifest is None or manifest["timeline_id"] != timeline_id or manifest["department"] != department:
            raise NetworkError("event_timeline_corrupt", "The timeline manifest is missing or invalid.")
        issues, events = [], []
        names = sorted(os.listdir(folder / "events")) if (folder / "events").is_dir() else []
        if any(name.endswith(".tmp") for name in names):
            issues.append("interrupted_write_debris")
        for name in names:
            match = EVENT_FILE.match(name)
            if not match:
                continue
            try:
                event = validate_event(json.loads((folder / "events" / name).read_text(encoding="utf-8")))
                if (event["origin"], event["timeline_id"], event["sequence"], event["department"], event["correlation_id"]) != (
                        "recorded", timeline_id, int(match.group(1)), department, manifest["correlation_id"]):
                    raise ValueError
                events.append(event)
            except (OSError, ValueError, UnicodeError, NetworkError):
                issues.append("corrupt_event")
        events.sort(key=lambda e: e["sequence"])
        contiguous = []
        for expected, event in enumerate(events, start=1):
            if event["sequence"] != expected:
                issues.append("missing_events")
                break
            contiguous.append(event)
        closed = self._read_json(folder / "closed.json", CLOSE)
        if (folder / "closed.json").exists() and closed is None:
            issues.append("corrupt_close_marker")
        live = closed is None and self.live(folder)
        if closed is not None:
            if closed["event_count"] != len(events) or len(contiguous) != len(events):
                issues.append("event_count_mismatch")
            completeness = "partial" if closed["outcome"] in ("persistence_failed", "event_limit_reached") else "complete"
        else:
            completeness = "open" if live else "interrupted"
        return build_view(timeline_id=timeline_id, origin="recorded", department=department, kind=manifest["kind"],
                          correlation_id=manifest["correlation_id"],
                          run_id=manifest["run_id"] or next((e["run_id"] for e in contiguous if e["run_id"]), None), source=None,
                          time_basis="recorded_at_emission", started_at=manifest["started_at"],
                          components=manifest["components"], events=contiguous, completeness=completeness,
                          outcome=closed["outcome"] if closed else None, live=live,
                          issues=list(dict.fromkeys(issues)))

    @staticmethod
    def _created_ns(folder):
        """When the manifest was written (ns): orders attempts that started within the same second."""
        try:
            return (folder / "manifest.json").stat().st_mtime_ns
        except OSError:
            return 0

    def list(self, department=None):
        rows = []
        for name in ([department] if department else DEPARTMENTS):
            for folder in self._folders(name):
                try:
                    view = self.load(folder.name)
                    rows.append({"timeline_id": view["timeline_id"], "origin": "recorded", "department": name,
                                 "kind": view["kind"], "run_id": view["run_id"], "started_at": view["started_at"],
                                 "correlation_id": view["correlation_id"], "created_ns": self._created_ns(folder),
                                 "completeness": view["completeness"], "outcome": view["outcome"], "live": view["live"],
                                 "event_count": view["event_count"], "readable": True})
                except NetworkError:
                    rows.append({"timeline_id": folder.name, "origin": "recorded", "department": name, "readable": False})
        rows.sort(key=lambda r: (r.get("started_at") or "", r["timeline_id"]), reverse=True)
        return rows
