import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from vicekrack.__main__ import main
from vicekrack.dashboard import collect, summarize, render, label
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.persistence import RunStore, SavedRuns
from test_persistence import Provider

class DashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = RunStore(temp.name)
        self.provider = Provider()
        self.task = read_json(ROOT / "examples/workflow-task.json")
        self.guard = patch("socket.socket.connect", side_effect=AssertionError("No network"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def start(self):
        return SavedRuns(self.store).start(self.task, providers={"mock": self.provider})["run_id"]

    def command(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["vicekrack", *args]), patch("vicekrack.dashboard.RunStore", return_value=self.store), redirect_stdout(output):
            code = main()
        return code, output.getvalue()

    def test_empty_dashboard_and_agents(self):
        code, output = self.command("dashboard")
        self.assertEqual(code, 0)
        self.assertIn("No saved runs", output)
        self.assertIn("reviewer", output)
        self.assertEqual(self.command("agents")[0], 0)

    def test_success_metadata_only_and_no_execution(self):
        run_id = self.start()
        before = self.store.path(run_id).read_bytes()
        with patch("vicekrack.orchestrator.Orchestrator.run", side_effect=AssertionError("No execution")):
            code, output = self.command("dashboard", run_id, "--json")
        data = json.loads(output)["runs"][0]
        self.assertEqual(code, 0)
        self.assertEqual(data["completed"], 3)
        self.assertEqual(data["recovery"], "complete")
        self.assertEqual(len(data["trace"]), 6)
        self.assertNotIn(self.task["instructions"], output)
        self.assertNotIn("original_request", output)
        self.assertEqual(before, self.store.path(run_id).read_bytes())
        self.assertEqual(len(self.provider.calls), 3)

    def test_failure_and_exhaustion(self):
        self.provider.failure = "missing_credentials"
        run_id = self.start()
        row = collect(self.store, run_id)[0]
        self.assertEqual(row["next_agent"], "analyst")
        self.assertEqual(row["remaining_attempts"], 1)
        SavedRuns(self.store).resume(run_id, providers={"mock": self.provider})
        self.assertIn("exhausted", collect(self.store, run_id)[0]["recovery"])

    def test_uncertain_request(self):
        self.provider.failure = "provider_timeout"
        run_id = self.start()
        self.assertIn("--retry-uncertain", self.command("dashboard", run_id)[1])
        self.assertEqual(len(self.provider.calls), 2)

    def test_corrupt_and_locked_runs(self):
        run_id = self.start()
        with self.store.lock(run_id):
            self.assertIn("run_locked", self.command("dashboard")[1])
        self.store.path(run_id).write_text("private broken content")
        code, output = self.command("dashboard")
        self.assertEqual(code, 1)
        self.assertIn("invalid_state", output)
        self.assertNotIn("private broken content", output)

    def test_missing_run(self):
        code, output = self.command("dashboard", "a"*32)
        self.assertEqual(code, 1)
        self.assertIn("run_not_found", output)

    def test_legacy_budget_is_unknown(self):
        run_id = self.start()
        state = self.store.read(run_id)
        del state["workflow_state"]
        self.store.write(state)
        self.assertIn("unknown (legacy)", self.command("dashboard", run_id)[1])

    def test_selected_run_independent_of_registry(self):
        run_id = self.start()
        self.assertEqual(self.command("dashboard", run_id, "--registry", "missing.json")[0], 0)

    def test_mixed_provider_inventory_without_keys(self):
        code, output = self.command("agents", "--registry", "config/agents.workflow-mixed.json")
        self.assertEqual(code, 0)
        self.assertIn("openai", output)
        self.assertIn("anthropic", output)

    def test_control_characters_and_key_shapes_suppressed(self):
        self.assertNotIn("\x1b", label("\x1b[2J"))
        self.assertEqual(label("sk-" + "x"*30), "[redacted]")

    def test_active_credentials_in_registry_labels_are_redacted(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic-private-value"}):
            self.assertEqual(label("prefix synthetic-private-value suffix"), "[redacted]")
