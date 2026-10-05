"""Append-only local trading journal (ignored storage under runtime/trading/journal/).

One folder per run (`correlation_id`), one JSON file per event named
`<sequence>-<event_id>.json`. Each file is written to a temporary file, fsynced and
published with an exclusive hard link, so a reader never sees a partial event and an
existing event is never overwritten. Duplicate event IDs and sequence numbers are rejected.
Every event is validated and credential-checked before it is written.

Events are designed for a future read-only dashboard: `agent` (who acted), `document`
(what it saw or produced), `observed` (key figures), `reason_codes` and, for risk events,
the full list of checks with limits and observed values.
"""

import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from .contracts import ROOT, sha256, validate_journal_event
from .errors import TradingError

RUN_ID = re.compile(r"^run-[0-9a-f]{32}$")
EVENT_FILE = re.compile(r"^(\d{4})-(evt-[0-9a-f]{32})\.json$")
MAX_EVENTS = 1000


class TradingJournal:
    def __init__(self, root=None):
        self.folder = Path(root if root is not None else ROOT) / "runtime/trading/journal"
        self._sequence = {}

    # ------------------------------------------------------------------ writing
    def start_run(self, correlation_id=None):
        correlation_id = correlation_id or "run-" + uuid.uuid4().hex
        if not RUN_ID.match(correlation_id):
            raise TradingError("invalid_run_id", "A trading run ID has an invalid format.")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / correlation_id).mkdir()
        except FileExistsError:
            raise TradingError("journal_duplicate_run", "A trading run with this ID already exists.") from None
        except OSError:
            raise TradingError("journal_write_failed", "Could not create local trading journal storage.") from None
        self._sequence[correlation_id] = 0
        return correlation_id

    def append(self, correlation_id, *, stage, status, agent, recorded_at, subject=None, observed=None,
               reason_codes=(), document=None, causation_id=None, event_id=None):
        if correlation_id not in self._sequence:
            raise TradingError("journal_unknown_run", "Events can only be appended to a run started by this journal.")
        sequence = self._sequence[correlation_id] + 1
        event = {
            "contract": "trading_journal_event", "version": "1.0",
            "event_id": event_id or "evt-" + uuid.uuid4().hex, "correlation_id": correlation_id,
            "causation_id": causation_id, "sequence": sequence, "recorded_at": recorded_at,
            "stage": stage, "status": status, "agent": agent, "subject": dict(subject or {}),
            "observed": dict(observed or {}), "reason_codes": list(reason_codes), "document": document,
            "document_sha256": sha256(document) if document is not None else None, "mode": "paper", "simulated": True,
        }
        validate_journal_event(event)
        if self._event_exists(event["event_id"]):
            raise TradingError("journal_duplicate_event", "A journal event with this ID already exists.")
        self._publish(event, self.folder / correlation_id / f"{sequence:04d}-{event['event_id']}.json")
        self._sequence[correlation_id] = sequence
        return event

    def _event_exists(self, event_id):
        if not self.folder.is_dir():
            return False
        try:
            return any(self.folder.glob(f"run-*/*-{event_id}.json"))
        except OSError:
            raise TradingError("journal_read_failed", "Could not read local trading journal storage.") from None

    @staticmethod
    def _publish(event, target):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(event, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        except FileExistsError:
            raise TradingError("journal_duplicate_event", "A journal event with this sequence already exists.") from None
        except (OSError, ValueError, TypeError):
            raise TradingError("journal_write_failed", "Could not write a complete trading journal event.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    # ------------------------------------------------------------------ reading
    def read_run(self, correlation_id):
        if not RUN_ID.match(str(correlation_id)):
            raise TradingError("invalid_run_id", "A trading run ID has an invalid format.")
        folder = self.folder / correlation_id
        if not folder.is_dir():
            raise TradingError("journal_run_not_found", "No trading run with this ID was found.")
        events = []
        try:
            names = sorted(p.name for p in folder.iterdir() if not p.name.endswith(".tmp"))
            if len(names) > MAX_EVENTS:
                raise ValueError
            for name in names:
                match = EVENT_FILE.match(name)
                if not match:
                    raise ValueError
                event = json.loads((folder / name).read_text(encoding="utf-8"))
                validate_journal_event(event)
                if (event["correlation_id"] != correlation_id or event["event_id"] != match.group(2)
                        or event["sequence"] != int(match.group(1)) or event["sequence"] != len(events) + 1):
                    raise ValueError
                events.append(event)
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("journal_corrupt", "A stored trading journal event is invalid.") from None
        except (OSError, ValueError, UnicodeError):
            raise TradingError("journal_corrupt", "A stored trading journal event is unreadable or out of order.") from None
        return events

    def list_runs(self):
        if not self.folder.is_dir():
            return []
        try:
            names = [p.name for p in self.folder.iterdir() if p.is_dir() and RUN_ID.match(p.name)]
        except OSError:
            raise TradingError("journal_read_failed", "Could not read local trading journal storage.") from None
        runs = []
        for name in names:
            try:
                events = self.read_run(name)
                first, last = events[0], events[-1]
                runs.append({"run_id": name, "started_at": first["recorded_at"],
                             "scenario": first["subject"].get("scenario"), "events": len(events),
                             "finished": last["stage"] == "run_finished", "readable": True})
            except (TradingError, IndexError):
                runs.append({"run_id": name, "started_at": None, "scenario": None, "events": None,
                             "finished": False, "readable": False})
        return sorted(runs, key=lambda r: (r["started_at"] or "", r["run_id"]), reverse=True)


def summarize(events):
    """Dashboard-ready view: who saw what, why it acted, what risk allowed or blocked."""
    timeline = []
    for event in events:
        item = {"sequence": event["sequence"], "agent": event["agent"], "stage": event["stage"],
                "status": event["status"], "subject": event["subject"], "reason_codes": event["reason_codes"],
                "observed": event["observed"]}
        if event["stage"] == "risk_check" and event["document"] is not None:
            item["checks"] = [{k: c[k] for k in ("check", "status", "limit", "observed")} for c in event["document"]["checks"]]
        timeline.append(item)
    return timeline
