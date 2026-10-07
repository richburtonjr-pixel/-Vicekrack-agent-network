"""Step 33: accurate agent activity. Recorded events for the Step 5 workflow, the production
pipeline and the quality report; role-accurate Living HQ attribution. All provider, Creator
and renderer calls are mocked; no network, no credits.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_persistence import Provider
from test_production import PAID, Base as ProductionBase, CountingDrafter, FakeRenderer
from test_quality import Base as QualityBase, ManifestRenderer
from vicekrack.content_events import (PRODUCTION_COMPONENTS, WORKFLOW_COMPONENTS, ContentEvents, EventFailure,
                                      correlation_for)
from vicekrack.errors import NetworkError
from vicekrack.events.cli import load_timeline
from vicekrack.events.sink import Recorder
from vicekrack.events.store import EventStore, TimelineWriter
from vicekrack.hq import api
from vicekrack.hq.scene import scene_from_view
from vicekrack.orchestrator import ROOT, Orchestrator, read_json
from vicekrack.persistence import RunStore, SavedRuns
from vicekrack.production import Pipeline, ProductionStore
from vicekrack.quality import QualityChecker
from vicekrack.workflow_timeline import from_saved_run

REAL_APPEND = TimelineWriter.append
REAL_PERSISTENT = ContentEvents.persistent
PORT = 8765


def memory(kind):
    """In-memory recorder factory (Step 31 Recorder without storage)."""
    components = {"workflow": WORKFLOW_COMPONENTS, "production": PRODUCTION_COMPONENTS,
                  "quality": ("content.production.quality",)}[kind]
    timeline_kind = {"workflow": "research_review_workflow", "production": "content_production", "quality": "content_quality"}[kind]
    return ContentEvents(kind, lambda run_id, correlation_id: Recorder(
        department="content", kind=timeline_kind, components=components, run_id=run_id, correlation_id=correlation_id))


def kinds(events):
    return [(e["component"].split(".")[-1], e["event_type"]) for e in events]


def failing_append(after):
    """Event-file writes succeed `after` times, then fail like a full disk (only event files are affected)."""
    calls = []

    def append(writer, event):
        calls.append(event["sequence"])
        if len(calls) > after:
            raise NetworkError("event_persistence_failed", "Could not save an execution event.")
        return REAL_APPEND(writer, event)
    return append


def strip(value, keys=("updated_at", "created_at", "timestamp", "task_id", "stage_task_id", "run_id", "parent_task_id")):
    if isinstance(value, dict):
        return {k: strip(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [strip(v, keys) for v in value]
    return value


class WorkflowBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = RunStore(self.root / "runtime/runs")
        self.task = read_json(ROOT / "examples/workflow-task.json")
        self.provider = Provider()
        guard = patch("socket.socket.connect", side_effect=AssertionError("No API calls"))
        guard.start()
        self.addCleanup(guard.stop)

    def runs(self, events=None):
        return SavedRuns(self.store, events=events)

    def persistent(self):
        return ContentEvents.persistent("workflow", self.root)

    def start(self, events=None):
        return self.runs(events).start(self.task, providers={"mock": self.provider})

    def resume(self, run_id, events=None, **kwargs):
        return self.runs(events).resume(run_id, providers={"mock": self.provider}, **kwargs)


class WorkflowRecordingTests(WorkflowBase):
    def test_actual_role_events(self):
        events = memory("workflow")
        result = self.start(events)
        self.assertEqual(result["status"], "completed")
        recorded = events.recorder.events
        self.assertEqual(kinds(recorded), [
            ("orchestrator", "stage_started"), ("researcher", "stage_started"), ("researcher", "stage_completed"),
            ("analyst", "stage_started"), ("analyst", "stage_completed"), ("reviewer", "stage_started"),
            ("reviewer", "stage_completed"), ("orchestrator", "stage_completed")])
        reference = "wfr-" + result["run_id"]
        self.assertTrue(all(e["run_id"] == reference and e["correlation_id"] == correlation_for(reference) for e in recorded))
        self.assertEqual(recorded[-1]["refs"], [{"kind": "workflow_run", "id": reference}])
        self.assertEqual(result["events"]["completeness"], "complete")
        self.assertEqual(self.provider.calls, ["researcher", "analyst", "reviewer"])

    def test_outputs_unchanged_by_recording(self):
        plain = self.start()
        plain_state = self.store.read(plain["run_id"])
        self.task = dict(self.task, task_id="workflow-002")
        recorded = self.start(self.persistent())
        recorded_state = self.store.read(recorded["run_id"])
        self.assertEqual(set(plain), {"run_id", "status", "task"})
        self.assertEqual(strip({k: v for k, v in recorded.items() if k != "events"}), strip(plain))
        self.assertEqual(strip(recorded_state), strip(plain_state))

    def test_one_shot_command(self):
        runner = Orchestrator(registry_path="config/agents.workflow.json", providers={"mock": self.provider})
        runner.events = memory("workflow")
        outcome = runner.run(dict(self.task))
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(runner.events.recorder.events[0]["correlation_id"], correlation_for(self.task["task_id"]))
        self.assertIsNone(runner.events.recorder.events[0]["run_id"])

    def test_failure_then_resume_attempts(self):
        self.provider.failure = "provider_error"
        first = self.start(self.persistent())
        self.assertEqual(first["status"], "uncertain")
        self.provider.failure = None
        second = self.resume(first["run_id"], self.persistent(), retry_uncertain=True)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.provider.calls, ["researcher", "analyst", "analyst", "reviewer"])   # researcher ran once
        one = EventStore(self.root).load(first["events"]["timeline_id"])
        two = EventStore(self.root).load(second["events"]["timeline_id"])
        self.assertEqual(kinds(one["events"])[-3:], [("analyst", "stage_started"), ("analyst", "stage_failed"),
                                                     ("orchestrator", "stage_failed")])
        self.assertEqual(kinds(two["events"]), [
            ("orchestrator", "stage_started"), ("researcher", "stage_reused"), ("analyst", "stage_started"),
            ("analyst", "stage_completed"), ("reviewer", "stage_started"), ("reviewer", "stage_completed"),
            ("orchestrator", "stage_completed")])
        retried = two["events"][2]
        self.assertEqual((retried["details"]["attempt"], retried["reason_codes"]), (2, ["retry_after_uncertain"]))
        self.assertEqual(two["events"][1]["reason_codes"], ["completed_in_earlier_attempt"])
        self.assertEqual(one["correlation_id"], two["correlation_id"])
        self.assertNotEqual(one["timeline_id"], two["timeline_id"])
        # Duplicate prevention: in every attempt each role starts at most once.
        for view in (one, two):
            starts = [e["component"] for e in view["events"] if e["event_type"] == "stage_started"]
            self.assertEqual(len(starts), len(set(starts)))
        listing = {row["id"]: row for row in api.timelines(self.root)["items"]}
        self.assertEqual((listing[one["timeline_id"]]["attempt"], listing[two["timeline_id"]]["attempt"]), (1, 2))
        self.assertEqual(listing[two["timeline_id"]]["attempts"], 2)
        self.assertEqual(listing["wfr-" + first["run_id"]]["correlation_id"], one["correlation_id"])

    def test_interrupted_attempt_then_resume(self):
        self.provider.failure = "interrupt"
        events = self.persistent()
        with self.assertRaises(KeyboardInterrupt):
            self.start(events)
        events.recorder.writer.abandon()                         # the interrupted process is gone
        run_id = next(p.stem for p in self.store.directory.glob("*.json"))
        first = EventStore(self.root).load(events.recorder.timeline_id)
        self.assertEqual((first["completeness"], first["live"]), ("interrupted", False))
        self.assertEqual([c["display_state"] for c in first["components"] if c["component"].endswith("analyst")], ["unknown"])
        self.provider.failure = None
        second = self.resume(run_id, self.persistent(), retry_uncertain=True)
        two = EventStore(self.root).load(second["events"]["timeline_id"])
        analyst = next(e for e in two["events"] if e["event_type"] == "stage_started" and e["stage"] == "analyst")
        self.assertEqual(analyst["reason_codes"], ["retry_after_uncertain"])
        self.assertEqual(self.provider.calls.count("researcher"), 1)

    def test_completed_run_records_nothing_new(self):
        result = self.start()
        events = self.persistent()
        with self.assertRaises(NetworkError):
            self.resume(result["run_id"], events)
        self.assertIsNone(events.recorder)                       # no timeline was opened
        self.assertFalse((self.root / "runtime/events/content").exists())

    def test_storage_failure_keeps_committed_results(self):
        # orchestrator started, researcher started, then "researcher completed" cannot be saved
        with patch.object(TimelineWriter, "append", autospec=True, side_effect=failing_append(2)):
            result = self.start(self.persistent())
        self.assertEqual(result["error"]["code"], "event_persistence_failed")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.provider.calls, ["researcher"])            # no new work after the failure
        saved = self.store.read(result["run_id"])
        self.assertEqual([row["agent"] for row in saved["history"]], ["researcher"])   # committed result kept
        view = EventStore(self.root).load(result["events"]["timeline_id"])
        self.assertEqual((view["completeness"], view["outcome"]), ("partial", "persistence_failed"))
        resumed = self.resume(result["run_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(self.provider.calls, ["researcher", "analyst", "reviewer"])    # nothing repeated

    def test_storage_failure_before_any_work(self):
        with patch("vicekrack.events.store.os.link", side_effect=OSError("disk full")):   # even the manifest fails
            result = self.start(self.persistent())
        self.assertEqual((result["error"]["code"], result["status"], self.provider.calls),
                         ("event_persistence_failed", "ready", []))

    def test_cli_flags(self):
        path = self.root / "task.json"
        path.write_text(json.dumps(self.task), encoding="utf-8")
        from vicekrack.__main__ import main
        output = io.StringIO()
        with patch("sys.argv", ["vicekrack", str(path), "--registry", "config/agents.workflow.json", "--record-events"]), \
                patch.object(ContentEvents, "persistent", side_effect=lambda kind, root=None: REAL_PERSISTENT(kind, self.root)), \
                redirect_stdout(output):
            code = main()
        printed = json.loads(output.getvalue())
        self.assertEqual((code, printed["task"]["status"], printed["events"]["completeness"]), (0, "completed", "complete"))


class WorkflowReconstructionTests(WorkflowBase):
    def test_saved_run_reconstruction(self):
        self.provider.failure = "provider_error"
        first = self.start()
        self.provider.failure = None
        self.resume(first["run_id"], retry_uncertain=True)
        state = self.store.read(first["run_id"])
        view = from_saved_run(state)
        self.assertEqual((view["origin"], view["time_basis"], view["completeness"]), ("reconstructed", "source_recorded", "complete"))
        self.assertEqual(kinds(view["events"]), [
            ("researcher", "stage_started"), ("researcher", "stage_completed"), ("analyst", "stage_started"),
            ("analyst", "stage_failed"), ("analyst", "stage_started"), ("analyst", "stage_completed"),
            ("reviewer", "stage_started"), ("reviewer", "stage_completed")])
        self.assertTrue(all(len(e["recorded_at"]) == 20 for e in view["events"]))   # the run's own times, to the second
        self.assertEqual(view, from_saved_run(state))                                # deterministic
        self.assertEqual(load_timeline("wfr-" + first["run_id"], self.root)["timeline_id"], view["timeline_id"])
        legacy = dict(state)
        legacy.pop("workflow_state")
        old = from_saved_run(legacy)
        self.assertEqual((old["time_basis"], old["issues"]), ("none_saved", ["legacy_run_without_times"]))
        self.assertTrue(all(e["recorded_at"] is None for e in old["events"]))

    def test_listing_never_creates_folders(self):
        before = sorted(p.name for p in self.root.iterdir())
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        api.timelines(Path(empty.name))
        self.assertEqual(list(Path(empty.name).iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)


class ProductionRecordingTests(ProductionBase):
    def make(self, renderer=None, drafter=None, events=None):
        self.renderer = renderer or FakeRenderer()
        return Pipeline(root=self.root, clock=lambda: self.now, renderer=self.renderer, drafter=drafter, events=events)

    def test_pipeline_events_and_unchanged_state(self):
        events = memory("production")
        result = self.make(events=events).produce(self.run_id, self.record_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(kinds(events.recorder.events), [
            ("pipeline", "stage_started"), ("brief", "stage_started"), ("brief", "stage_completed"),
            ("creator", "stage_started"), ("creator", "stage_completed"), ("validate", "stage_started"),
            ("validate", "stage_completed"), ("plan", "stage_started"), ("plan", "stage_completed"),
            ("preview", "stage_started"), ("preview", "stage_completed"), ("pipeline", "stage_completed")])
        self.assertEqual(events.recorder.events[0]["correlation_id"], correlation_for(result["production_id"]))
        self.assertNotIn("events", result)                      # the pipeline summary contract is unchanged

    def test_resume_reports_reused_stages(self):
        drafter = CountingDrafter()
        first = self.make(FakeRenderer([NetworkError("render_failed", "x")]), drafter, memory("production"))
        result = first.produce(self.run_id, self.record_id)
        self.assertEqual(result["status"], "failed")
        events = memory("production")
        second = self.make(drafter=drafter, events=events).resume(result["production_id"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(drafter.calls, 1)
        self.assertEqual(kinds(events.recorder.events), [
            ("pipeline", "stage_started"), ("brief", "stage_reused"), ("creator", "stage_reused"),
            ("validate", "stage_reused"), ("plan", "stage_reused"), ("preview", "stage_started"),
            ("preview", "stage_completed"), ("pipeline", "stage_completed")])
        preview = events.recorder.events[5]
        self.assertEqual((preview["details"]["attempt"], preview["reason_codes"]), (2, ["retry_after_failure"]))
        self.assertEqual(first.events.recorder.events[0]["correlation_id"], events.recorder.events[0]["correlation_id"])

    def test_storage_failure_never_repeats_a_paid_request(self):
        drafter = CountingDrafter()
        events = ContentEvents.persistent("production", self.root)
        # pipeline, brief started, brief done, creator started, then "creator completed" cannot be saved
        with patch.object(TimelineWriter, "append", autospec=True, side_effect=failing_append(4)):
            result = self.make(drafter=drafter, events=events).produce(self.run_id, self.record_id, allow_paid=True,
                                                                       paths=PAID)
        self.assertEqual(drafter.calls, 1)
        state = ProductionStore(self.root).read(result["production_id"])
        self.assertEqual([s["status"] for s in state["stages"]], ["completed", "completed", "pending", "pending", "pending"])
        self.assertEqual(result["error"], {"stage": "validate", "code": "event_persistence_failed"})
        resumed = self.make(drafter=drafter).resume(result["production_id"], allow_paid=True)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(drafter.calls, 1)                       # the paid draft was never requested again

    def test_liveness_changes(self):
        events = ContentEvents.persistent("production", self.root)
        seen = {}

        class Watching(FakeRenderer):
            def __call__(inner, plan, **kwargs):
                scene = json.loads(api.respond("GET", "/api/scene?timeline=" + events.recorder.timeline_id,
                                               {"host": f"127.0.0.1:{PORT}"}, port=PORT, root=self.root)[2])
                seen["during"] = (scene["mode"], scene["current"]["room_states"]["creator"],
                                  scene["current"]["component_states"]["content.production.preview"])
                return FakeRenderer.__call__(inner, plan, **kwargs)
        self.make(Watching(), events=events).produce(self.run_id, self.record_id)
        events.close("completed")
        self.assertEqual(seen["during"], ("observed", "completed", "working"))
        after = scene_from_view(EventStore(self.root).load(events.recorder.timeline_id))
        self.assertEqual((after["mode"], after["timeline"]["completeness"]), ("recorded_replay", "complete"))
        self.assertEqual(after["current"]["component_states"]["content.production.preview"], "completed")

    def test_hq_attribution_for_recorded_production(self):
        events = ContentEvents.persistent("production", self.root)
        self.make(events=events).produce(self.run_id, self.record_id)
        events.close("completed")
        scene = scene_from_view(EventStore(self.root).load(events.recorder.timeline_id))
        rooms = {r["room"]: r["has_activity"] for r in scene["rooms"]}
        self.assertEqual({r for r, active in rooms.items() if active}, {"creator", "operations"})
        self.assertFalse(rooms["researcher"] or rooms["analyst"] or rooms["reviewer"])
        stations = {s["station"] for s in scene["stations"] if s["has_activity"]}
        self.assertEqual(stations, {"production_pipeline", "brief_builder", "script_validator", "scene_planner",
                                    "preview_renderer"})


class QualityRecordingTests(QualityBase):
    def test_quality_report_events(self):
        self.produce(renderer=ManifestRenderer())
        events = memory("quality")
        report, _ = QualityChecker(root=self.root, clock=lambda: self.now, prober=lambda path: dict(self.media),
                                   events=events).run(self.pid)
        recorded = events.recorder.events
        self.assertEqual(kinds(recorded), [("quality", "stage_started"), ("quality", "stage_completed")])
        self.assertEqual(recorded[-1]["reason_codes"], ["result_" + report["result"]])
        self.assertIn({"kind": "quality_report", "id": report["report_id"]}, recorded[-1]["refs"])
        self.assertEqual(recorded[0]["correlation_id"], correlation_for(self.pid))   # joins the production's attempts

    def test_quality_failure_before_checks(self):
        self.produce(renderer=ManifestRenderer())
        broken = ContentEvents("quality", lambda run_id, correlation_id: (_ for _ in ()).throw(
            NetworkError("event_storage_full", "full")))
        with self.assertRaises(EventFailure):
            QualityChecker(root=self.root, clock=lambda: self.now, events=broken).run(self.pid)
        self.assertFalse((self.root / "runtime/quality").exists())


class WorkflowHqTests(WorkflowBase):
    def test_roles_drive_their_rooms(self):
        events = self.persistent()
        result = self.start(events)
        view = EventStore(self.root).load(result["events"]["timeline_id"])
        scene = scene_from_view(view)
        active = {r["room"] for r in scene["rooms"] if r["has_activity"]}
        self.assertEqual(active, {"researcher", "analyst", "reviewer", "operations"})
        self.assertEqual({s["station"] for s in scene["stations"] if s["has_activity"]}, {"workflow_orchestrator"})
        handoffs = [(f["handoff"]["from"], f["handoff"]["to"]) for f in scene["frames"] if f["handoff"]]
        self.assertEqual(handoffs, [("operations", "researcher"), ("researcher", "analyst"), ("analyst", "reviewer"),
                                    ("reviewer", "operations")])
        first = scene["frames"][0]["room_states"]
        self.assertEqual([first[r] for r in ("researcher", "analyst", "reviewer")], ["waiting"] * 3)
        status, _, body = api.respond("GET", "/api/scene?timeline=wfr-" + result["run_id"], {"host": f"127.0.0.1:{PORT}"},
                                      port=PORT, root=self.root)
        self.assertEqual((status, json.loads(body)["mode"]), (200, "reconstructed"))
        self.assertEqual(api.respond("GET", "/api/scene?timeline=wfr-" + "a" * 31, {"host": f"127.0.0.1:{PORT}"},
                                     port=PORT, root=self.root)[0], 400)


class DecorationIsolationTests(unittest.TestCase):
    def test_decoration_cannot_change_status(self):
        app = (ROOT / "vicekrack/hq/static/app.js").read_text(encoding="utf-8")
        decoration = app[app.index("function targetFor"):app.index("function spawnToken")]
        for forbidden in ("getJSON", "fetch", "loadScene", "S.replay", "S.scene", ".state =", "renderStatuses"):
            self.assertNotIn(forbidden, decoration)
        self.assertIn("movementFor", app)                          # the inspector explains movement in words
        self.assertIn("occupancy", app)


if __name__ == "__main__":
    unittest.main()
