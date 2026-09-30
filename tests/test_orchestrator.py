import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from shutil import copytree

from vicekrack import Orchestrator
from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT, read_json


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.runner = Orchestrator()
        self.task = read_json(ROOT / "examples/research-task.json")

    def assert_failure(self, code):
        result = self.runner.run(self.task)
        self.runner.validate(result)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], code)
        self.assertNotIn("result", result)
        return result

    def test_end_to_end_preserves_parent_and_routes_child(self):
        original = deepcopy(self.task)
        result = self.runner.run(self.task)
        self.runner.validate(result)
        self.assertEqual(self.task, original)
        for key in ("task_id", "sender", "recipient", "instructions", "context", "created_at"):
            self.assertEqual(result[key], original[key])
        self.assertEqual(result["status"], "completed")
        child = result["result"]["data"]["delegated_task"]
        self.runner.validate(child)
        self.assertEqual(child["recipient"], "researcher")
        self.assertEqual(child["sender"], "orchestrator")
        self.assertEqual(child["parent_task_id"], self.task["task_id"])
        self.assertNotEqual(child["task_id"], self.task["task_id"])
        self.assertEqual(child["result"]["data"]["provider"], "mock")
        self.assertIn(self.task["context"]["design_notes"][0], child["result"]["summary"])

    def test_direct_researcher_and_summarization(self):
        self.task["recipient"] = "researcher"
        self.task["context"]["capability"] = "summarization"
        self.assertEqual(self.runner.run(self.task)["status"], "completed")

    def test_unsupported_capability(self):
        self.task["context"]["capability"] = "translation"
        self.assert_failure("unsupported_capability")

    def test_direct_recipient_capability_mismatch(self):
        self.task["recipient"] = "researcher"
        self.task["context"]["capability"] = "translation"
        self.assert_failure("unsupported_capability")

    def test_missing_capability(self):
        del self.task["context"]["capability"]
        self.assert_failure("missing_capability")

    def test_unknown_agent(self):
        self.task["recipient"] = "unknown"
        self.assert_failure("unknown_agent")

    def test_disabled_agent(self):
        self.runner.agents["researcher"]["enabled"] = False
        self.task["recipient"] = "researcher"
        self.assert_failure("agent_disabled")

    def test_disabled_worker_excluded_from_routing(self):
        self.runner.agents["researcher"]["enabled"] = False
        self.assert_failure("unsupported_capability")

    def test_disabled_orchestrator(self):
        self.runner.agents["orchestrator"]["enabled"] = False
        self.assert_failure("agent_disabled")

    def test_ambiguous_capability(self):
        agent = deepcopy(self.runner.agents["researcher"])
        agent["id"] = "second-researcher"
        self.runner.agents[agent["id"]] = agent
        self.assert_failure("ambiguous_capability")

    def test_missing_handler(self):
        self.runner.handlers.clear()
        self.assert_failure("agent_unavailable")

    def test_unbound_adapter(self):
        self.runner.agents["researcher"]["execution"]["adapter"] = None
        self.assert_failure("adapter_unavailable")

    def test_mock_rejects_model(self):
        self.runner.agents["researcher"]["execution"]["model"] = "some-model"
        self.assert_failure("unsupported_model")

    def test_missing_notes(self):
        del self.task["context"]["design_notes"]
        self.assert_failure("missing_research_context")

    def test_bad_provider_result(self):
        class BadProvider:
            def research(self, **kwargs):
                return {"summary": ""}
        self.runner.providers["mock"] = BadProvider()
        self.assert_failure("invalid_agent_result")

    def test_provider_exception_redacted(self):
        class FailingProvider:
            def research(self, **kwargs):
                raise RuntimeError("sensitive-provider-diagnostic")
        self.runner.providers["mock"] = FailingProvider()
        result = self.assert_failure("execution_failed")
        self.assertNotIn("sensitive-provider-diagnostic", json.dumps(result))

    def test_provider_is_replaceable_and_cannot_mutate_task(self):
        class AlternateProvider:
            def research(self, **kwargs):
                kwargs["notes"].clear()
                return {"summary": "Alternate local provider"}
        self.runner.providers["mock"] = AlternateProvider()
        before = deepcopy(self.task)
        result = self.runner.run(self.task)
        self.assertEqual(result["result"]["summary"], "Alternate local provider")
        self.assertEqual(self.task, before)
        self.assertEqual(result["context"], before["context"])

    def test_schema_rejects_bad_inputs(self):
        cases = [None, [], {}, {**self.task, "extra": True},
                 {**self.task, "created_at": "2026-02-30T12:00:00Z"},
                 {**self.task, "updated_at": "2020-01-01T00:00:00Z"},
                 {**self.task, "result": {"summary": "premature"}},
                 {**self.task, "context": {"number": float("nan")}}]
        for task in cases:
            with self.subTest(task=task), self.assertRaises(NetworkError) as error:
                self.runner.run(task)
            self.assertEqual(error.exception.code, "invalid_task")

    def test_terminal_and_running_tasks_cannot_be_resubmitted(self):
        for status in ("running", "completed", "failed"):
            task = deepcopy(self.task)
            task["status"] = status
            if status == "completed":
                task["result"] = {"summary": "Done"}
            if status == "failed":
                task["error"] = {"code": "failure", "message": "Failed"}
            with self.subTest(status=status), self.assertRaises(NetworkError) as error:
                self.runner.run(task)
            self.assertEqual(error.exception.code, "invalid_status")

    def test_duplicate_id(self):
        self.runner.run(self.task)
        with self.assertRaises(NetworkError) as error:
            self.runner.run(self.task)
        self.assertEqual(error.exception.code, "duplicate_task")

    def test_transition_rejects_terminal_reentry(self):
        result = self.runner.run(self.task)
        with self.assertRaises(NetworkError):
            self.runner._transition(result, "running")

    def test_invalid_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("config", "agents", "schemas"):
                copytree(ROOT / name, root / name)
            registry = read_json(root / "config/agents.json")
            registry["agents"].append(registry["agents"][0])
            (root / "config/agents.json").write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaises(NetworkError) as error:
                Orchestrator(root)
            self.assertEqual(error.exception.code, "invalid_configuration")

    def test_cli_success_failure_and_malformed_json(self):
        cases = [("examples/research-task.json", None, 0, "completed"),
                 ("examples/unsupported-task.json", None, 1, "failed"),
                 ("-", "{", 1, None), ("missing-task.json", None, 1, None)]
        for path, input_text, code, status in cases:
            with self.subTest(path=path):
                process = subprocess.run([sys.executable, "-m", "vicekrack", path],
                                         input=input_text, capture_output=True, text=True, cwd=ROOT)
                self.assertEqual(process.returncode, code, process.stderr)
                output = json.loads(process.stdout)
                self.assertEqual(output.get("status"), status)
                self.assertEqual(process.stderr, "")


if __name__ == "__main__":
    unittest.main()
