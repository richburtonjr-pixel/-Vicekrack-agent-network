"""Step 39: controlled end-to-end trading research sessions. Synthetic data only; no network, no credits.

Every stage reuses the existing Step 25-30 code. These tests check the session controls around
it: the fixed order, explicit failures and resume, duplicate and concurrent prevention, crash
recovery, tamper and configuration-change detection, research/simulation separation (no future
data), event-persistence failures, isolation and the read-only Living HQ view.
"""

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack.errors import NetworkError
from vicekrack.events.store import TimelineWriter
from vicekrack.hq import api
from vicekrack.hq.server import HQServer
from vicekrack.hq.sessions import check as check_session, session_document
from vicekrack.trading.agents.handlers import Handler, default_handlers
from vicekrack.trading.agents.store import AgentRunStore
from vicekrack.trading.analytics.store import AnalyticsStore
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.market.store import MarketStore, load_market_config, make_adapter
from vicekrack.trading.session import runner as runner_module
from vicekrack.trading.session.cli import main as session_main
from vicekrack.trading.session.manifest import validate_manifest
from vicekrack.trading.session.runner import Interrupted, SessionRunner, load_inputs
from vicekrack.trading.session.store import STAGES, SessionStore
from vicekrack.trading.session.view import describe, list_sessions
from vicekrack.trading.simulation.engine import run_simulation
from vicekrack.trading.simulation.store import SimulationStore
from vicekrack.trading.state import PaperAccount

NOW = "2026-10-08T12:00:00Z"
PORT = 8765
HOST = {"host": f"127.0.0.1:{PORT}"}
REAL_APPEND = TimelineWriter.append
REAL_SAVE = SessionStore.save_checkpoint
FIXTURE = json.loads((ROOT / "examples/trading/market/synth1-5m-reclaim.json").read_text(encoding="utf-8"))


def failing_append(after):
    calls = []

    def append(writer, event):
        calls.append(event["sequence"])
        if len(calls) > after:
            raise NetworkError("event_persistence_failed", "Could not save an execution event.")
        return REAL_APPEND(writer, event)
    return append


def crash_when(stage, status):
    """Simulate a process dying right before the checkpoint that would record `stage` as `status`."""
    def save(store, session_id, checkpoint):
        row = next(s for s in checkpoint["stages"] if s["stage"] == stage)
        if row["status"] == status:
            raise Interrupted()
        return REAL_SAVE(store, session_id, checkpoint)
    return save


def tree(root):
    base = Path(root)
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(base.rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.market_config, self.market_sha = load_market_config()
        self.markets = MarketStore(self.root, clock=lambda: NOW)

    def reclaim(self):
        if not hasattr(self, "_reclaim"):
            self._reclaim = self.markets.import_dataset(
                make_adapter("synthetic", config=self.market_config, fixture="synth1-5m-reclaim"),
                config=self.market_config, config_sha256=self.market_sha)
        return self._reclaim

    def csv_dataset(self, bars, name):
        lines = ["timestamp,open,high,low,close,volume"]
        lines += [f"{b['timestamp']},{b['open']},{b['high']},{b['low']},{b['close']},{b['volume']}" for b in bars]
        path = self.root / "inputs" / f"{name}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.markets.import_dataset(make_adapter("csv", config=self.market_config, file=str(path), symbol="SYNTH1"),
                                           config=self.market_config, config_sha256=self.market_sha, interval="5m",
                                           tz="America/New_York", label="synthetic")

    def runner(self, **kwargs):
        return SessionRunner(self.root, clock=lambda: NOW, **kwargs)

    def config_root(self, session=None, policy=None):
        """A private copy of config/ so a test can change one file."""
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        shutil.copytree(ROOT / "config", folder / "config")
        if session is not None:
            path = folder / "config/trading-session.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            for key, value in session.items():
                document[key].update(value)
            path.write_text(json.dumps(document), encoding="utf-8")
        if policy is not None:
            path = folder / "config/simulation.paper.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document.update(policy)
            path.write_text(json.dumps(document), encoding="utf-8")
        return folder

    def artifact_path(self, session_id, stage):
        return self.root / "runtime/trading/sessions" / session_id / f"artifacts/{STAGES.index(stage) + 1}-{stage}.json"

    def checkpoint(self, session_id):
        return SessionStore(self.root).load_checkpoint(session_id)

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)


def failing_strategy():
    handlers = default_handlers()

    def boom(evidence, prior):
        raise RuntimeError("handler crashed")
    handlers[2] = Handler("strategy_agent", boom)
    return handlers


# ---------------------------------------------------------------- the full workflow
class WorkflowTests(Base):
    def test_full_session_in_fixed_order(self):
        dataset = self.reclaim()
        out = self.runner().start(dataset["dataset_id"])
        self.assertEqual((out["status"], out["failure"], out["simulated"], out["paper_account_access"]),
                         ("completed", None, True, False))
        self.assertEqual([s["stage"] for s in out["stages"]], list(STAGES))
        self.assertTrue(all(s["status"] == "completed" and s["attempts"] == 1 for s in out["stages"]))
        view = describe(out["session_id"], self.root)
        self.assertTrue(all(s["verification"] == "verified" for s in view["stages"]))
        docs = view["documents"]
        research, sim, report, manifest = (docs["research_analysis"], docs["simulation"], docs["performance_analytics"],
                                           docs["hq_summary"])
        # each stage's record is also in its existing store (append-only), and the links agree
        self.assertEqual(AgentRunStore(self.root).load(research["run_id"])["results_sha256"], research["results_sha256"])
        self.assertEqual(SimulationStore(self.root).load(sim["run_id"])["results_sha256"], sim["results_sha256"])
        self.assertEqual(AnalyticsStore(self.root).load(report["report_id"])["source"]["run_id"], sim["run_id"])
        validate_manifest(manifest)
        self.assertEqual(manifest["results"]["simulation_run_id"], sim["run_id"])
        self.assertFalse(manifest["research_used_by_simulation"])
        # every recorded hash and version
        record = SessionStore(self.root).load_record(out["session_id"])
        self.assertEqual(record["dataset"]["bars_sha256"], dataset["bars_sha256"])
        self.assertEqual(set(record["inputs"]), {"session_config", "market_data", "indicators", "research_signals",
                                                 "research_agents", "simulation_policy", "analytics"})
        self.assertEqual(record["versions"]["research_handlers"], [h.identity for h in default_handlers()])
        for stage in view["stages"]:
            data = self.artifact_path(out["session_id"], stage["stage"]).read_bytes()
            self.assertEqual(stage["artifact"]["file_sha256"], hashlib.sha256(data).hexdigest())

    def test_time_domains_are_labelled_and_research_as_of_is_exact(self):
        dataset = self.reclaim()
        out = self.runner().start(dataset["dataset_id"])
        manifest = describe(out["session_id"], self.root)["manifest"]
        domains = manifest["time_domains"]
        self.assertEqual(domains["historical_research"]["as_of_utc"], dataset["last_available_utc"])
        self.assertEqual(domains["historical_research"]["relation_to_simulation"], "at_or_after_simulation_end")
        self.assertFalse(domains["historical_research"]["used_by_simulation"])
        self.assertEqual(domains["wall_clock"]["session_created_at"], NOW)
        self.assertEqual(domains["simulated_execution"]["start_utc"], dataset["first_start_utc"])
        for key in ("historical_data", "historical_research", "simulated_execution", "wall_clock"):
            self.assertTrue(domains[key]["label"])
        research = describe(out["session_id"], self.root)["documents"]["research_analysis"]
        self.assertTrue(all(s["sim_time_utc"] == dataset["last_available_utc"] for s in research["stages"]))

    def test_simulation_identical_to_a_standalone_run(self):
        """Research output never reaches the simulator: its run (and hash) equals sim-run's."""
        dataset = self.reclaim()
        out = self.runner().start(dataset["dataset_id"])
        sim = describe(out["session_id"], self.root)["documents"]["simulation"]
        inputs = {k: v["document"] for k, v in load_inputs().items()}
        alone = run_simulation(dataset, inputs["simulation_policy"], market_config=inputs["market_data"],
                               indicator_config=inputs["indicators"], signal_config=inputs["research_signals"],
                               kill_switch=(False, None), created_at="2020-01-01T00:00:00Z")
        self.assertEqual((alone["run_id"], alone["results_sha256"]), (sim["run_id"], sim["results_sha256"]))

    def test_existing_identical_records_are_kept_not_overwritten(self):
        dataset = self.reclaim()
        inputs = {k: v["document"] for k, v in load_inputs().items()}
        earlier = run_simulation(dataset, inputs["simulation_policy"], market_config=inputs["market_data"],
                                 indicator_config=inputs["indicators"], signal_config=inputs["research_signals"],
                                 kill_switch=(False, None), created_at="2020-01-01T00:00:00Z")
        SimulationStore(self.root).save(earlier)
        path = self.root / f"runtime/trading/simulation/runs/{earlier['run_id']}.json"
        before = path.read_bytes()
        out = self.runner().start(dataset["dataset_id"])
        self.assertEqual(out["stages"][2]["artifact"]["store"], "already_present")
        self.assertEqual(path.read_bytes(), before)

    def test_cli_start_list_inspect(self):
        dataset = self.reclaim()

        def cli(*argv):
            output = io.StringIO()
            with redirect_stdout(output):
                code = session_main(list(argv), root=self.root)
            return code, json.loads(output.getvalue())
        code, started = cli("trading-session-start", dataset["dataset_id"])
        self.assertEqual((code, started["status"]), (0, "completed"))
        self.assertIn(f"python -m vicekrack trading-session-inspect {started['session_id']}", started["next"])
        code, listed = cli("trading-session-list")
        self.assertEqual(listed["sessions"][0]["stages_completed"], 5)
        code, shown = cli("trading-session-inspect", started["session_id"], "--stage", "simulation")
        self.assertEqual((code, shown["artifact"]["contract"]), (0, "simulation_run"))
        self.assertIsNone(shown["integrity_problem"])
        self.assertNotIn("document", json.dumps(shown["inputs"]))
        self.assertEqual(cli("trading-session-start", dataset["dataset_id"])[1]["error"]["code"], "session_exists")
        self.assertEqual(cli("trading-session-resume", started["session_id"])[1]["error"]["code"], "session_completed")
        self.assertEqual(cli("trading-session-inspect", "tss-bad")[1]["error"]["code"], "invalid_session_id")
        self.assertEqual(cli("trading-session-start", "mds-" + "0" * 24)[1]["error"]["code"], "dataset_not_found")
        self.assertEqual(cli("trading-session-start", dataset["dataset_id"], "--config", "../x.json")[1]["error"]["code"],
                         "invalid_session_config")


# ---------------------------------------------------------------- failures and resume
class FailureAndResumeTests(Base):
    def test_stage_failure_stops_without_retry_and_resume_continues(self):
        dataset = self.reclaim()
        calls = []
        real = runner_module.run_workflow

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)
        with patch.object(runner_module, "run_workflow", counting):
            out = self.runner(handlers=failing_strategy()).start(dataset["dataset_id"])
        self.assertEqual(out["failure"], {"stage": "research_analysis", "code": "research_workflow_failed"})
        self.assertEqual(len(calls), 1)                                      # no automatic retry
        self.assertEqual([s["status"] for s in out["stages"]], ["completed", "failed", "pending", "pending", "pending"])
        self.assertFalse(self.artifact_path(out["session_id"], "research_analysis").exists())
        self.assertEqual(AgentRunStore(self.root).list(), [])
        self.assertEqual(list_sessions(self.root)[0][0]["status"], "failed")
        resumed = self.runner().resume(out["session_id"])
        self.assertEqual(resumed["status"], "completed")
        research = self.checkpoint(out["session_id"])["stages"][1]
        self.assertEqual([(a["attempt"], a["reason"], a["outcome"]) for a in research["attempts"]],
                         [(1, "first_run", "failed"), (2, "retry_after_failure", "completed")])
        first = self.checkpoint(out["session_id"])["stages"][0]
        self.assertEqual(len(first["attempts"]), 1)                          # never ran again

    def test_attempt_limit(self):
        folder = self.config_root(session={"limits": {"max_attempts_per_stage": 1}})
        dataset = self.reclaim()
        out = self.runner(config_root=folder, handlers=failing_strategy()).start(dataset["dataset_id"])
        self.assertCode("session_attempt_limit", self.runner(config_root=folder).resume, out["session_id"])

    def test_dataset_check_failure_is_explicit(self):
        folder = self.config_root(session={"research": {"as_of": "2030-01-01T00:00:00Z"}})
        out = self.runner(config_root=folder).start(self.reclaim()["dataset_id"])
        self.assertEqual(out["failure"], {"stage": "dataset_validation", "code": "research_as_of_outside_data"})
        self.assertEqual(out["status"], "failed")

    def test_completed_stages_are_never_executed_again(self):
        dataset = self.reclaim()
        out = self.runner(handlers=failing_strategy()).start(dataset["dataset_id"])
        with patch.object(runner_module.SessionRunner, "_dataset_check", side_effect=AssertionError("ran again")):
            self.assertEqual(self.runner().resume(out["session_id"])["status"], "completed")


class CrashTests(Base):
    def test_crash_after_publication_adopts_the_artifact(self):
        dataset = self.reclaim()
        with patch.object(SessionStore, "save_checkpoint", crash_when("simulation", "completed")):
            with self.assertRaises(Interrupted):
                self.runner().start(dataset["dataset_id"])
        session_id = SessionStore(self.root).ids()[0]
        published = self.artifact_path(session_id, "simulation").read_bytes()
        self.assertEqual(self.checkpoint(session_id)["stages"][2]["status"], "running")
        self.assertEqual(list_sessions(self.root)[0][0]["status"], "interrupted")
        with patch.object(runner_module, "run_simulation", side_effect=AssertionError("simulation ran again")):
            out = self.runner().resume(session_id)
        self.assertEqual(out["status"], "completed")
        stage = self.checkpoint(session_id)["stages"][2]
        self.assertEqual([(a["attempt"], a["outcome"]) for a in stage["attempts"]], [(1, "recovered")])
        self.assertEqual(self.artifact_path(session_id, "simulation").read_bytes(), published)   # not overwritten
        self.assertEqual(stage["artifact"]["store"], "already_present")

    def test_crash_before_publication_reruns_explicitly(self):
        dataset = self.reclaim()
        with patch.object(runner_module, "run_simulation", side_effect=Interrupted()):
            with self.assertRaises(Interrupted):
                self.runner().start(dataset["dataset_id"])
        session_id = SessionStore(self.root).ids()[0]
        self.assertFalse(self.artifact_path(session_id, "simulation").exists())
        out = self.runner().resume(session_id)
        self.assertEqual(out["status"], "completed")
        stage = self.checkpoint(session_id)["stages"][2]
        self.assertEqual([(a["attempt"], a["reason"], a["outcome"], a["error_code"]) for a in stage["attempts"]],
                         [(1, "first_run", "interrupted", "interrupted_before_completion"),
                          (2, "retry_after_interruption", "completed", None)])

    def test_crashed_artifact_that_fails_validation_stops_resume(self):
        dataset = self.reclaim()
        with patch.object(SessionStore, "save_checkpoint", crash_when("simulation", "completed")):
            with self.assertRaises(Interrupted):
                self.runner().start(dataset["dataset_id"])
        session_id = SessionStore(self.root).ids()[0]
        path = self.artifact_path(session_id, "simulation")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["summary"]["ending_cash"] = "1"
        path.write_text(json.dumps(document), encoding="utf-8")
        before = tree(self.root)
        self.assertCode("session_artifact_tampered", self.runner().resume, session_id)
        self.assertEqual(tree(self.root), before)


class DuplicateAndConcurrencyTests(Base):
    def test_duplicate_start_is_refused(self):
        dataset = self.reclaim()
        self.runner().start(dataset["dataset_id"])
        before = tree(self.root)
        self.assertCode("session_exists", self.runner().start, dataset["dataset_id"])
        self.assertEqual(tree(self.root), before)

    def test_concurrent_resume_is_refused(self):
        dataset = self.reclaim()
        out = self.runner(handlers=failing_strategy()).start(dataset["dataset_id"])
        lock = SessionStore(self.root).lock(out["session_id"])        # another process is running it
        try:
            self.assertCode("session_busy", self.runner().resume, out["session_id"])
            row = list_sessions(self.root)[0][0]
            self.assertEqual((row["status"], row["live"]), ("running", True))
            self.assertEqual(session_document(out["session_id"], self.root)["status"], "running")
        finally:
            lock.release()
        self.assertEqual(self.runner().resume(out["session_id"])["status"], "completed")

    def test_concurrent_start_from_threads_runs_once(self):
        dataset = self.reclaim()
        results, calls = [], []
        real = runner_module.run_simulation

        def slow(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        def go():
            try:
                results.append(self.runner().start(dataset["dataset_id"])["status"])
            except TradingError as error:
                results.append(error.code)
        with patch.object(runner_module, "run_simulation", slow):
            threads = [threading.Thread(target=go) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results.count("completed"), 1, results)
        self.assertTrue(set(results) <= {"completed", "session_exists", "session_busy"})
        self.assertEqual(len(calls), 1)


class TamperTests(Base):
    def failed_session(self):
        return self.runner(handlers=failing_strategy()).start(self.reclaim()["dataset_id"])["session_id"]

    def test_tampered_completed_artifact(self):
        session_id = self.failed_session()
        path = self.artifact_path(session_id, "dataset_validation")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["bar_count"] += 1
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertCode("session_artifact_tampered", self.runner().resume, session_id)
        view = describe(session_id, self.root)
        self.assertEqual(view["integrity_problem"], {"stage": "dataset_validation", "code": "session_artifact_tampered"})
        document = session_document(session_id, self.root)
        self.assertEqual(document["integrity"]["status"], "failed")
        self.assertIsNone(document["results"])
        self.assertTrue(all(not link["available"] for link in document["links"].values()))

    def test_tampered_checkpoint_and_record(self):
        session_id = self.failed_session()
        folder = self.root / "runtime/trading/sessions" / session_id
        checkpoint = json.loads((folder / "checkpoint.json").read_text(encoding="utf-8"))
        checkpoint["stages"][1]["status"] = "completed"
        (folder / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
        self.assertCode("session_checkpoint_corrupt", self.runner().resume, session_id)
        record = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        record["dataset"]["symbol"] = "OTHER"
        (folder / "session.json").write_text(json.dumps(record), encoding="utf-8")
        self.assertCode("session_corrupt", self.runner().resume, session_id)
        self.assertEqual(list_sessions(self.root)[0][0], {"session_id": session_id, "readable": False, "code": "session_corrupt"})
        status, _, body = api.respond("GET", f"/api/session?id={session_id}", HOST, port=PORT, root=self.root)
        self.assertEqual((status, json.loads(body)["error"]["code"]), (409, "session_corrupt"))

    def test_artifact_for_a_stage_that_never_ran(self):
        session_id = self.failed_session()
        self.artifact_path(session_id, "performance_analytics").write_text("{}", encoding="utf-8")
        self.assertCode("session_artifact_unexpected", self.runner().resume, session_id)

    def test_tampered_dataset(self):
        session_id = self.failed_session()
        path = self.root / f"runtime/trading/market/datasets/{self.reclaim()['dataset_id']}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["bars"][0][1] = "1"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertCode("session_dataset_changed", self.runner().resume, session_id)


class ConfigurationTests(Base):
    def test_configuration_change_is_refused_on_resume(self):
        folder = self.config_root()
        dataset = self.reclaim()
        out = self.runner(config_root=folder, handlers=failing_strategy()).start(dataset["dataset_id"])
        policy = folder / "config/simulation.paper.json"
        document = json.loads(policy.read_text(encoding="utf-8"))
        document["costs"]["fee_per_order"] = "2.00"
        policy.write_text(json.dumps(document), encoding="utf-8")
        before = tree(self.root)
        with self.assertRaises(TradingError) as caught:
            self.runner(config_root=folder).resume(out["session_id"])
        self.assertEqual(caught.exception.code, "session_config_changed")
        self.assertIn("simulation_policy", caught.exception.message)
        self.assertEqual(tree(self.root), before)
        # a new session with the changed configuration is a different session
        other = self.runner(config_root=folder).start(dataset["dataset_id"])
        self.assertNotEqual(other["session_id"], out["session_id"])

    def test_component_version_change_is_refused(self):
        out = self.runner(handlers=failing_strategy()).start(self.reclaim()["dataset_id"])
        changed = dict(runner_module.component_versions(), simulation_run="9.9")
        with patch.object(runner_module, "component_versions", return_value=changed):
            self.assertCode("component_version_changed", self.runner().resume, out["session_id"])

    def test_invalid_session_config(self):
        folder = self.config_root(session={"simulation": {"policy": "config/missing.json"}})
        self.assertCode("invalid_simulation_policy", self.runner(config_root=folder).start, self.reclaim()["dataset_id"])
        bad = self.config_root(session={"research": {"as_of": "yesterday"}})
        self.assertCode("invalid_session_config", self.runner(config_root=bad).start, self.reclaim()["dataset_id"])


# ---------------------------------------------------------------- separation and future data
NORMALIZE = re.compile(r"(mds|rsig|rar)-[0-9a-f]{24}|[0-9a-f]{64}")


def normalized(value):
    return json.loads(NORMALIZE.sub("X", json.dumps(value, sort_keys=True)))


class FutureDataTests(Base):
    def test_research_and_simulation_never_see_later_bars(self):
        bars = FIXTURE["bars"]
        cut = 8                                                  # bars 1..8 identical; bars 9-10 differ wildly
        changed = deepcopy(bars)
        for bar in changed[cut:]:
            bar.update(open="99.00", high="99.50", low="98.50", close="99.20", volume="9000")
        a, b = self.csv_dataset(bars, "a"), self.csv_dataset(changed, "b")
        as_of = "2026-01-20T15:10:00Z"                           # bar 8 (10:05 New York) closes at 15:10 UTC
        folder = self.config_root(session={"research": {"as_of": as_of}})
        docs = {}
        for name, dataset in (("a", a), ("b", b)):
            out = self.runner(config_root=folder).start(dataset["dataset_id"])
            self.assertEqual(out["status"], "completed", out)
            docs[name] = describe(out["session_id"], self.root)["documents"]
        ra, rb = docs["a"]["research_analysis"], docs["b"]["research_analysis"]
        self.assertEqual((ra["sim_time_utc"], rb["sim_time_utc"]), (as_of, as_of))
        self.assertEqual(normalized(ra["stages"]), normalized(rb["stages"]))     # later bars changed nothing
        self.assertEqual(docs["a"]["hq_summary"]["time_domains"]["historical_research"]["relation_to_simulation"],
                         "during_simulation_window")

        def early(run):
            return [(o["purpose"], o["source"]["decision_bar_sequence"], o["history"][0]["status"],
                     o["history"][0]["reason_codes"]) for o in run["orders"] if o["source"]["decision_bar_sequence"] <= cut]
        sa, sb = docs["a"]["simulation"], docs["b"]["simulation"]
        self.assertEqual(early(sa), early(sb))
        self.assertTrue(early(sa))                                          # the check is not vacuous

    def test_research_output_is_not_passed_to_the_simulator(self):
        dataset = self.reclaim()
        seen = []
        real = runner_module.run_simulation

        def spy(*args, **kwargs):
            seen.append(sorted(kwargs))
            return real(*args, **kwargs)
        with patch.object(runner_module, "run_simulation", spy):
            self.runner().start(dataset["dataset_id"])
        self.assertEqual(seen, [["created_at", "end", "events", "indicator_config", "kill_switch", "market_config",
                                 "signal_config", "start", "step_seconds"]])


# ---------------------------------------------------------------- events
class EventTests(Base):
    def test_recorded_stage_timelines_share_the_session_correlation(self):
        out = self.runner(record_events=True).start(self.reclaim()["dataset_id"])
        record = SessionStore(self.root).load_record(out["session_id"])
        stages = self.checkpoint(out["session_id"])["stages"]
        from vicekrack.events.cli import load_timeline
        for index, kind in ((1, "research_agent_workflow"), (2, "simulation")):
            timeline = stages[index]["attempts"][0]["events"]["timeline_id"]
            view = load_timeline(timeline, self.root)
            self.assertEqual((view["kind"], view["correlation_id"], view["completeness"]),
                             (kind, record["correlation_id"], "complete"))
            self.assertEqual(view["run_id"], stages[index]["artifact"]["record_id"])
        for index in (0, 3, 4):
            self.assertEqual(stages[index]["attempts"][0]["events"], {"recorded": False, "timeline_id": None, "outcome": None})
        links = session_document(out["session_id"], self.root)["links"]
        self.assertEqual(links["research_rooms"]["origin"], "recorded")
        self.assertEqual(links["analytics_desk"]["timeline_id"], links["simulator_station"]["timeline_id"])

    def test_event_persistence_failure_keeps_committed_results(self):
        dataset = self.reclaim()
        with patch.object(TimelineWriter, "append", failing_append(2)):
            out = self.runner(record_events=True).start(dataset["dataset_id"])
        self.assertEqual(out["failure"], {"stage": "research_analysis", "code": "event_persistence_failed"})
        stages = self.checkpoint(out["session_id"])["stages"]
        self.assertEqual(stages[0]["status"], "completed")
        self.assertEqual(stages[1]["attempts"][0]["events"]["outcome"], "persistence_failed")
        self.assertFalse(self.artifact_path(out["session_id"], "research_analysis").exists())
        self.assertEqual(AgentRunStore(self.root).list(), [])
        resumed = self.runner(record_events=True).resume(out["session_id"])
        self.assertEqual(resumed["status"], "completed")
        manifest = describe(out["session_id"], self.root)["manifest"]
        research = manifest["stages"][1]
        self.assertEqual([t["outcome"] for t in research["timelines"]], ["persistence_failed", "completed"])
        self.assertEqual(manifest["links"]["research_rooms"]["timeline_id"], research["timelines"][1]["timeline_id"])

    def test_without_recording_links_use_reconstructed_timelines(self):
        out = self.runner().start(self.reclaim()["dataset_id"])
        links = session_document(out["session_id"], self.root)["links"]
        self.assertEqual((links["research_rooms"]["origin"], links["research_rooms"]["timeline_id"][:4]),
                         ("reconstructed", "rar-"))
        self.assertEqual(links["simulator_station"]["timeline_id"][:5], "srun-")
        self.assertFalse(list((self.root / "runtime").glob("events")))


# ---------------------------------------------------------------- isolation and the read-only HQ
class IsolationTests(Base):
    def test_paper_accounts_and_sources_untouched(self):
        PaperAccount("session-guard", root=self.root, clock=lambda: NOW).initialize()
        state = self.root / "runtime/trading/accounts/acct-session-guard/state.json"
        before = state.read_bytes()
        dataset = self.reclaim()
        dataset_file = self.root / f"runtime/trading/market/datasets/{dataset['dataset_id']}.json"
        dataset_bytes = dataset_file.read_bytes()
        tracked = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / "config").rglob("*")) if p.is_file()}
        self.runner(record_events=True).start(dataset["dataset_id"])
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(dataset_file.read_bytes(), dataset_bytes)
        self.assertEqual(tracked, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked})
        for path in (ROOT / "vicekrack/trading/session").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("from ..state", "from ..risk", "from ..orders", "from ..journal", "PaperAccount",
                              "build_intent", "authorize(", "openai", "anthropic", "socket", "urllib", "requests",
                              "http.client", "subprocess", "threading", "while True", "os.environ", "schedule"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


class HQSessionTests(Base):
    def get(self, target, method="GET"):
        status, headers, body = api.respond(method, target, dict(HOST), port=PORT, root=self.root)
        return status, json.loads(body)

    def test_list_summary_and_demo(self):
        done = self.runner(record_events=True).start(self.reclaim()["dataset_id"])
        status, listing = self.get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([i["session_id"] for i in listing["items"]], ["demo", done["session_id"]])
        status, document = self.get(f"/api/session?id={done['session_id']}")
        self.assertEqual((status, document["summary_source"], document["integrity"]["status"]), (200, "manifest", "verified"))
        check_session(document)
        self.assertEqual([s["stage"] for s in document["stages"]], list(STAGES))
        self.assertTrue(all(link["available"] for link in document["links"].values()))
        self.assertIn(f"python -m vicekrack trading-session-inspect {done['session_id']}", document["commands"])
        status, demo = self.get("/api/session?id=demo")
        self.assertEqual((status, demo["origin"], demo["links"]["simulator_station"]["timeline_id"]), (200, "demo", "demo"))
        self.assertIn("DEMO DATA", demo["notice"])

    def test_incomplete_session_is_summarized_from_its_checkpoint(self):
        out = self.runner(handlers=failing_strategy()).start(self.reclaim()["dataset_id"])
        document = session_document(out["session_id"], self.root)
        self.assertEqual((document["summary_source"], document["status"], document["results"]), ("checkpoint", "failed", None))
        self.assertIsNone(document["time_domains"]["historical_research"])
        self.assertEqual(document["links"]["research_rooms"]["reason"], "stage_not_completed")
        self.assertIn(f"python -m vicekrack trading-session-resume {out['session_id']}", document["commands"])

    def test_hq_is_read_only_and_never_runs_sessions(self):
        out = self.runner(handlers=failing_strategy()).start(self.reclaim()["dataset_id"])
        before = tree(self.root)
        with patch.object(SessionRunner, "resume", side_effect=AssertionError("HQ resumed")), \
                patch.object(SessionRunner, "start", side_effect=AssertionError("HQ started")):
            for target in ("/api/sessions", f"/api/session?id={out['session_id']}", "/api/session?id=demo"):
                self.assertEqual(self.get(target)[0], 200)
                self.assertEqual(self.get(target, method="POST")[0], 405)
        self.assertEqual(tree(self.root), before)
        self.assertFalse(SessionStore(self.root).live(out["session_id"]))
        for target, code in (("/api/session?id=../x", "invalid_session_id"), ("/api/session", "invalid_session_id"),
                             ("/api/session?id=demo&id=demo", "invalid_session_id"),
                             ("/api/sessions?x=1", "invalid_session_request"),
                             ("/api/session?id=tss-" + "a" * 24, "session_not_found")):
            self.assertEqual(self.get(target)[1]["error"]["code"], code)
        app = (ROOT / "vicekrack/hq/static/app.js").read_text(encoding="utf-8")
        self.assertNotIn("POST", app)
        self.assertNotRegex(app, r"trading-session-(start|resume)\"|/api/session/(start|resume)")


@unittest.skipUnless(os.environ.get("RUN_LOCAL_BROWSER_TESTS") == "1", "set RUN_LOCAL_BROWSER_TESTS=1 (needs Playwright + Chromium)")
class SessionBrowserTests(Base):
    def test_sessions_view_in_a_real_browser(self):
        from playwright.sync_api import sync_playwright
        done = self.runner(record_events=True).start(self.reclaim()["dataset_id"])
        server = HQServer(0, self.root)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width, height in ((1600, 900), (390, 844)):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    problems, posts = [], []
                    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
                    page.on("pageerror", lambda e: problems.append(str(e)))
                    page.on("request", lambda r: posts.append(r.url) if r.method != "GET" else None)
                    page.goto(url)
                    page.wait_for_selector(".bot[data-bot='creator']")
                    page.click("#btn-play")
                    page.click(".view-btn[data-view='sessions']")
                    page.wait_for_selector("#session-body .domains")
                    self.assertEqual(page.inner_text("#session-badge"), "DEMO DATA · SYNTHETIC · SIMULATED")
                    page.select_option("#session-select", done["session_id"])
                    page.wait_for_selector(f"#session-body:has-text('{done['session_id']}')")
                    self.assertEqual(page.inner_text("#session-badge"), "SAVED SESSION · SIMULATED")
                    text = page.inner_text("#session-body")
                    for phrase in ("Historical research", "Simulated execution", "Wall clock", "never received them",
                                   "this page cannot run them"):
                        self.assertIn(phrase, text)
                    buttons = [b.lower() for b in page.locator("#sessions-view button").all_inner_texts()]
                    self.assertFalse([b for b in buttons if "start" in b or "resume" in b], buttons)
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    page.click("button[data-link='analytics_desk']")
                    page.wait_for_selector("#results-view:not([hidden]) .summary-label")
                    self.assertIn("SAVED SIMULATION", page.inner_text("#results-badge"))
                    page.keyboard.press("s")
                    page.wait_for_selector("#sessions-view:not([hidden])")
                    page.wait_for_selector("button[data-link='research_rooms']:not([disabled])")
                    page.click("button[data-link='research_rooms']")
                    page.wait_for_selector("#timeline-name:has-text('research agent workflow')")
                    self.assertTrue(page.is_visible("#stage-wrap"))
                    self.assertEqual((problems, posts), ([], []))
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
