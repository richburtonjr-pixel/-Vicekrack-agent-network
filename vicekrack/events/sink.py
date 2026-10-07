"""Optional event sinks (Step 31).

Instrumented code takes an `events` sink and calls `emit(...)`. The default `NullSink`
does nothing, so commands without `--record-events` behave and hash exactly as before:
events never enter a run's results.

`Recorder` numbers events 1..N, validates every event against the contract and the
display-state transition rules, refuses duplicate order decisions or fills, and (when
given an EventStore) persists each event before returning. On ANY failure it stops
recording, marks the timeline (`persistence_failed`, `event_limit_reached` or
`aborted`) if it still can, and raises a NetworkError with that code, so the command
fails explicitly instead of reporting a timeline with missing events. It never retries.
"""

from uuid import uuid4

from ..errors import NetworkError
from .contract import apply, event_id, utc_now, validate_event

FAILURE_OUTCOME = {"event_limit_reached": "event_limit_reached", "event_persistence_failed": "persistence_failed",
                   "event_duplicate": "persistence_failed"}


class NullSink:
    """Recording disabled: every call is a no-op."""
    enabled = False
    timeline_id = None
    run_id = None

    def emit(self, *args, **kwargs):
        return None

    def close(self, *args, **kwargs):
        return None

    def summary(self):
        return {"enabled": False}


class Recorder:
    enabled = True

    def __init__(self, *, department, kind, components, run_id=None, correlation_id=None, store=None,
                 max_events=5000, clock=utc_now):
        self.department, self.kind, self.components = department, kind, tuple(components)
        self.timeline_id = "tl-" + uuid4().hex[:24]
        self.correlation_id = correlation_id or "cor-" + uuid4().hex[:24]
        self.run_id, self.clock, self.max_events = run_id, clock, max_events
        if store is not None:
            max_events = min(max_events, store.config["limits"]["max_events_per_timeline"])
            self.max_events = max_events
        self.events, self.states = [], {name: "idle" for name in self.components}
        self.seen, self.failure, self.outcome = set(), None, None
        self.started_at = clock()
        self.writer = None
        if store is not None:
            self.writer = store.open({"contract": "execution_timeline_manifest", "version": "1.0",
                                      "timeline_id": self.timeline_id, "correlation_id": self.correlation_id,
                                      "department": department, "kind": kind, "components": list(self.components),
                                      "run_id": run_id, "started_at": self.started_at})

    def emit(self, component, stage, event_type, status, *, sim_time=None, reason_codes=(), refs=(), details=None):
        if self.failure is not None:
            raise NetworkError(self.failure, "Event recording already failed; the timeline is incomplete.")
        if self.outcome is not None:
            self._fail("invalid_event", "aborted")
        if len(self.events) >= self.max_events:
            self._fail("event_limit_reached")
        event = {"contract": "execution_event", "version": "1.0", "event_id": None, "origin": "recorded",
                 "timeline_id": self.timeline_id, "correlation_id": self.correlation_id, "run_id": self.run_id,
                 "department": self.department, "component": component, "stage": stage,
                 "sequence": len(self.events) + 1, "event_type": event_type, "status": status, "sim_time_utc": sim_time,
                 "recorded_at": self.clock(), "reason_codes": list(reason_codes), "refs": [dict(r) for r in refs],
                 "details": dict(details or {})}
        event["event_id"] = event_id(self.timeline_id, event["sequence"])
        try:
            validate_event(event)
        except NetworkError:
            self._fail("invalid_event", "aborted")
        key = (event_type, status, tuple((r["kind"], r["id"]) for r in event["refs"]))
        if event_type in ("order_decision", "simulated_fill") and key in self.seen:
            self._fail("event_duplicate")
        states = dict(self.states)
        try:
            apply(states, event)
        except NetworkError:
            self._fail("invalid_event_transition", "aborted")
        if self.writer is not None:
            try:
                self.writer.append(event)
            except NetworkError as error:
                self._fail("event_duplicate" if error.code == "event_duplicate" else "event_persistence_failed")
        self.events.append(event)
        self.states = states
        self.seen.add(key)
        return event

    def _fail(self, code, outcome=None):
        self.failure = code
        if self.writer is not None:
            try:
                self.writer.close(outcome or FAILURE_OUTCOME.get(code, "persistence_failed"), len(self.events), [code],
                                  self.clock())
            except NetworkError:
                pass                                   # the missing close marker already reads as not complete
        raise NetworkError(code, "Event recording failed; the timeline is incomplete and the command stopped.")

    def close(self, outcome, reason_codes=()):
        """Finish the timeline. Raises if persistence fails (or already failed)."""
        if self.failure is not None:
            raise NetworkError(self.failure, "Event recording failed; the timeline is incomplete.")
        if self.outcome is not None:
            return
        self.outcome = outcome
        if self.writer is not None:
            try:
                self.writer.close(outcome, len(self.events), reason_codes, self.clock())
            except NetworkError:
                self.failure = "event_persistence_failed"
                raise NetworkError("event_persistence_failed", "Could not close the event timeline.") from None

    def abort(self, code):
        """Best-effort close after the instrumented command itself failed: `failed` if a component
        recorded a failure, otherwise `aborted`. A failed close leaves no marker (never `complete`)."""
        if self.failure is None and self.outcome is None:
            try:
                self.close("failed" if "failed" in self.states.values() else "aborted", [code])
            except NetworkError:
                pass

    def summary(self):
        """What the command reports: never `complete` once recording failed."""
        if self.failure is not None:
            completeness = "partial"
        else:
            completeness = "complete" if self.outcome is not None else "open"
        return {"enabled": True, "timeline_id": self.timeline_id, "department": self.department,
                "outcome": FAILURE_OUTCOME.get(self.failure, "aborted") if self.failure else self.outcome,
                "completeness": completeness, "event_count": len(self.events), "failure": self.failure}
