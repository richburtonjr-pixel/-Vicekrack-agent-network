"""Step 28: bounded deterministic research-agent workflow. Synthetic data only; no network, no credits."""

import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack.trading.agents import handlers as role
from vicekrack.trading.agents.analysis import AnalysisLayer, NoAnalysisLayer
from vicekrack.trading.agents.cli import main as agent_main
from vicekrack.trading.agents.controller import STAGES, run_workflow, validate_run
from vicekrack.trading.agents.handlers import Handler, default_handlers
from vicekrack.trading.agents.store import AgentRunStore, load_agent_config
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.indicators.store import load_indicator_config
from vicekrack.trading.market.store import MarketStore, load_market_config, make_adapter
from vicekrack.trading.signals.store import load_signal_config
from vicekrack.trading.state import PaperAccount

NOW = "2026-10-05T12:00:00Z"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.market_config, _ = load_market_config()
        self.indicator_config, _ = load_indicator_config()
        self.signal_config, _ = load_signal_config()
        self.config, _ = load_agent_config()
        self.store = MarketStore(self.root, clock=lambda: NOW)

    def fixture(self, name):
        return self.store.import_dataset(make_adapter("synthetic", config=self.market_config, fixture=name),
                                         config=self.market_config, config_sha256="0" * 64)

    def workflow(self, dataset, sim_time=None, config=None, **kwargs):
        return run_workflow(dataset, workflow_config=config or self.config, market_config=self.market_config,
                            indicator_config=self.indicator_config, signal_config=self.signal_config, created_at=NOW,
                            sim_time=sim_time, **kwargs)

    @staticmethod
    def stage(run, name):
        return next(s for s in run["stages"] if s["role"] == name)

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception


class HandoffTests(Base):
    def test_successful_four_stage_handoff(self):
        run = self.workflow(self.fixture("synth1-5m-reclaim"))
        self.assertEqual([(s["position"], s["role"], s["status"]) for s in run["stages"]],
                         [(1, "market_scout", "completed"), (2, "trend_agent", "completed"),
                          (3, "strategy_agent", "completed"), (4, "risk_review", "completed")])
        self.assertEqual([s["conclusion"] for s in run["stages"]],
                         ["data_available", "trend_assessed", "active_research_signal", "sufficient_for_future_paper_evaluation"])
        self.assertEqual((run["status"], run["failure"], run["final"]["verdict"]),
                         ("completed", None, "sufficient_for_future_paper_evaluation"))
        self.assertEqual((run["research_only"], run["authorization_possible"], run["account_access"]), (True, False, False))
        self.assertEqual((run["final"]["research_only"], run["final"]["authorization_possible"]), (True, False))
        self.assertEqual(len(run["final"]["explanation"]), 4)
        for stage in run["stages"]:
            self.assertTrue(stage["input_sha256"] and stage["output_sha256"] and stage["limitations"])
            self.assertEqual(stage["sim_time_utc"], run["sim_time_utc"])
        self.assertEqual(self.stage(run, "risk_review")["findings"]["authorization_performed"], False)
        self.assertEqual(set(run["hashes"]), {"workflow_config_sha256", "indicator_settings_sha256",
                                              "signal_run_results_sha256", "strategies", "evidence_sha256"})

    def test_handlers_receive_prior_outputs_read_only(self):
        seen = {}

        def make(function, name):
            def analyze(evidence, prior):
                seen[name] = [p["role"] for p in prior]
                result = function(evidence, prior)
                for item in prior:                                   # attempts to rewrite history must not leak
                    item["conclusion"] = "tampered"
                evidence.clear()
                return result
            return analyze
        handlers = [Handler(h.role, make(h._function, h.role)) for h in default_handlers()]
        run = self.workflow(self.fixture("synth1-5m-reclaim"), handlers=handlers)
        self.assertEqual(seen, {"market_scout": [], "trend_agent": ["market_scout"],
                                "strategy_agent": ["market_scout", "trend_agent"],
                                "risk_review": ["market_scout", "trend_agent", "strategy_agent"]})
        self.assertEqual([s["conclusion"] for s in run["stages"]],
                         ["data_available", "trend_assessed", "active_research_signal", "sufficient_for_future_paper_evaluation"])

    def test_invalid_transitions_and_stage_limit(self):
        dataset = self.fixture("synth1-5m-reclaim")
        handlers = default_handlers()
        self.assertCode("invalid_transition", self.workflow, dataset, handlers=[handlers[1], handlers[0], *handlers[2:]])
        self.assertCode("invalid_transition", self.workflow, dataset, handlers=handlers[:3])
        self.assertCode("invalid_transition", self.workflow, dataset, handlers=[*handlers[:3], Handler("trend_agent", role.trend_agent)])
        self.assertCode("too_many_stages", self.workflow, dataset, handlers=[*handlers, Handler("extra", role.risk_review)])
        bad = deepcopy(self.config)
        bad["limits"]["max_stages"] = 5
        self.assertRaises(TradingError, self.workflow, dataset, config=bad)

    def test_agents_cannot_choose_successors_or_launch_tasks(self):
        def sneaky(evidence, prior):
            output = role.market_scout(evidence, prior)
            output["next_stage"] = "risk_review"
            output["launch_task"] = "trade"
            return output
        handlers = default_handlers()
        handlers[0] = Handler("market_scout", sneaky)
        run = self.workflow(self.fixture("synth1-5m-reclaim"), handlers=handlers)
        self.assertEqual(run["failure"], {"role": "market_scout", "code": "invalid_handoff"})
        self.assertEqual([s["status"] for s in run["stages"]], ["failed", "not_run", "not_run", "not_run"])
        self.assertNotIn("next_stage", json.dumps(run))


class FailureTests(Base):
    def run_with(self, position, function, **kwargs):
        handlers = default_handlers()
        handlers[position] = Handler(STAGES[position], function)
        if not hasattr(self, "dataset"):
            self.dataset = self.fixture("synth1-5m-reclaim")
        return self.workflow(self.dataset, handlers=handlers, **kwargs)

    def test_stage_error_stops_without_retry(self):
        calls = []

        def broken(evidence, prior):
            calls.append(1)
            raise RuntimeError("/private/path API_KEY=abc")
        run = self.run_with(1, broken)
        self.assertEqual(calls, [1])                                             # no automatic retry
        self.assertEqual((run["status"], run["failure"], run["final"]["verdict"]),
                         ("failed", {"role": "trend_agent", "code": "stage_error"}, "workflow_failed"))
        self.assertEqual([s["status"] for s in run["stages"]], ["completed", "failed", "not_run", "not_run"])
        self.assertNotIn("/private", json.dumps(run))
        self.assertNotIn("API_KEY", json.dumps(run))

    def test_timeout_and_oversized_and_credential_outputs(self):
        ticks = iter([0.0, 100.0])
        timed = self.run_with(2, role.strategy_agent, monotonic=lambda: next(ticks, 200.0))
        self.assertEqual(timed["failure"]["code"], "stage_timeout")
        self.assertEqual(timed["failure"]["role"], "market_scout")              # first stage measured 0 -> 100
        config = deepcopy(self.config)
        config["limits"]["max_handoff_bytes"] = 1000
        large = self.workflow(self.dataset, config=config)
        self.assertEqual(large["failure"]["code"], "handoff_too_large")

        def leaky(evidence, prior):
            output = role.risk_review(evidence, prior)
            output["findings"]["api_key"] = "x"
            return output
        self.assertEqual(self.run_with(3, leaky)["failure"], {"role": "risk_review", "code": "invalid_handoff"})

    def test_analysis_layer_is_interface_only(self):
        self.assertIsNone(NoAnalysisLayer().commentary("trend_agent", {}, {}))

        class Mock(AnalysisLayer):
            name = "mock"

            def commentary(self, *args):
                return "buy now"
        dataset = self.fixture("synth1-5m-reclaim")
        self.assertCode("analysis_layer_unavailable", self.workflow, dataset, analysis_layer=Mock())
        self.assertEqual(self.workflow(dataset, analysis_layer=NoAnalysisLayer())["analysis_layer"],
                         {"name": "none", "advisory_only": True})

    def test_daily_datasets_need_compatible_strategies(self):
        daily = self.fixture("synth1-1d-dst")
        self.assertCode("invalid_strategy_config", self.workflow, daily)              # VWAP reclaim needs intraday bars
        run = self.workflow(daily, strategies=["breakout-3", "ema-cross-3-5"])
        self.assertEqual(run["status"], "completed")
        self.assertIn("vwap_not_applicable", self.stage(run, "trend_agent")["reason_codes"])
        self.assertEqual([s["name"] for s in run["hashes"]["strategies"]], ["breakout-3", "ema-cross-3-5"])
        self.assertCode("unknown_strategy", self.workflow, daily, strategies=["nope"])


class ConclusionTests(Base):
    def test_missing_indicators_and_insufficient_data(self):
        run = self.workflow(self.fixture("synth1-5m"), "2026-01-15T14:45:00Z")
        self.assertEqual(self.stage(run, "market_scout")["conclusion"], "insufficient_data")
        trend = self.stage(run, "trend_agent")
        self.assertEqual(trend["conclusion"], "insufficient_indicators")
        self.assertIn("ema_5_unavailable", trend["reason_codes"])
        self.assertEqual(trend["findings"]["ema_trend"], "undetermined")
        evidence = {"indicators": {}, "keys": {"ema_fast": "ema_3", "ema_slow": "ema_5", "rsi": "rsi_3",
                                                "volume_sma": "volume_sma_3", "vwap": None},
                    "thresholds": {"rsi_overbought": "70", "rsi_oversold": "30", "volume_elevated_multiple": "1.5"},
                    "last_bar": {"sequence": 4, "close": "1", "volume": "1"}}
        output = role.trend_agent(evidence, ())
        self.assertEqual(output["conclusion"], "insufficient_indicators")
        self.assertIn("missing_ema_3", output["reason_codes"])
        self.assertCode("sim_time_before_data", self.workflow, self.fixture("synth1-1d-dst"), "2026-03-05T05:00:00Z")
        self.assertCode("invalid_sim_time", self.workflow, self.fixture("synth1-5m-reclaim"), "yesterday")

    def test_expired_signals_are_never_active(self):
        run = self.workflow(self.fixture("synth1-5m"))                           # signals on bars 3 and 11 have expired
        strategy = self.stage(run, "strategy_agent")
        self.assertEqual((strategy["conclusion"], strategy["findings"]["expired_signals"], strategy["findings"]["active_signals"]),
                         ("no_active_signal", 2, []))
        self.assertIn("expired_signals_not_actionable", strategy["reason_codes"])
        signal = {"signal_id": "rsig-" + "0" * 24, "strategy": {"name": "x"}, "bar": {"sequence": 1},
                  "detected_at_sim_utc": "2026-01-15T14:35:00Z", "expires_at_utc": "2026-01-15T14:40:00Z",
                  "expired_when_detected": False, "supporting_values": {}}
        base = {"sim_time_utc": "2026-01-15T14:40:00Z", "latest_evaluations": {}, "signals_truncated": False}
        self.assertEqual(role.strategy_agent({**base, "signals": [signal]}, ())["conclusion"], "no_active_signal")  # expiry == T
        late = dict(signal, expires_at_utc="2026-01-15T14:45:00Z", expired_when_detected=True)
        self.assertEqual(role.strategy_agent({**base, "signals": [late]}, ())["conclusion"], "no_active_signal")
        live = dict(signal, expires_at_utc="2026-01-15T14:45:00Z")
        self.assertEqual(role.strategy_agent({**base, "signals": [live]}, ())["conclusion"], "active_research_signal")

    def test_conflicting_and_corroborating_signals(self):
        conflict = self.workflow(self.fixture("synth1-5m"), "2026-01-15T15:30:00Z")
        strategy = self.stage(conflict, "strategy_agent")
        self.assertEqual((strategy["conclusion"], strategy["findings"]["conflicts"]),
                         ("conflicting_signals", ["signal_in_overbought_zone"]))
        self.assertIn("conflicting_research_signals", self.stage(conflict, "risk_review")["reason_codes"])
        signal = {"signal_id": "rsig-" + "1" * 24, "strategy": {"name": "x"}, "bar": {"sequence": 5},
                  "detected_at_sim_utc": "2026-01-15T14:55:00Z", "expires_at_utc": "2026-01-15T15:00:00Z",
                  "expired_when_detected": False, "supporting_values": {"close": "1"}}
        prior = ({"role": "trend_agent", "status": "completed", "findings": {"ema_trend": "downtrend", "rsi_zone": "neutral_zone"}},)
        output = role.strategy_agent({"sim_time_utc": "2026-01-15T14:55:00Z", "signals": [signal], "latest_evaluations": {},
                                      "signals_truncated": False}, prior)
        self.assertEqual((output["conclusion"], output["findings"]["conflicts"]), ("conflicting_signals", ["signal_against_trend"]))
        agreeing = self.stage(self.workflow(self.fixture("synth1-5m-reclaim")), "strategy_agent")
        self.assertTrue(agreeing["findings"]["corroborating"])
        self.assertEqual(len(agreeing["findings"]["active_signals"]), 3)

    def test_gaps_block_the_review(self):
        run = self.workflow(self.fixture("synth1-5m"))
        self.assertIn("gaps_in_window", self.stage(run, "market_scout")["reason_codes"])
        self.assertEqual(self.stage(run, "market_scout")["findings"]["window_gaps"], 1)
        review = self.stage(run, "risk_review")
        self.assertEqual(review["conclusion"], "insufficient_for_future_paper_evaluation")
        self.assertIn("recent_gaps_in_data", review["reason_codes"])

    def test_stale_data(self):
        dataset = self.fixture("synth1-5m-reclaim")
        replay_config = deepcopy(self.market_config)
        run = run_workflow(dataset, workflow_config=self.config, market_config=replay_config,
                           indicator_config=self.indicator_config, signal_config=self.signal_config, created_at=NOW,
                           sim_time="2026-01-20T15:40:00Z")                    # 4 intervals after the last close
        scout = self.stage(run, "market_scout")
        self.assertEqual((scout["conclusion"], scout["findings"]["age_seconds"]), ("stale_data", 1200))
        self.assertEqual(self.stage(run, "strategy_agent")["findings"]["active_signals"], [])


class IntegrityTests(Base):
    def test_deterministic(self):
        dataset = self.fixture("synth1-5m-reclaim")
        one, two = self.workflow(dataset), self.workflow(dataset)
        self.assertEqual((one["run_id"], one["results_sha256"]), (two["run_id"], two["results_sha256"]))
        self.assertNotEqual(one["run_id"], self.workflow(dataset, "2026-01-20T15:10:00Z")["run_id"])

    def test_no_future_data(self):
        full = self.fixture("synth1-5m-reclaim")
        sim_time = "2026-01-20T15:00:00Z"                                         # 6 of 10 bars closed
        fixture = json.loads((ROOT / "examples/trading/market/synth1-5m-reclaim.json").read_text(encoding="utf-8"))
        lines = ["timestamp,open,high,low,close,volume"] + [
            ",".join(b[k] for k in ("timestamp", "open", "high", "low", "close", "volume")) for b in fixture["bars"][:6]]
        path = self.root / "truncated.csv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        truncated = self.store.import_dataset(make_adapter("csv", config=self.market_config, file=str(path), symbol="SYNTH1"),
                                              config=self.market_config, config_sha256="0" * 64, interval="5m",
                                              tz="America/New_York", label="synthetic")

        def comparable(run):
            text = json.dumps([{k: s[k] for k in ("role", "status", "conclusion", "summary", "findings", "reason_codes")}
                               for s in run["stages"]], sort_keys=True)
            return re.sub(r"rsig-[0-9a-f]{24}", "rsig-x", text)
        early = self.workflow(full, sim_time)
        self.assertEqual(comparable(early), comparable(self.workflow(truncated, sim_time)))
        self.assertEqual(self.stage(early, "market_scout")["findings"]["closed_bars_total"], 6)
        self.assertNotIn("49.90\"", json.dumps(self.stage(early, "market_scout")))  # the later 49.90 close is unseen

    def test_tampered_inputs_and_runs(self):
        dataset = self.fixture("synth1-5m-reclaim")
        run = self.workflow(dataset)
        store = AgentRunStore(self.root)
        store.save(run)
        self.assertCode("agent_run_exists", store.save, run)
        path = store.folder / f"{run['run_id']}.json"
        changed = deepcopy(run)
        changed["final"]["verdict"] = "insufficient_for_future_paper_evaluation"
        path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertCode("agent_run_corrupt", store.load, run["run_id"])
        reordered = deepcopy(run)
        reordered["stages"][0], reordered["stages"][1] = reordered["stages"][1], reordered["stages"][0]
        self.assertRaises(TradingError, validate_run, reordered)
        flipped = deepcopy(run)
        flipped["authorization_possible"] = True
        self.assertRaises(TradingError, validate_run, flipped)
        data_path = self.root / "runtime/trading/market/datasets" / f"{dataset['dataset_id']}.json"
        data = json.loads(data_path.read_text(encoding="utf-8"))
        data["bars"][-1]["close"] = "60.00"
        data_path.write_text(json.dumps(data), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            agent_main(["agent-run", dataset["dataset_id"]], root=self.root)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "dataset_corrupt")
        bad = deepcopy(self.config)
        bad["trend"]["ema_fast"] = 9
        self.assertCode("invalid_agent_config", self.workflow, self.fixture("synth1-5m"), config=bad)

    def test_interrupted_write(self):
        run = self.workflow(self.fixture("synth1-5m-reclaim"))
        store = AgentRunStore(self.root)
        with patch("vicekrack.trading.agents.store.os.link", side_effect=OSError("disk /private")):
            error = self.assertCode("agent_write_failed", store.save, run)
        self.assertNotIn("/private", str(error))
        self.assertEqual(store.list(), [])
        store.save(run)
        self.assertEqual(store.list()[0]["verdict"], "sufficient_for_future_paper_evaluation")

    def test_paper_accounts_untouched_and_no_authorization_code(self):
        PaperAccount("agent-guard", root=self.root, clock=lambda: NOW).initialize()
        state = self.root / "runtime/trading/accounts/acct-agent-guard/state.json"
        before = state.read_bytes()
        AgentRunStore(self.root).save(self.workflow(self.fixture("synth1-5m-reclaim")))
        self.assertEqual(state.read_bytes(), before)
        for path in (ROOT / "vicekrack/trading/agents").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in (".state", ".risk", ".orders", ".journal", "PaperAccount", "build_intent", "authorize(",
                              "openai", "anthropic", "socket", "urllib", "requests", "http.client", "subprocess",
                              "threading", "while True", "os.environ"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


class CliTests(Base):
    def cli(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = agent_main(list(argv), root=self.root)
        return code, json.loads(output.getvalue())

    def test_commands(self):
        dataset = self.fixture("synth1-5m-reclaim")
        code, run = self.cli("agent-run", dataset["dataset_id"])
        self.assertEqual((code, run["saved"], run["verdict"], run["authorization_possible"]),
                         (0, False, "sufficient_for_future_paper_evaluation", False))
        self.assertEqual(self.cli("agent-list")[1]["runs"], [])
        code, saved = self.cli("agent-run", dataset["dataset_id"], "--as-of", "2026-01-20T15:00:00Z", "--save")
        self.assertEqual((code, saved["saved"]), (0, True))
        self.assertEqual(self.cli("agent-run", dataset["dataset_id"], "--as-of", "2026-01-20T15:00:00Z", "--save")[1]["error"]["code"],
                         "agent_run_exists")
        code, shown = self.cli("agent-inspect", saved["run_id"], "--role", "trend_agent")
        self.assertEqual((code, [s["role"] for s in shown["stages"]]), (0, ["trend_agent"]))
        self.assertEqual(self.cli("agent-list")[1]["runs"][0]["run_id"], saved["run_id"])
        self.assertEqual(self.cli("agent-inspect", "rar-bad")[1]["error"]["code"], "invalid_agent_run_id")
        self.assertEqual(self.cli("agent-run", dataset["dataset_id"], "--as-of", "2026-01-20T14:00:00Z")[1]["error"]["code"],
                         "sim_time_before_data")


if __name__ == "__main__":
    unittest.main()
