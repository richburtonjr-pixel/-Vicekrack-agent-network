"""Step 31: execution events, timelines and replay. Local synthetic data only; no network, no credits.

Expected transition rules are written out independently here as a table, not read from
the module.
"""

import hashlib
import io
import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from test_production import NOW as CONTENT_NOW, Base as ProductionBase, FakeRenderer
from test_simulation import BARS, NOW, Base as SimulationBase
from vicekrack.errors import NetworkError
from vicekrack.events.cli import main as events_main
from vicekrack.events.contract import STATES, TRANSITIONS, apply, display, event_id
from vicekrack.events.sink import NullSink, Recorder
from vicekrack.events.store import EventStore, load_events_config
from vicekrack.production import Pipeline
from vicekrack.production_timeline import load_production_timeline
from vicekrack.trading.agents.cli import main as agent_main
from vicekrack.trading.agents.controller import run_workflow
from vicekrack.trading.agents.handlers import default_handlers
from vicekrack.trading.agents.store import load_agent_config
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.market.store import make_adapter
from vicekrack.trading.simulation.cli import main as sim_main
from vicekrack.trading.simulation.engine import run_simulation
from vicekrack.trading.simulation.store import SimulationStore
from vicekrack.trading.timeline import SIM_COMPONENTS, from_agent_run, from_simulation_run

# Independent statement of the documented transition table: event type -> (allowed from, to).
RULES = {"stage_started": ({"idle", "failed", "unknown"}, "working"), "stage_completed": ({"working"}, "completed"),
         "stage_failed": ({"working"}, "failed"), "stage_blocked": ({"idle"}, "blocked"),
         "stage_interrupted": ({"working"}, "unknown"), "order_decision": ({"working"}, "working"),
         "simulated_fill": ({"working"}, "working")}
STATUS = {"stage_started": "started", "stage_completed": "completed", "stage_failed": "failed", "stage_blocked": "blocked",
          "stage_interrupted": "interrupted", "order_decision": "accepted", "simulated_fill": "filled"}
ENGINE = "trading.simulation.engine"


def tree(root):
    """Every file under root with its content hash (to prove read-only commands change nothing)."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


def memory(components=SIM_COMPONENTS, **kwargs):
    return Recorder(department="trading", kind="simulation", components=components, clock=lambda: NOW, **kwargs)


class EventBase(SimulationBase):
    def setUp(self):
        super().setUp()
        self.config = load_events_config()
        self.agent_config, _ = load_agent_config()

    def events_store(self, **limits):
        config = deepcopy(self.config)
        config["limits"].update(limits)
        return EventStore(self.root, config)

    def recorder(self, store=None, kind="simulation", components=SIM_COMPONENTS):
        return Recorder(department="trading", kind=kind, components=components, store=store or self.events_store(),
                        clock=lambda: NOW)

    def sim(self, events=None, bars=BARS):
        return run_simulation(self.dataset(bars), self.policy, market_config=self.market_config,
                              indicator_config=self.indicator_config, signal_config=self.signal_config,
                              kill_switch=(False, None), created_at=NOW, events=events)

    def agents(self, events=None, handlers=None):
        dataset = self.store.import_dataset(make_adapter("synthetic", config=self.market_config, fixture="synth1-5m-reclaim"),
                                            config=self.market_config, config_sha256="0" * 64) \
            if not hasattr(self, "_reclaim") else self._reclaim
        self._reclaim = dataset
        return run_workflow(dataset, workflow_config=self.agent_config, market_config=self.market_config,
                            indicator_config=self.indicator_config, signal_config=self.signal_config, created_at=NOW,
                            handlers=handlers, events=events)

    def cli(self, main, *argv, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(list(argv), root=self.root, **kwargs)
        return code, output.getvalue()

    def cli_json(self, main, *argv, **kwargs):
        code, text = self.cli(main, *argv, **kwargs)
        return code, json.loads(text)

    def assertNetCode(self, code, function, *args, **kwargs):
        with self.assertRaises(NetworkError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class ContractTests(EventBase):
    def test_transition_table(self):
        self.assertEqual(set(TRANSITIONS), set(RULES))
        for event_type, (allowed, target) in RULES.items():
            for state in STATES:
                states = {ENGINE: state}
                event = {"component": ENGINE, "event_type": event_type}
                with self.subTest(event_type=event_type, state=state):
                    if state in allowed:
                        apply(states, event)
                        self.assertEqual(states[ENGINE], target)
                    else:
                        self.assertNetCode("invalid_event_transition", apply, states, event)

    def test_recorder_rejects_invalid_transitions(self):
        for sequence in (["stage_completed"], ["stage_started", "stage_started"], ["stage_started", "stage_blocked"],
                         ["order_decision"], ["stage_started", "stage_completed", "stage_started"]):
            recorder = memory()
            with self.subTest(sequence=sequence):
                with self.assertRaises(NetworkError) as caught:
                    for event_type in sequence:
                        recorder.emit(ENGINE, "replay", event_type, STATUS[event_type])
                self.assertEqual(caught.exception.code, "invalid_event_transition")
                self.assertEqual(recorder.summary()["completeness"], "partial")
                self.assertNetCode("invalid_event_transition", recorder.emit, ENGINE, "replay", "stage_started", "started")

    def test_sanitized_payloads(self):
        bad = [dict(details={"prompt": "write a script about ..."}), dict(details={"strategy": "/home/user/file"}),
               dict(reason_codes=["Raw exception: boom"]), dict(refs=[{"kind": "file", "id": "/etc/passwd"}]),
               dict(refs=[{"kind": "order", "id": "sord-../../x"}]), dict(details={"apiKey": "x"}),
               dict(details={"strategy": "sk-" + "abcdefghij" * 3})]
        for fields in bad:
            with self.subTest(fields=fields):
                recorder = memory()
                self.assertNetCode("invalid_event", recorder.emit, ENGINE, "replay", "stage_started", "started", **fields)
        recorder = memory()
        self.assertNetCode("invalid_event", recorder.emit, "content.production.brief", "brief", "stage_started", "started")
        recorder = memory()
        self.assertNetCode("invalid_event", recorder.emit, ENGINE, "replay", "stage_started", "completed")

    def test_event_ids_and_order(self):
        recorder = memory()
        run = self.sim(recorder)
        events = recorder.events
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({e["event_id"] for e in events}), len(events))
        self.assertTrue(all(e["event_id"] == event_id(e["timeline_id"], e["sequence"]) for e in events))
        self.assertEqual((events[0]["event_type"], events[-1]["event_type"]), ("stage_started", "stage_completed"))
        fills = [e for e in events if e["event_type"] == "simulated_fill"]
        self.assertEqual([f["refs"][0]["id"] for f in fills], [f["fill_id"] for f in run["fills"]])
        # Each fill comes after the accepted decision for its order.
        for fill in fills:
            order = fill["refs"][1]["id"]
            decision = next(e for e in events if e["event_type"] == "order_decision" and e["refs"][0]["id"] == order)
            self.assertLess(decision["sequence"], fill["sequence"])
        self.assertTrue(all(e["recorded_at"] == NOW and e["run_id"] == run["run_id"] for e in events))

    def test_display_rules(self):
        last = {ENGINE: 3}
        self.assertEqual(display({ENGINE: "working"}, last, live=True, degraded=False)[0]["display_state"], "working")
        row = display({ENGINE: "working"}, last, live=False, degraded=False)[0]
        self.assertEqual((row["display_state"], row["note"]), ("unknown", "not_live_last_known_working"))
        for state, shown in (("completed", "completed"), ("blocked", "blocked"), ("failed", "unknown"), ("idle", "unknown")):
            self.assertEqual(display({ENGINE: state}, last, live=False, degraded=True)[0]["display_state"], shown)


class CompatibilityTests(EventBase):
    def test_results_and_hashes_unchanged(self):
        plain = self.sim()
        recorder = self.recorder()
        recorded = self.sim(recorder)
        recorder.close("completed")
        self.assertEqual(plain, recorded)
        self.assertEqual(self.sim(NullSink()), plain)
        plain_agents = self.agents()
        self.assertEqual(self.agents(memory(("trading.research.controller", "trading.research.market_scout",
                                             "trading.research.trend_agent", "trading.research.strategy_agent",
                                             "trading.research.risk_review"))), plain_agents)

    def test_commands_unchanged_without_flag(self):
        dataset = self.dataset()
        code, output = self.cli_json(sim_main, "sim-run", dataset["dataset_id"])
        self.assertEqual(code, 0)
        self.assertNotIn("events", output)
        self.assertFalse((self.root / "runtime/events").exists())


class AgentEventTests(EventBase):
    COMPONENTS = ("trading.research.controller", "trading.research.market_scout", "trading.research.trend_agent",
                  "trading.research.strategy_agent", "trading.research.risk_review")

    def test_completed_workflow_events(self):
        recorder = memory(self.COMPONENTS)
        run = self.agents(recorder)
        kinds = [(e["component"].split(".")[-1], e["event_type"]) for e in recorder.events]
        expected = [("controller", "stage_started")]
        for role in ("market_scout", "trend_agent", "strategy_agent", "risk_review"):
            expected += [(role, "stage_started"), (role, "stage_completed")]
        self.assertEqual(kinds, expected + [("controller", "stage_completed")])
        self.assertEqual(recorder.events[-1]["refs"], [{"kind": "research_agent_run", "id": run["run_id"]}])
        reconstructed = from_agent_run(run)
        strip = lambda e: {k: e[k] for k in ("component", "event_type", "status", "sim_time_utc", "reason_codes", "details")}
        self.assertEqual([strip(e) for e in reconstructed["events"]], [strip(e) for e in recorder.events])

    def test_failed_stage_blocks_later_stages(self):
        handlers = default_handlers()
        handlers[1].analyze = lambda *args: (_ for _ in ()).throw(RuntimeError("boom /home/secret"))
        recorder = memory(self.COMPONENTS)
        run = self.agents(recorder, handlers)
        self.assertEqual(run["status"], "failed")
        kinds = [(e["component"].split(".")[-1], e["event_type"]) for e in recorder.events]
        self.assertEqual(kinds[3:], [("trend_agent", "stage_started"), ("trend_agent", "stage_failed"),
                                     ("strategy_agent", "stage_blocked"), ("risk_review", "stage_blocked"),
                                     ("controller", "stage_failed")])
        self.assertEqual(recorder.events[4]["reason_codes"], ["stage_error"])
        self.assertNotIn("boom", json.dumps(recorder.events))
        self.assertNotIn("/home", json.dumps(recorder.events))
        states = {c: s for c, s in recorder.states.items()}
        self.assertEqual([states[c] for c in self.COMPONENTS], ["failed", "completed", "failed", "blocked", "blocked"])


class PersistenceTests(EventBase):
    def test_recorded_timeline_round_trip_and_cli(self):
        dataset = self.dataset()
        code, output = self.cli_json(sim_main, "sim-run", dataset["dataset_id"], "--save", "--record-events")
        self.assertEqual(code, 0)
        self.assertEqual((output["events"]["completeness"], output["events"]["outcome"]), ("complete", "completed"))
        timeline = output["events"]["timeline_id"]
        folder = self.root / "runtime/events/trading/timelines" / timeline
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["closed.json", "events", "manifest.json", "writer.lock"])
        view = EventStore(self.root).load(timeline)
        self.assertEqual((view["completeness"], view["live"], view["issues"], view["run_id"]),
                         ("complete", False, [], output["run_id"]))
        for path in (folder / "events").iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(str(self.root), text)
            self.assertNotIn("\\\\", text)
        self.assertFalse((self.root / "runtime/events/content").exists())     # departments stay separate

    def test_duplicates(self):
        store = self.events_store()
        recorder = self.recorder(store)
        recorder.emit(ENGINE, "replay", "stage_started", "started")
        self.assertNetCode("event_duplicate", recorder.writer.append, recorder.events[0])
        manifest = dict(recorder.writer.manifest)
        self.assertNetCode("event_timeline_exists", store.open, manifest)
        decision = dict(sim_time=NOW, refs=[{"kind": "order", "id": "sord-" + "a" * 24}])
        other = self.recorder(store)
        other.emit(ENGINE, "replay", "stage_started", "started")
        other.emit(ENGINE, "replay", "order_decision", "accepted", **decision)
        self.assertNetCode("event_duplicate", other.emit, ENGINE, "replay", "order_decision", "accepted", **decision)
        self.assertEqual(store.load(other.timeline_id)["outcome"], "persistence_failed")
        recorder.close("completed")

    def test_concurrent_writers(self):
        store = self.events_store()
        errors, ids = [], []

        def work():
            try:
                recorder = Recorder(department="trading", kind="simulation", components=SIM_COMPONENTS,
                                    store=EventStore(self.root, store.config))
                recorder.emit(ENGINE, "replay", "stage_started", "started")
                for number in range(30):
                    recorder.emit(ENGINE, "replay", "order_decision", "rejected",
                                  refs=[{"kind": "order", "id": f"sord-{number:024x}"}])
                recorder.close("completed")
                ids.append(recorder.timeline_id)
            except Exception as error:                     # noqa: BLE001 - reported below
                errors.append(error)
        threads = [threading.Thread(target=work) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        for timeline in ids:
            view = store.load(timeline)
            self.assertEqual((view["completeness"], view["event_count"]), ("complete", 31))

    def test_live_then_interrupted(self):
        recorder = self.recorder()
        recorder.emit(ENGINE, "replay", "stage_started", "started")
        view = EventStore(self.root).load(recorder.timeline_id)
        self.assertEqual((view["completeness"], view["live"], view["components"][0]["display_state"]),
                         ("open", True, "working"))
        recorder.writer.abandon()                          # what a crashed process leaves behind
        view = EventStore(self.root).load(recorder.timeline_id)
        self.assertEqual((view["completeness"], view["live"], view["outcome"]), ("interrupted", False, None))
        self.assertEqual(view["components"][0]["display_state"], "unknown")

    def test_partial_timelines(self):
        store = self.events_store()
        recorder = self.recorder(store)
        self.sim(recorder)
        recorder.close("completed")
        folder = store.folder("trading", recorder.timeline_id)
        (folder / "events" / "000003.json").unlink()
        view = store.load(recorder.timeline_id)
        self.assertEqual(view["completeness"], "partial")
        self.assertIn("missing_events", view["issues"])
        self.assertEqual(view["event_count"], 2)                    # only the contiguous prefix is shown
        self.assertEqual(view["components"][0]["display_state"], "unknown")
        (folder / "events" / "000002.json").write_text('{"contract": "execution_event", "extra": 1}', encoding="utf-8")
        (folder / "events" / "000009.json.tmp").write_text("{", encoding="utf-8")
        view = store.load(recorder.timeline_id)
        self.assertTrue({"corrupt_event", "interrupted_write_debris", "event_count_mismatch"} <= set(view["issues"]))
        self.assertEqual(view["completeness"], "partial")

    def test_persistence_failure_is_explicit(self):
        dataset = self.dataset()
        real_link, calls = os.link, []

        def flaky(source, target):
            calls.append(target)
            if len(calls) == 3:                           # manifest, event 1, then event 2 fails
                raise OSError("disk full /private/path")
            return real_link(source, target)
        with patch("vicekrack.events.store.os.link", side_effect=flaky):
            code, output = self.cli_json(sim_main, "sim-run", dataset["dataset_id"], "--save", "--record-events")
        self.assertEqual((code, output["error"]["code"]), (1, "event_persistence_failed"))
        self.assertNotIn("/private", json.dumps(output))
        self.assertEqual((output["events"]["completeness"], output["events"]["outcome"]), ("partial", "persistence_failed"))
        self.assertEqual(SimulationStore(self.root).list(), [])           # nothing saved
        view = EventStore(self.root).load(output["events"]["timeline_id"])
        self.assertEqual((view["completeness"], view["outcome"], view["event_count"]), ("partial", "persistence_failed", 1))
        # When even the close marker cannot be written, the timeline reads as interrupted, never complete.
        calls.clear()
        reclaim = self.agents()["dataset"]["dataset_id"]           # imported before os.link is patched (patch is global)

        def broken_after_manifest(source, target):
            calls.append(target)
            if len(calls) > 2:
                raise OSError("disk")
            return real_link(source, target)
        with patch("vicekrack.events.store.os.link", side_effect=broken_after_manifest):
            code, output = self.cli_json(agent_main, "agent-run", reclaim, "--record-events")
        self.assertEqual((code, output["error"]["code"]), (1, "event_persistence_failed"))
        view = EventStore(self.root).load(output["events"]["timeline_id"])
        self.assertEqual((view["completeness"], view["live"]), ("interrupted", False))

    def test_event_limit_and_retention(self):
        recorder = Recorder(department="trading", kind="simulation", components=SIM_COMPONENTS,
                            store=self.events_store(max_events_per_timeline=10), clock=lambda: NOW)
        with self.assertRaises(TradingError) as caught:
            self._limit(recorder)
        self.assertEqual(caught.exception.code, "event_limit_reached")
        self.assertEqual(EventStore(self.root).load(recorder.timeline_id)["outcome"], "event_limit_reached")
        store = self.events_store(max_timelines_per_department=3)
        made = []
        for minute in range(5):
            recorder = Recorder(department="trading", kind="simulation", components=SIM_COMPONENTS, store=store,
                                clock=lambda m=minute: f"2026-10-0{m + 1}T00:00:00Z")
            recorder.emit(ENGINE, "replay", "stage_started", "started")
            recorder.close("aborted")
            made.append(recorder.timeline_id)
        remaining = {row["timeline_id"] for row in store.list("trading")}
        self.assertTrue(set(made[-2:]) <= remaining)
        self.assertLessEqual(len(remaining), 3)
        self.assertFalse(set(made[:2]) & remaining)                         # oldest closed ones pruned
        crowded = self.events_store(max_timelines_per_department=1)
        for folder in crowded._folders("trading"):
            (folder / "closed.json").unlink()                                # nothing closed: nothing may be pruned
        self.assertNetCode("event_storage_full", self.recorder, crowded)

    def _limit(self, recorder):
        from vicekrack.trading.timeline import TradingEvents
        events = TradingEvents(recorder)
        events.emit(ENGINE, "replay", "stage_started", "started")
        for number in range(20):
            events.emit(ENGINE, "replay", "order_decision", "rejected", refs=[{"kind": "order", "id": f"sord-{number:024x}"}])


class ReconstructionTests(EventBase):
    def test_simulation_reconstruction_matches_recording(self):
        recorder = memory()
        run = self.sim(recorder)
        first, second = from_simulation_run(run), from_simulation_run(deepcopy(run))
        self.assertEqual(first, second)                                       # deterministic
        self.assertEqual((first["origin"], first["time_basis"], first["started_at"], first["live"]),
                         ("reconstructed", "simulated_only", None, False))
        self.assertTrue(all(e["recorded_at"] is None for e in first["events"]))   # no invented times
        strip = lambda e: {k: e[k] for k in ("component", "event_type", "status", "sim_time_utc", "reason_codes",
                                              "refs", "details")}
        self.assertEqual([strip(e) for e in first["events"]], [strip(e) for e in recorder.events])
        self.assertEqual(first["source"]["saved_at"], run["created_at"])

    def test_content_production_timeline(self):
        case = _ContentCase("run_content")
        case.setUp()
        try:
            case.run_content(self)
        finally:
            case.doCleanups()


class _ContentCase(ProductionBase):
    def run_content(self, outer):
        from vicekrack.errors import NetworkError as Err
        result = self.produce(self.pipeline(FakeRenderer([Err("render_failed", "x")])))
        production_id = result["production_id"]
        before = tree(self.root)
        view = load_production_timeline(production_id, self.root)
        outer.assertEqual(tree(self.root), before)                          # read-only: no lock or files created
        outer.assertEqual((view["department"], view["outcome"], view["completeness"], view["time_basis"]),
                          ("content", "failed", "complete", "source_recorded"))
        outer.assertEqual(view["components"][-1]["display_state"], "failed")
        outer.assertTrue(all(e["recorded_at"] == CONTENT_NOW for e in view["events"]))
        outer.assertEqual(load_production_timeline(production_id, self.root), view)
        try:
            Pipeline(root=self.root, clock=lambda: CONTENT_NOW, renderer=FakeRenderer([KeyboardInterrupt()])).resume(production_id)
        except KeyboardInterrupt:
            pass
        view = load_production_timeline(production_id, self.root)
        outer.assertEqual((view["completeness"], view["live"]), ("open", False))
        row = view["components"][-1]
        outer.assertEqual((row["display_state"], row["note"]), ("unknown", "not_live_last_known_working"))
        self.pipeline().resume(production_id)
        view = load_production_timeline(production_id, self.root)
        outer.assertEqual((view["outcome"], [c["display_state"] for c in view["components"]]),
                          ("completed", ["completed"] * 5))
        state = self.state(production_id)
        state["trace"] = state["trace"] * 20                                  # a full trace may have lost older entries
        from vicekrack.production_timeline import from_production
        state["trace"] = state["trace"][-100:]
        outer.assertIn("trace_may_be_truncated", from_production(state)["issues"])
        outer.assertEqual(from_production(state)["completeness"], "partial")


class ReplayAndSeparationTests(EventBase):
    def test_replay_has_no_side_effects(self):
        dataset = self.dataset()
        _, sim = self.cli_json(sim_main, "sim-run", dataset["dataset_id"], "--save", "--record-events")
        reclaim = self.agents()["dataset"]["dataset_id"]
        _, agents = self.cli_json(agent_main, "agent-run", reclaim, "--save")
        before = tree(self.root)
        sleeps = []
        forbidden = AssertionError("replay must not execute anything")
        with patch("vicekrack.trading.simulation.engine.run_simulation", side_effect=forbidden), \
                patch("vicekrack.trading.agents.controller.run_workflow", side_effect=forbidden), \
                patch("vicekrack.trading.simulation.engine.drive", side_effect=forbidden):
            for identifier in (sim["events"]["timeline_id"], sim["run_id"], agents["run_id"]):
                code, text = self.cli(events_main, "events-replay", identifier, "--delay-ms", "999999",
                                      sleep=sleeps.append)
                lines = [json.loads(line) for line in text.splitlines()]
                self.assertEqual(code, 0)
                self.assertEqual(lines[0]["delay_ms"], self.config["replay"]["max_delay_ms"])   # clipped
                frames = [line for line in lines if "frame" in line]
                self.assertEqual(lines[-1]["frames"], len(frames))
                self.assertEqual([f["event"]["sequence"] for f in frames], list(range(1, len(frames) + 1)))
            code, text = self.cli(events_main, "events-replay", sim["run_id"], "--max-events", "2", "--delay-ms", "0",
                                  sleep=sleeps.append)
            self.assertEqual(json.loads(text.splitlines()[-1])["frames"], 2)
            for command in (("events-list",), ("events-inspect", sim["events"]["timeline_id"]),
                            ("events-inspect", agents["run_id"], "--component", "trading.research.trend_agent")):
                self.assertEqual(self.cli_json(events_main, *command)[0], 0)
        self.assertEqual(tree(self.root), before)
        self.assertTrue(all(delay == self.config["replay"]["max_delay_ms"] / 1000 for delay in sleeps[:5]))
        listing = self.cli_json(events_main, "events-list")[1]
        self.assertEqual(listing["recorded"]["total"], 1)
        self.assertEqual({r["kind"] for r in listing["reconstructable"]["items"]}, {"simulation", "research_agent_workflow"})
        self.assertEqual(self.cli_json(events_main, "events-inspect", "nope")[1]["error"]["code"], "invalid_timeline_id")

    def test_department_separation(self):
        core = [ROOT / "vicekrack/events" / name for name in ("contract.py", "sink.py", "store.py", "__init__.py")]
        content_modules = [p for p in (ROOT / "vicekrack").glob("*.py") if p.name != "__main__.py"]
        for path in core:
            source = path.read_text(encoding="utf-8")
            imports = "\n".join(line for line in source.splitlines() if line.strip().startswith(("from ", "import ")))
            for forbidden in ("trading", "production", "creator", "scout", "selection", "openai", "anthropic",
                              "socket", "urllib", "requests", "subprocess", "threading"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, imports)
            for forbidden in ("os.environ", "while True", "time.sleep"):
                self.assertNotIn(forbidden, source)
        for path in content_modules:
            with self.subTest(file=path.name):
                self.assertNotIn("vicekrack.trading", path.read_text(encoding="utf-8"))
                self.assertNotIn("from .trading", path.read_text(encoding="utf-8"))
        trading_timeline = (ROOT / "vicekrack/trading/timeline.py").read_text(encoding="utf-8")
        for forbidden in ("production", "creator", "from ..state", "PaperAccount", "from ..agents", "from ..simulation"):
            self.assertNotIn(forbidden, trading_timeline.split('"""', 2)[-1])
        recorder = memory()
        self.assertNetCode("invalid_event", recorder.emit, "content.production.brief", "brief", "stage_started", "started")


if __name__ == "__main__":
    unittest.main()
