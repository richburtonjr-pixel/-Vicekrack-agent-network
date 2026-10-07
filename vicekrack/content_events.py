"""Content execution events (Step 33): optional recording for the Step 5 Researcher -> Analyst ->
Reviewer workflow, the Step 21 production pipeline and the Step 22 quality report.

Components (department "content"):
  content.workflow.orchestrator   the Step 5 workflow controller
  content.workflow.researcher / .analyst / .reviewer   the Step 5 roles
  content.production.pipeline     the Step 21 pipeline controller
  content.production.brief / .creator / .validate / .plan / .preview   pipeline stages
  content.production.quality      the Step 22 quality report

Failure handling ("fail closed for new work, keep committed results"):
- An event failure is captured, never raised in the middle of a stage, so a result that was
  already produced (possibly by a paid request) is always saved first.
- Instrumented code calls `check()` before it starts any NEW stage. If recording has failed,
  `check()` raises EventFailure there: no new provider or paid call is made, the saved run
  or production stays consistent and resumable, and the command reports
  `event_persistence_failed` (or the recorder's own code). Nothing is retried.
- "started" is emitted before a stage's intent checkpoint (a failure there costs nothing);
  "completed"/"failed" are emitted after the result is saved.

Attempts: every invocation (start or resume) is its own recorded timeline. All attempts of
one run share `correlation_id = "cor-" + sha256({"run": <run or production ID>})[:24]`, the
same correlation the read-only reconstructions use. Stages finished in an earlier attempt are
reported once as `stage_reused` (reason `completed_in_earlier_attempt`); they never appear to
run again. Restarted stages carry `details.attempt` and a reason such as
`retry_after_failure`, `retry_after_uncertain` or `resumed_after_interruption`.
"""

from .errors import NetworkError

ORCHESTRATOR = "content.workflow.orchestrator"
ROLES = {"researcher": "content.workflow.researcher", "analyst": "content.workflow.analyst",
         "reviewer": "content.workflow.reviewer"}
WORKFLOW_COMPONENTS = (ORCHESTRATOR,) + tuple(ROLES.values())
PIPELINE = "content.production.pipeline"
STAGE_COMPONENTS = {name: "content.production." + name for name in ("brief", "creator", "validate", "plan", "preview")}
PRODUCTION_COMPONENTS = (PIPELINE,) + tuple(STAGE_COMPONENTS.values())
QUALITY = "content.production.quality"
QUALITY_COMPONENTS = (QUALITY,)
KINDS = {"workflow": ("research_review_workflow", WORKFLOW_COMPONENTS),
         "production": ("content_production", PRODUCTION_COMPONENTS),
         "quality": ("content_quality", QUALITY_COMPONENTS)}
REUSED = ["completed_in_earlier_attempt"]


def correlation_for(source_id):
    from .events.contract import digest        # lazy: the event core imports persistence, which imports us
    return "cor-" + digest({"run": source_id})[:24]


def workflow_run_ref(run_id):
    return "wfr-" + run_id


class EventFailure(Exception):
    """Recording failed; raised only before new work starts (deliberately not a NetworkError)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ContentEvents:
    """Optional, deferred-failure event sink for content commands. Disabled without a factory."""

    def __init__(self, kind=None, factory=None):
        self.enabled = factory is not None
        self.kind = kind
        self.factory = factory
        self.recorder = None
        self.failure = None
        self.binding = None

    @classmethod
    def persistent(cls, kind, root=None):
        """Events saved under runtime/events/content/ (Step 31 storage rules)."""
        from .events.sink import Recorder
        from .events.store import EventStore
        timeline_kind, components = KINDS[kind]

        def factory(run_id, correlation_id):
            return Recorder(department="content", kind=timeline_kind, components=components, run_id=run_id,
                            correlation_id=correlation_id, store=EventStore(root))
        return cls(kind, factory)

    def bind(self, run_id, source_id):
        """Remember which run these events belong to; the timeline opens on the first event."""
        if self.enabled and self.binding is None:
            self.binding = (run_id, correlation_for(source_id))

    def _open(self):
        if self.recorder is None and self.failure is None:
            if self.binding is None:
                raise RuntimeError("ContentEvents.bind() must be called before emitting")
            try:
                self.recorder = self.factory(*self.binding)
            except NetworkError as error:
                self.failure = error.code
        return self.recorder

    def emit(self, component, stage, event_type, status, **fields):
        if not self.enabled or self.failure is not None:
            return
        recorder = self._open()
        if recorder is None:
            return
        try:
            recorder.emit(component, stage, event_type, status, **fields)
        except NetworkError as error:
            self.failure = error.code

    def check(self):
        """Raise EventFailure if recording failed: call before starting any new stage."""
        if self.enabled and (self.failure is not None or (self.binding is not None and self._open() is None)):
            raise EventFailure(self.failure or "event_persistence_failed")

    def close(self, outcome):
        if self.recorder is not None and self.failure is None:
            try:
                self.recorder.close(outcome)
            except NetworkError as error:
                self.failure = error.code

    def abort(self, code):
        if self.recorder is not None:
            self.recorder.abort(code)

    def summary(self):
        if not self.enabled:
            return {"enabled": False}
        if self.recorder is None:
            return {"enabled": True, "timeline_id": None, "event_count": 0, "failure": self.failure,
                    "completeness": "partial" if self.failure else "none_recorded"}
        summary = self.recorder.summary()
        if self.failure is not None:
            summary.update(failure=self.failure, completeness="partial")
        return summary


DISABLED = ContentEvents()


def retry_reason(previous_status, previous_error):
    """Why a stage runs again in this attempt (None for a first attempt)."""
    if previous_error == "interrupted":
        return "resumed_after_interruption"
    if previous_status == "uncertain":
        return "retry_after_uncertain"
    if previous_status == "failed":
        return "retry_after_failure"
    return None
