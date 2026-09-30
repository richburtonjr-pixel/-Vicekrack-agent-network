import json
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch
from vicekrack.manager import AgentManager, WorkflowState
from vicekrack.orchestrator import Orchestrator, ROOT, read_json
from vicekrack.persistence import RunStore, SavedRuns
from vicekrack.errors import NetworkError
from test_persistence import Provider


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(self.temp.name)
        self.runs = SavedRuns(self.store)
        self.task = read_json(ROOT / "examples/workflow-task.json")
        self.provider = Provider()
        self.guard = patch("socket.socket.connect", side_effect=AssertionError("No external calls"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def start(self):
        return self.runs.start(self.task, providers={"mock": self.provider})

    def resume(self, run_id, **kw):
        return self.runs.resume(run_id, providers={"mock": self.provider}, **kw)

    def test_inventory_and_permitted_handoffs(self):
        runner = Orchestrator(registry_path="config/agents.workflow.json")
        inventory = {row["agent"]: row for row in runner.manager.inventory()}
        self.assertEqual(inventory["researcher"]["next_agent"], "analyst")
        self.assertEqual(inventory["analyst"]["provider"], "mock")
        self.assertEqual(inventory["reviewer"]["capabilities"], ["review"])
        with self.assertRaises(NetworkError):
            runner.manager.begin("reviewer", "researcher")
        runner.manager.begin("researcher", None)
        with self.assertRaises(NetworkError):
            runner.manager.begin("researcher", None)

    def test_successful_state_and_audit(self):
        result = self.start()
        state = self.store.read(result["run_id"])["workflow_state"]
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["completed_stages"], ["researcher", "analyst", "reviewer"])
        self.assertIsNone(state["next_agent"])
        self.assertIsNone(state["current_agent"])
        self.assertEqual(state["retry_count"], 0)
        self.assertEqual(len(state["audit_trace"]), 6)
        self.assertNotIn(self.task["instructions"], json.dumps(state))
        WorkflowState.validate(state)

    def test_explicit_recovery_retains_failed_attempt(self):
        self.provider.failure = "missing_credentials"
        result = self.start()
        state = self.store.read(result["run_id"])["workflow_state"]
        self.assertEqual(state["next_agent"], "analyst")
        self.assertEqual(state["status"], "failed")
        self.provider.failure = None
        self.resume(result["run_id"])
        state = self.store.read(result["run_id"])["workflow_state"]
        self.assertEqual(state["attempts"], {"researcher": 1, "analyst": 2, "reviewer": 1})
        self.assertEqual(state["retry_count"], 1)
        self.assertEqual(state["failures"][0]["error"], "missing_credentials")
        self.assertEqual(len(state["audit_trace"]), 8)

    def test_recovery_exhausted_is_final(self):
        self.provider.failure = "missing_credentials"
        result = self.start()
        self.assertEqual(self.resume(result["run_id"])["status"], "failed")
        state = self.store.read(result["run_id"])["workflow_state"]
        self.assertEqual(state["status"], "exhausted")
        before = list(self.provider.calls)
        with self.assertRaises(NetworkError) as error:
            self.resume(result["run_id"])
        self.assertEqual(error.exception.code, "retry_exhausted")
        self.assertEqual(before, self.provider.calls)

    def test_duplicate_task_across_new_instances(self):
        self.start()
        with self.assertRaises(NetworkError) as error:
            SavedRuns(self.store).start(self.task, providers={"mock": self.provider})
        self.assertEqual(error.exception.code, "duplicate_task")
        self.assertEqual(len(self.provider.calls), 3)

    def test_concurrent_task_start_lock(self):
        import hashlib
        key = hashlib.sha256(self.task["task_id"].encode()).hexdigest()[:32]
        with self.store.lock(key), self.assertRaises(NetworkError) as error:
            self.start()
        self.assertEqual(error.exception.code, "run_locked")
        self.assertEqual(self.provider.calls, [])

    def test_invalid_transitions_and_zero_retry_budget(self):
        state = WorkflowState("task", 0)
        with self.assertRaises(NetworkError):
            state.begin("analyst", "mock")
        state.begin("researcher", "mock")
        with self.assertRaises(NetworkError):
            state.begin("analyst", "mock")
        state.finish(False, "provider_timeout")
        self.assertEqual(state.data["status"], "exhausted")
        with self.assertRaises(NetworkError):
            state.begin("researcher", "mock")
        WorkflowState.validate(state.data)

    def test_retry_limits_and_step_budget_preflight(self):
        for budget in (-1, 4, True, "2"):
            runner = Orchestrator(registry_path="config/agents.workflow.json", providers={"mock": self.provider})
            runner.workflow["max_retries"] = budget
            self.assertEqual(runner.run(self.task)["error"]["code"], "invalid_workflow")
        runner = Orchestrator(registry_path="config/agents.workflow.json", providers={"mock": self.provider})
        runner.workflow["max_steps"] = 2
        self.assertEqual(runner.run(self.task)["error"]["code"], "maximum_steps_exceeded")
        self.assertEqual(self.provider.calls, [])

    def test_mixed_provider_audit(self):
        runner = Orchestrator(registry_path="config/agents.workflow-mixed.json",
                              providers={"openai": self.provider, "anthropic": self.provider})
        self.assertEqual(runner.run(self.task)["status"], "completed")
        self.assertEqual([e["provider"] for e in runner.last_workflow_state["audit_trace"] if e["status"] == "running"],
                         ["openai", "anthropic", "anthropic"])

    def test_state_tampering_rejected(self):
        run_id = self.start()["run_id"]
        original = self.store.read(run_id)
        for field, value in [("retry_count", 9), ("next_agent", "researcher"), ("failures", [{}]), ("status", "ready")]:
            changed = deepcopy(original)
            changed["workflow_state"][field] = value
            self.store.path(run_id).write_text(json.dumps(changed))
            with self.subTest(field=field), self.assertRaises(NetworkError):
                self.store.read(run_id)

    def test_unknown_error_text_not_in_audit(self):
        state = WorkflowState("task")
        state.begin("researcher", "openai")
        state.finish(False, "untrusted-secret-message")
        self.assertNotIn("untrusted-secret-message", json.dumps(state.data))
        self.assertEqual(state.data["failures"][0]["error"], "execution_failed")

    def test_legacy_snapshot_can_resume(self):
        self.provider.failure = "missing_credentials"
        run_id = self.start()["run_id"]
        state = self.store.read(run_id)
        del state["workflow_state"]
        self.store.write(state)
        self.provider.failure = None
        self.assertEqual(self.resume(run_id)["status"], "completed")
        self.assertEqual(self.provider.calls.count("researcher"), 1)

    def test_uncertain_request_keeps_attempt_count(self):
        self.provider.failure = "interrupt"
        with self.assertRaises(KeyboardInterrupt):
            self.start()
        run_id = self.store.list_runs()[0]["run_id"]
        self.provider.failure = None
        with self.assertRaises(NetworkError):
            self.resume(run_id)
        self.resume(run_id, retry_uncertain=True)
        state = self.store.read(run_id)["workflow_state"]
        self.assertEqual(state["attempts"]["analyst"], 2)
        self.assertEqual(state["failures"][0]["error"], "interrupted")

    def test_last_permitted_attempt_interrupted_becomes_final_failure(self):
        self.provider.failure = "missing_credentials"
        run_id = self.start()["run_id"]
        self.provider.failure = "interrupt"
        with self.assertRaises(KeyboardInterrupt):
            self.resume(run_id)
        before = list(self.provider.calls)
        with self.assertRaises(NetworkError) as error:
            self.resume(run_id, retry_uncertain=True)
        self.assertEqual(error.exception.code, "retry_exhausted")
        state = self.store.read(run_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["workflow_state"]["status"], "exhausted")
        self.assertEqual(state["workflow_state"]["failures"][-1]["error"], "interrupted")
        self.assertEqual(before, self.provider.calls)

    def test_retry_policy_change_rejects_resume(self):
        self.provider.failure = "missing_credentials"
        run_id = self.start()["run_id"]
        runner = Orchestrator(registry_path="config/agents.workflow.json", providers={"mock": self.provider})
        runner.workflow["max_retries"] = 2
        with patch("vicekrack.persistence.Orchestrator", return_value=runner):
            with self.assertRaises(NetworkError) as error:
                self.resume(run_id)
        self.assertEqual(error.exception.code, "configuration_mismatch")
        self.assertEqual(len(self.provider.calls), 2)

    def test_preflight_failure_is_reflected_in_workflow_state(self):
        runner = Orchestrator(registry_path="config/agents.workflow.json", providers={"mock": self.provider})
        runner.workflow["max_steps"] = 2
        self.assertEqual(runner.run(self.task)["status"], "failed")
        self.assertEqual(runner.last_workflow_state["status"], "failed")
        self.assertEqual(runner.last_workflow_state["blocked_error"], "maximum_steps_exceeded")
        WorkflowState.validate(runner.last_workflow_state)
        self.assertEqual(self.provider.calls, [])
