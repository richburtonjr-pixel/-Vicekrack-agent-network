import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from vicekrack import Orchestrator
from vicekrack.errors import NetworkError
from vicekrack.handoff import validate_handoff
from vicekrack.orchestrator import ROOT, read_json


class RecordingProvider:
    def __init__(self, calls, name, fail_role=None, malformed=False):
        self.calls, self.name = calls, name
        self.fail_role, self.malformed = fail_role, malformed

    def research(self, *, instructions, notes, model):
        self.calls.append((self.name, instructions, deepcopy(notes), model))
        if self.fail_role and instructions.startswith(f"Role: {self.fail_role}."):
            raise NetworkError("provider_timeout", "synthetic sensitive diagnostic")
        if self.malformed:
            return {"summary": ""}
        return {"summary": "Findings and limitations from " + self.name,
                "data": {"provider": self.name}}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.task = read_json(ROOT / "examples/workflow-task.json")
        self.runner = Orchestrator(registry_path="config/agents.workflow.json")
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("No network in tests")).start()

    def test_full_mock_workflow(self):
        original = deepcopy(self.task)
        result = self.runner.run(self.task)
        self.runner.validate(result)
        self.assertEqual(self.task, original)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([row["agent"] for row in result["execution_trace"]],
                         ["researcher", "analyst", "reviewer"])
        self.assertEqual(len(self.runner.seen_ids), 4)
        self.assertEqual(result["result"]["summary"], result["result"]["data"]["stages"][-1]["result"]["summary"])

    def test_mixed_providers_and_structured_combined_handoff(self):
        calls = []
        runner = Orchestrator(registry_path="config/agents.workflow-mixed.json", providers={
            "openai": RecordingProvider(calls, "openai"),
            "anthropic": RecordingProvider(calls, "anthropic")})
        result = runner.run(self.task)
        runner.validate(result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call[0] for call in calls], ["openai", "anthropic", "anthropic"])
        analyst = json.loads(calls[1][2][0])
        reviewer = json.loads(calls[2][2][0])
        self.assertEqual(analyst["task_id"], self.task["task_id"])
        self.assertEqual(analyst["original_request"]["instructions"], self.task["instructions"])
        self.assertEqual(analyst["provider_used"], "openai")
        self.assertEqual(reviewer["provider_used"], "anthropic")
        self.assertEqual([record["agent"] for record in reviewer["history"]], ["researcher", "analyst"])
        self.assertEqual(reviewer["previous_output"], reviewer["history"][-1]["result"])

    def test_failure_stops_pipeline_and_sanitizes_trace(self):
        calls = []
        self.runner.providers["mock"] = RecordingProvider(calls, "mock", fail_role="analyst")
        result = self.runner.run(self.task)
        self.runner.validate(result)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "provider_timeout")
        self.assertEqual(len(calls), 2)
        self.assertEqual([row["status"] for row in result["execution_trace"]], ["completed", "failed"])
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertNotIn("result", result)

    def test_research_failure_prevents_later_stages(self):
        self.task["context"]["design_notes"] = []
        result = self.runner.run(self.task)
        self.assertEqual(result["error"]["code"], "missing_research_context")
        self.assertEqual(len(result["execution_trace"]), 1)

    def test_reviewer_failure_is_not_success(self):
        self.runner.providers["mock"] = RecordingProvider([], "mock", fail_role="reviewer")
        result = self.runner.run(self.task)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["execution_trace"]), 3)

    def test_budget_preflight_makes_no_calls(self):
        calls = []
        self.runner.providers["mock"] = RecordingProvider(calls, "mock")
        self.runner.workflow["max_steps"] = 2
        result = self.runner.run(self.task)
        self.assertEqual(result["error"]["code"], "maximum_steps_exceeded")
        self.assertEqual(result["execution_trace"], [])
        self.assertEqual(calls, [])

    def test_invalid_limits_and_cycles(self):
        for limit in (0, 4, True, "3"):
            runner = Orchestrator(registry_path="config/agents.workflow.json")
            runner.workflow["max_steps"] = limit
            self.assertEqual(runner.run(self.task)["error"]["code"], "invalid_workflow")
        for stages in (["researcher", "analyst", "researcher"], ["orchestrator"], []):
            runner = Orchestrator(registry_path="config/agents.workflow.json")
            runner.workflow["agents"] = stages
            self.assertEqual(runner.run(self.task)["error"]["code"], "invalid_workflow")

    def test_disabled_or_unsupported_agent_preflight(self):
        self.runner.agents["reviewer"]["enabled"] = False
        result = self.runner.run(self.task)
        self.assertEqual(result["error"]["code"], "workflow_agent_unavailable")
        self.assertEqual(result["execution_trace"], [])

    def test_unsupported_provider_preflight(self):
        self.runner.agents["reviewer"]["execution"]["adapter"] = "unknown"
        result = self.runner.run(self.task)
        self.assertEqual(result["error"]["code"], "adapter_unavailable")
        self.assertEqual(result["execution_trace"], [])

    def test_invalid_agent_result(self):
        self.runner.providers["mock"] = RecordingProvider([], "mock", malformed=True)
        result = self.runner.run(self.task)
        self.assertEqual(result["error"]["code"], "invalid_agent_result")
        self.assertEqual(len(result["execution_trace"]), 1)

    def test_cannot_forge_trace_or_start_from_worker(self):
        task = deepcopy(self.task)
        task["execution_trace"] = []
        with self.assertRaises(NetworkError):
            self.runner.run(task)
        self.task["recipient"] = "researcher"
        self.assertEqual(self.runner.run(self.task)["error"]["code"], "invalid_workflow")

    def test_bad_handoff_rejected(self):
        with self.assertRaises(NetworkError) as raised:
            validate_handoff({"context": {"handoff": {}}}, "analyst")
        self.assertEqual(raised.exception.code, "invalid_handoff")

    def test_unknown_workflow(self):
        self.task["context"]["workflow"] = "unbounded"
        self.assertEqual(self.runner.run(self.task)["error"]["code"], "unsupported_workflow")

    def test_trace_is_metadata_only(self):
        result = self.runner.run(self.task)
        for row in result["execution_trace"]:
            self.assertEqual(set(row), {"step", "agent", "provider", "status"})

    def test_provider_output_cannot_extend_workflow(self):
        calls = []
        class LoopRequestProvider(RecordingProvider):
            def research(self, **kwargs):
                result = super().research(**kwargs)
                result["data"]["next_agent"] = "orchestrator"
                result["data"]["repeat"] = True
                return result
        self.runner.providers["mock"] = LoopRequestProvider(calls, "mock")
        result = self.runner.run(self.task)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result["execution_trace"]), 3)

    def test_semantically_inconsistent_handoff_is_rejected(self):
        calls = []
        self.runner.providers["mock"] = RecordingProvider(calls, "mock")
        self.runner.run(self.task)
        handoff = json.loads(calls[1][2][0])
        task = {"task_id": handoff["stage_task_id"], "parent_task_id": handoff["task_id"],
                "context": {"handoff": handoff}}
        handoff["metadata"]["step"] = 3
        with self.assertRaises(NetworkError) as raised:
            validate_handoff(task, "analyst")
        self.assertEqual(raised.exception.code, "invalid_handoff")
