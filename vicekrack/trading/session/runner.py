"""Controlled end-to-end trading research session (Step 39). OFFLINE and SIMULATED only.

Fixed sequence (five stages, never reordered, never extended):

  1 dataset_validation     re-validate the stored dataset (Step 25 loader) and the session's
                           inputs against it
  2 research_analysis      the Step 28 research-agent workflow at an exact simulated as-of time
  3 simulation             the Step 29 simulator with its own validated policy
  4 performance_analytics  the Step 30 analytics report for that simulation
  5 hq_summary             the versioned session manifest the Living HQ reads

Separation of research and execution: nothing from stage 2 is passed to stage 3. The
simulator takes the dataset, its policy and the shared indicator/signal configuration
only, exactly as `sim-run` does, so its run (and run hash) is identical to a standalone
run with the same inputs. Research conclusions are labelled with their as-of time and
never authorize anything. Indicators and signals are computed by the existing closed-bar
replay code in both stages.

Control:
- No automatic retries. A failed stage stops the session (`failed`); later stages stay
  `pending`. `trading-session-resume` is the only way to try again, from the first
  incomplete stage, after re-validating every completed artifact, the dataset, every
  configuration snapshot and the component versions.
- One process per session (OS lock). Completed stages never run again. A stage's artifact
  is published once (exclusive link) BEFORE the checkpoint marks it completed. If a crash
  lands between the two, resume finds the artifact, validates it and adopts it
  (`recovered`) instead of running the stage again; an invalid artifact stops the resume.
- The session's research run, simulation run and analytics report are also saved in the
  existing stores (append-only). A record that is already there is kept as it is when its
  results hash matches (`already_present`); a different one is a conflict, never replaced.

Events: with `--record-events` the research and simulation stages record their normal
Step 31 timelines, with the session's correlation ID. A recording failure fails that stage
explicitly (`event_persistence_failed`); committed earlier stages are kept, and nothing is
saved for the failed stage. Stages 1, 4 and 5 have no event instrumentation.

This module never imports paper accounts, the paper risk engine, intents or the journal,
and never calls a provider or the network.
"""

import json
from pathlib import Path

from ..agents.controller import run_workflow, validate_run as validate_agent_run
from ..agents.handlers import default_handlers
from ..agents.store import AgentRunStore, load_agent_config
from ..analytics.report import build_report, validate_report
from ..analytics.store import AnalyticsStore, load_analytics_config
from ..contracts import ROOT, sha256, utc_now, validate_schema
from ..errors import TradingError
from ..indicators.store import load_indicator_config
from ..market.bars import from_utc_text
from ..market.store import MarketStore, load_market_config
from ..signals.engine import provenance
from ..signals.store import load_signal_config
from ..simulation.engine import validate_run as validate_sim_run
from ..simulation.engine import run_simulation
from ..simulation.store import SimulationStore, load_policy
from ..timeline import close_recorder, open_recorder
from .manifest import build_manifest, validate_manifest
from .store import STAGES, SessionStore, artifact_name, seal_checkpoint, validate_record

NOTICE = ("Controlled OFFLINE trading research session on stored historical data. Research conclusions are labelled "
          "with their simulated as-of time and never authorize or feed simulated orders; the simulator decides with its "
          "own policy. SIMULATED only: no broker, no live data, no paper-account or real orders. Not advice.")
TIME_DOMAIN = {"dataset_validation": "historical_data", "research_analysis": "historical_research",
               "simulation": "simulated_execution", "performance_analytics": "derived_from_simulation"}
RECORD_KINDS = {"dataset_validation": "trading_session_dataset_check", "research_analysis": "research_agent_run",
                "simulation": "simulation_run", "performance_analytics": "simulation_analytics_report",
                "hq_summary": "trading_session_manifest"}
SESSION_CODES = {"session_busy", "session_exists", "session_not_found", "session_corrupt", "session_checkpoint_corrupt",
                 "session_artifact_tampered", "session_artifact_unexpected", "session_config_changed",
                 "component_version_changed", "session_dataset_changed", "session_attempt_limit", "session_completed",
                 "session_checkpoint_failed", "session_write_failed", "invalid_session_id", "invalid_session_config"}


class Interrupted(BaseException):
    """Raised by tests to simulate a crash; never caught by the runner."""


# ---------------------------------------------------------------- inputs and versions
def _schema_version(name):
    schema = json.loads((ROOT / "schemas/trading" / f"{name}.schema.json").read_text(encoding="utf-8"))
    return schema["properties"]["version"]["const"]


def component_versions():
    """Contract versions as declared by the shipped schemas, plus the research handler identities."""
    return {"session_runner": "1.0", "session_manifest": _schema_version("trading-session-manifest"),
            "market_dataset": _schema_version("market-dataset"),
            "research_agent_run": _schema_version("research-agent-run"),
            "research_handlers": [getattr(h, "identity", f"{h.role}@1.0") for h in default_handlers()],
            "simulation_run": _schema_version("simulation-run"),
            "simulation_analytics_report": _schema_version("simulation-analytics-report")}


def load_session_config(path="config/trading-session.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root / "config") or target.suffix != ".json":
        raise TradingError("invalid_session_config", "The session configuration must be a JSON file under config/.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        validate_schema("trading_session_config", config)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        raise TradingError("invalid_session_config", "The session configuration is invalid.") from None
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_session_config", "Cannot read a valid session configuration.") from None
    simulation = config["simulation"]
    if simulation["start"] and simulation["end"] and simulation["start"] >= simulation["end"]:
        raise TradingError("invalid_session_config", "simulation.start must be before simulation.end.")
    return config, target.relative_to(root).as_posix()


def load_inputs(config_path="config/trading-session.json", config_root=ROOT):
    """Every configuration the five stages use, read by the existing loaders, with hashes."""
    session_config, session_path = load_session_config(config_path, config_root)
    policy_path = session_config["simulation"]["policy"]
    loaded = {
        "session_config": (session_path, session_config),
        "market_data": ("config/market-data.json", load_market_config(root=config_root)[0]),
        "indicators": ("config/indicators.json", load_indicator_config(root=config_root)[0]),
        "research_signals": ("config/research-signals.json", load_signal_config(root=config_root)[0]),
        "research_agents": ("config/research-agents.json", load_agent_config(root=config_root)[0]),
        "simulation_policy": (policy_path, load_policy(policy_path, root=config_root)[0]),
        "analytics": ("config/analytics.json", load_analytics_config(root=config_root)[0]),
    }
    return {name: {"path": path, "sha256": sha256(document), "document": document}
            for name, (path, document) in loaded.items()}


def session_identity(dataset, inputs, versions):
    data = provenance(dataset)
    session_id = "tss-" + sha256({"dataset": data, "inputs": {k: v["sha256"] for k, v in inputs.items()},
                                  "versions": versions})[:24]
    return session_id, "cor-" + sha256({"trading_session": session_id})[:24]


def research_as_of(record, dataset):
    rule = record["inputs"]["session_config"]["document"]["research"]["as_of"]
    return (dataset["last_available_utc"] if rule == "dataset_end" else rule), rule


def effective_agent_config(record):
    config = record["inputs"]["research_agents"]["document"]
    strategies = record["inputs"]["session_config"]["document"]["research"]["strategies"]
    return {**config, "strategies": list(strategies)} if strategies else config


def verify_artifact(record, name, document, prior):
    """Full validation of one stage artifact and its links to the session and earlier stages."""
    try:
        if name == "dataset_validation":
            validate_schema("trading_session_dataset_check", document)
            body = {k: document[k] for k in ("dataset", "bar_count", "first_start_utc", "last_available_utc",
                                             "gap_count", "checks")}
            ok = (sha256(body) == document["results_sha256"] and document["session_id"] == record["session_id"]
                  and document["dataset"] == record["dataset"])
        elif name == "research_analysis":
            validate_agent_run(document)
            dataset = prior["dataset_validation"]
            expected, _ = research_as_of(record, {"last_available_utc": dataset["last_available_utc"]})
            ok = (document["dataset"]["dataset_id"] == record["dataset"]["dataset_id"]
                  and document["dataset"]["bars_sha256"] == record["dataset"]["bars_sha256"]
                  and document["sim_time_utc"] == expected and document["status"] == "completed"
                  and document["hashes"]["workflow_config_sha256"] == sha256(effective_agent_config(record)))
        elif name == "simulation":
            validate_sim_run(document)
            ok = (document["dataset"] == record["dataset"]
                  and document["policy_sha256"] == record["inputs"]["simulation_policy"]["sha256"])
        elif name == "performance_analytics":
            validate_report(document)
            sim = prior["simulation"]
            ok = (document["source"]["run_id"] == sim["run_id"]
                  and document["source"]["run_results_sha256"] == sim["results_sha256"]
                  and document["source"]["analytics_config_sha256"] == record["inputs"]["analytics"]["sha256"])
        else:
            validate_manifest(document)
            ok = document["session_id"] == record["session_id"]
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        ok = False
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise TradingError("session_artifact_tampered", f"The {name} artifact failed validation; nothing was run.")
    return document


def _attempt(number, reason, now, recorded):
    return {"attempt": number, "reason": reason, "started_at": now, "ended_at": None, "outcome": "running",
            "error_code": None, "events": {"recorded": recorded, "timeline_id": None, "outcome": None}}


def first_checkpoint(session_id, now):
    return seal_checkpoint({"contract": "trading_session_checkpoint", "version": "1.0", "session_id": session_id,
                            "revision": 1, "status": "created", "updated_at": now,
                            "stages": [{"stage": s, "position": i, "status": "pending", "attempts": [], "artifact": None}
                                       for i, s in enumerate(STAGES, start=1)],
                            "previous_sha256": None})


# ---------------------------------------------------------------- the runner
class SessionRunner:
    def __init__(self, root=None, *, record_events=False, config_root=ROOT, clock=utc_now, handlers=None):
        self.root, self.record_events, self.config_root, self.clock = root, record_events, config_root, clock
        self.handlers = handlers                       # tests only: inject failing research handlers
        self.store = SessionStore(root)
        self.markets = MarketStore(root)

    # ------------------------------------------------------------------ entry points
    def start(self, dataset_id, config_path="config/trading-session.json"):
        inputs = load_inputs(config_path, self.config_root)
        dataset = self.markets.load(dataset_id)                       # missing or tampered datasets stop here
        versions = component_versions()
        session_id, correlation_id = session_identity(dataset, inputs, versions)
        body = {"contract": "trading_session", "version": "1.0", "session_id": session_id,
                "correlation_id": correlation_id, "created_at": self.clock(), "mode": "offline_simulation",
                "simulated": True, "paper_account_access": False, "broker": None, "notice": NOTICE,
                "dataset": provenance(dataset), "inputs": inputs, "versions": versions, "stages": list(STAGES)}
        record = validate_record({**body, "record_sha256": sha256(body)})
        self.store.create(record, first_checkpoint(session_id, self.clock()))
        lock = self.store.lock(session_id)
        try:
            return self._drive(record, self.store.load_checkpoint(session_id))
        finally:
            lock.release()

    def resume(self, session_id):
        record = self.store.load_record(session_id)
        lock = self.store.lock(session_id)
        try:
            checkpoint = self.store.load_checkpoint(session_id)
            if checkpoint["status"] == "completed":
                raise TradingError("session_completed", "This session is complete; there is nothing to resume.")
            self._check_unchanged(record)
            checkpoint = self._reconcile(record, checkpoint)
            return self._drive(record, checkpoint)
        finally:
            lock.release()

    # ------------------------------------------------------------------ resume checks
    def _check_unchanged(self, record):
        config_path = record["inputs"]["session_config"]["path"]
        try:
            current = load_inputs(config_path, self.config_root)
        except TradingError:
            raise TradingError("session_config_changed",
                               "A configuration this session used can no longer be read; nothing was run.") from None
        changed = sorted(n for n in record["inputs"] if current[n]["sha256"] != record["inputs"][n]["sha256"]
                         or current[n]["path"] != record["inputs"][n]["path"])
        if changed:
            raise TradingError("session_config_changed",
                               "Configuration changed since this session started (" + ", ".join(changed) +
                               "); start a new session instead. Nothing was run.")
        if component_versions() != record["versions"]:
            raise TradingError("component_version_changed",
                               "A component version changed since this session started; start a new session. Nothing was run.")
        self._dataset(record)

    def _dataset(self, record):
        try:
            dataset = self.markets.load(record["dataset"]["dataset_id"])
        except TradingError as error:
            if error.code == "dataset_not_found":
                raise
            raise TradingError("session_dataset_changed", "The session's dataset no longer validates.") from None
        if provenance(dataset) != record["dataset"]:
            raise TradingError("session_dataset_changed", "The session's dataset does not match its record.")
        return dataset

    def _reconcile(self, record, checkpoint):
        """Validate completed artifacts; adopt or mark interrupted a stage left `running` by a crash."""
        session_id = record["session_id"]
        documents = {}
        for index, stage in enumerate(checkpoint["stages"]):
            name = stage["stage"]
            found = self.store.read_artifact(session_id, name)
            if stage["status"] == "completed":
                if found is None or found[1] != stage["artifact"]["file_sha256"]:
                    raise TradingError("session_artifact_tampered",
                                       f"The {name} artifact is missing or changed since it was checkpointed; nothing was run.")
                documents[name] = self._verify(record, name, found[0], documents)
                self._check_info(stage["artifact"], documents[name], name)
                continue
            if stage["status"] == "running":
                attempt = stage["attempts"][-1]
                if found is not None:                                  # published, then the process died
                    document = self._verify(record, name, found[0], documents)
                    store_state = self._register(name, document)
                    attempt.update(outcome="recovered", ended_at=self.clock(), error_code=None)
                    stage.update(status="completed", artifact=self._info(name, document, found[1], store_state))
                    documents[name] = document
                    checkpoint = self._save(session_id, checkpoint, status="running")
                    continue
                attempt.update(outcome="interrupted", error_code="interrupted_before_completion")
                stage["status"] = "failed"
                checkpoint = self._save(session_id, checkpoint, status="failed")
            elif found is not None:
                raise TradingError("session_artifact_unexpected",
                                   f"An artifact exists for the {name} stage, which never ran; nothing was run.")
            for later in checkpoint["stages"][index + 1:]:
                if self.store.read_artifact(session_id, later["stage"]) is not None:
                    raise TradingError("session_artifact_unexpected",
                                       f"An artifact exists for the {later['stage']} stage, which never ran; nothing was run.")
            break
        self._documents = documents
        return checkpoint

    @staticmethod
    def _check_info(info, document, name):
        if (info["record_kind"] != RECORD_KINDS[name] or info["record_id"] != _record_id(name, document)
                or info["results_sha256"] != document["results_sha256"]):
            raise TradingError("session_artifact_tampered", f"The {name} artifact does not match its checkpoint.")

    def _verify(self, record, name, document, prior):
        return verify_artifact(record, name, document, prior)

    # ------------------------------------------------------------------ the fixed sequence
    def _drive(self, record, checkpoint):
        session_id = record["session_id"]
        documents = getattr(self, "_documents", {})
        limit = record["inputs"]["session_config"]["document"]["limits"]["max_attempts_per_stage"]
        for index, name in enumerate(STAGES):                       # at most five stages, in this order, once
            stage = checkpoint["stages"][index]
            if stage["status"] == "completed":
                if name not in documents:
                    found = self.store.read_artifact(session_id, name)
                    documents[name] = self._verify(record, name, found[0], documents)
                continue
            attempts = stage["attempts"]
            if len(attempts) >= limit:
                raise TradingError("session_attempt_limit",
                                   f"The {name} stage reached max_attempts_per_stage; start a new session.")
            reason = ("first_run" if not attempts else
                      "retry_after_interruption" if attempts[-1]["outcome"] == "interrupted" else "retry_after_failure")
            recorded = self.record_events and name in ("research_analysis", "simulation")
            attempt = _attempt(len(attempts) + 1, reason, self.clock(), recorded)
            attempts.append(attempt)
            stage["status"] = "running"
            checkpoint = self._save(session_id, checkpoint, status="running")   # intent, before any work
            recorder = None
            try:
                if recorded:
                    recorder = open_recorder("research_agent_workflow" if name == "research_analysis" else "simulation",
                                             self.root, correlation_id=record["correlation_id"])
                    attempt["events"]["timeline_id"] = recorder.timeline_id
                document = self._run_stage(name, record, documents, checkpoint, recorder)
                if recorder is not None:
                    close_recorder(recorder, "completed")                # a failed close saves nothing
                    attempt["events"]["outcome"] = recorder.summary()["outcome"]
                relative, digest = self.store.publish_artifact(session_id, name, document)
                store_state = self._register(name, document)
            except TradingError as error:
                if recorder is not None:
                    recorder.abort(error.code)
                    attempt["events"]["outcome"] = recorder.summary()["outcome"]
                attempt.update(outcome="failed", ended_at=self.clock(), error_code=error.code)
                stage["status"] = "failed"
                checkpoint = self._save(session_id, checkpoint, status="failed")
                return self.result(record, checkpoint, failure={"stage": name, "code": error.code})
            attempt.update(outcome="completed", ended_at=self.clock())
            stage.update(status="completed", artifact=self._info(name, document, digest, store_state))
            documents[name] = document
            checkpoint = self._save(session_id, checkpoint, status="completed" if name == STAGES[-1] else "running")
            assert relative == artifact_name(name)
        return self.result(record, checkpoint)

    def _run_stage(self, name, record, documents, checkpoint, recorder):
        inputs = {k: v["document"] for k, v in record["inputs"].items()}
        session = inputs["session_config"]
        if name == "dataset_validation":
            return self._dataset_check(record, inputs)
        dataset = self._dataset(record)
        if name == "research_analysis":
            as_of, _ = research_as_of(record, dataset)
            run = run_workflow(dataset, workflow_config=inputs["research_agents"], market_config=inputs["market_data"],
                               indicator_config=inputs["indicators"], signal_config=inputs["research_signals"],
                               created_at=self.clock(), sim_time=as_of, strategies=session["research"]["strategies"],
                               handlers=self.handlers, events=recorder)
            if run["status"] != "completed":
                if recorder is not None:
                    close_recorder(recorder, "failed")
                raise TradingError("research_workflow_failed",
                                   "The research workflow failed (" + run["failure"]["code"] + "); nothing was saved.")
            return run
        if name == "simulation":
            policy = inputs["simulation_policy"]
            window = session["simulation"]
            # Only the dataset, the policy and the shared indicator/signal configuration: never research output.
            return run_simulation(dataset, policy, market_config=inputs["market_data"],
                                  indicator_config=inputs["indicators"], signal_config=inputs["research_signals"],
                                  kill_switch=SimulationStore(self.root).kill_switch(policy), created_at=self.clock(),
                                  start=window["start"], end=window["end"], step_seconds=window["step_seconds"],
                                  events=recorder)
        if name == "performance_analytics":
            return build_report(documents["simulation"], dataset, market_config=inputs["market_data"],
                                analytics_config=inputs["analytics"],
                                analytics_config_sha256=record["inputs"]["analytics"]["sha256"], created_at=self.clock())
        return build_manifest(record, checkpoint, documents, created_at=self.clock())

    def _dataset_check(self, record, inputs):
        dataset = self._dataset(record)
        checks = ["dataset_contract_and_hashes", "dataset_matches_session_record"]
        if dataset["bar_count"] > inputs["simulation_policy"]["limits"]["max_bars"]:
            raise TradingError("sim_too_many_bars", "The dataset has more bars than the simulation policy allows.")
        checks.append("bars_within_simulation_limit")
        as_of, rule = research_as_of(record, dataset)
        if rule != "dataset_end":
            if not (from_utc_text(dataset["first_start_utc"]) < from_utc_text(as_of) <= from_utc_text(dataset["last_available_utc"])):
                raise TradingError("research_as_of_outside_data",
                                   "research.as_of must be after the first bar starts and no later than the last bar closes.")
        checks.append("research_as_of_within_data")
        window = inputs["session_config"]["simulation"]
        for key in ("start", "end"):
            if window[key] and not (dataset["first_start_utc"] <= window[key] <= dataset["last_available_utc"]):
                raise TradingError("simulation_window_outside_data", "The simulation window must lie within the dataset.")
        checks.append("simulation_window_within_data")
        body = {"dataset": provenance(dataset), "bar_count": dataset["bar_count"],
                "first_start_utc": dataset["first_start_utc"], "last_available_utc": dataset["last_available_utc"],
                "gap_count": dataset["gaps"]["gap_count"], "checks": [{"check": c, "status": "passed"} for c in checks]}
        document = {"contract": "trading_session_dataset_check", "version": "1.0",
                    "check_id": "tsdc-" + sha256({"session": record["session_id"], **body})[:24],
                    "session_id": record["session_id"], **body, "verified_as_authentic": False,
                    "results_sha256": sha256(body), "checked_at": self.clock(),
                    "notice": "Structural checks of stored historical data only; the data is NOT verified as authentic, "
                              "complete, current or licensed."}
        validate_schema("trading_session_dataset_check", document)
        return document

    # ------------------------------------------------------------------ shared stores (append-only)
    def _register(self, name, document):
        if name == "research_analysis":
            store, exists = AgentRunStore(self.root), "agent_run_exists"
            key = "run_id"
        elif name == "simulation":
            store, exists, key = SimulationStore(self.root), "sim_run_exists", "run_id"
        elif name == "performance_analytics":
            store, exists, key = AnalyticsStore(self.root), "report_exists", "report_id"
        else:
            return "session_only"
        try:
            store.save(document)
            return "published"
        except TradingError as error:
            if error.code != exists:
                raise
        existing = store.load(document[key])                           # re-validated by the store
        if existing["results_sha256"] != document["results_sha256"]:
            raise TradingError("session_store_conflict",
                               "A different saved record already uses this ID; it was not replaced.")
        return "already_present"

    @staticmethod
    def _info(name, document, digest, store_state):
        return {"file": artifact_name(name), "file_sha256": digest, "record_kind": RECORD_KINDS[name],
                "record_id": _record_id(name, document), "results_sha256": document["results_sha256"],
                "store": store_state}

    def _save(self, session_id, checkpoint, *, status):
        body = {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
        updated = seal_checkpoint({**body, "revision": checkpoint["revision"] + 1, "status": status,
                                   "updated_at": self.clock(), "previous_sha256": checkpoint["checkpoint_sha256"]})
        return self.store.save_checkpoint(session_id, updated)

    # ------------------------------------------------------------------ output
    def result(self, record, checkpoint, failure=None):
        session_id = record["session_id"]
        output = {"notice": NOTICE, "session_id": session_id, "status": checkpoint["status"], "simulated": True,
                  "paper_account_access": False, "dataset_id": record["dataset"]["dataset_id"],
                  "stages": stage_rows(checkpoint), "failure": failure,
                  "next": next_commands(session_id, checkpoint["status"])}
        return output


ID_FIELD = {"dataset_validation": "check_id", "research_analysis": "run_id", "simulation": "run_id",
            "performance_analytics": "report_id", "hq_summary": "manifest_id"}


def _record_id(name, document):
    return document[ID_FIELD[name]]


def stage_rows(checkpoint):
    return [{"stage": s["stage"], "position": s["position"], "status": s["status"], "attempts": len(s["attempts"]),
             "last_attempt": s["attempts"][-1] if s["attempts"] else None,
             "artifact": None if s["artifact"] is None else {k: s["artifact"][k] for k in ("record_kind", "record_id", "store")}}
            for s in checkpoint["stages"]]


def next_commands(session_id, status):
    inspect = f"python -m vicekrack trading-session-inspect {session_id}"
    if status == "completed":
        return [inspect, "python -m vicekrack hq-serve   (then open the Sessions view)"]
    return [inspect, f"python -m vicekrack trading-session-resume {session_id}"]
