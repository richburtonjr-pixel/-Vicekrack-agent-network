import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack.__main__ import main
from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.persistence import RunStore, SavedRuns


class Provider:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def research(self, *, instructions, notes, model):
        role = "analyst" if instructions.startswith("Role: analyst") else "reviewer" if instructions.startswith("Role: reviewer") else "researcher"
        self.calls.append(role)
        if role == "analyst" and self.failure:
            if self.failure == "interrupt":
                raise KeyboardInterrupt
            raise NetworkError(self.failure, "Do not persist this raw diagnostic")
        return {"summary": f"Completed {role}", "data": {"provider": "mock"}}


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(self.temp.name)
        self.runs = SavedRuns(self.store)
        self.task = read_json(ROOT / "examples/workflow-task.json")
        self.provider = Provider()
        guard = patch("socket.socket.connect", side_effect=AssertionError("No API calls"))
        guard.start()
        self.addCleanup(guard.stop)

    def start(self):
        return self.runs.start(self.task, providers={"mock": self.provider})

    def resume(self, run_id, **kwargs):
        return self.runs.resume(run_id, providers={"mock": self.provider}, **kwargs)

    def test_success_saved_and_listed(self):
        result = self.start()
        state = self.store.inspect(result["run_id"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["task"], self.task)
        self.assertEqual(len(state["history"]), 3)
        self.assertEqual(len(state["trace"]), 3)
        self.assertEqual(state["outcome"], result["task"])
        self.assertEqual(self.store.list_runs()[0]["run_id"], result["run_id"])
        with self.assertRaises(NetworkError) as error:
            self.resume(result["run_id"])
        self.assertEqual(error.exception.code, "run_completed")
        self.assertEqual(len(self.provider.calls), 3)

    def test_resume_reuses_research(self):
        self.provider.failure = "missing_credentials"
        first = self.start()
        self.assertEqual(first["status"], "failed")
        self.provider.failure = None
        result = self.resume(first["run_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.provider.calls, ["researcher", "analyst", "analyst", "reviewer"])
        self.assertNotIn("raw diagnostic", self.store.path(first["run_id"]).read_text())

    def test_uncertain_timeout_requires_explicit_choice(self):
        self.provider.failure = "provider_timeout"
        result = self.start()
        self.assertEqual(result["status"], "uncertain")
        with self.assertRaises(NetworkError) as error:
            self.resume(result["run_id"])
        self.assertEqual(error.exception.code, "uncertain_stage")
        self.assertEqual(len(self.provider.calls), 2)
        self.provider.failure = None
        self.assertEqual(self.resume(result["run_id"], retry_uncertain=True)["status"], "completed")

    def test_interrupted_request_is_uncertain_and_lock_released(self):
        self.provider.failure = "interrupt"
        with self.assertRaises(KeyboardInterrupt):
            self.start()
        run_id = self.store.list_runs()[0]["run_id"]
        self.assertEqual(self.store.inspect(run_id)["status"], "uncertain")
        with self.assertRaises(NetworkError) as error:
            self.resume(run_id)
        self.assertEqual(error.exception.code, "uncertain_stage")
        self.provider.failure = None
        self.assertEqual(self.resume(run_id, retry_uncertain=True)["status"], "completed")
        self.assertEqual(self.provider.calls.count("researcher"), 1)

    def test_failed_result_checkpoint_preserves_intent(self):
        real_write = self.store.write
        writes = []
        def fail_after_provider(state):
            writes.append(state["status"])
            if len(writes) == 3:
                raise NetworkError("storage_error", "simulated")
            real_write(state)
        with patch.object(self.store, "write", side_effect=fail_after_provider):
            with self.assertRaises(NetworkError):
                self.start()
        run_id = self.store.list_runs()[0]["run_id"]
        state = self.store.inspect(run_id)
        self.assertEqual(state["status"], "uncertain")
        self.assertEqual(state["history"], [])
        self.assertEqual(self.provider.calls, ["researcher"])

    def test_atomic_replace_failure_preserves_previous_file(self):
        result = self.start()
        before = self.store.path(result["run_id"]).read_bytes()
        state = self.store.read(result["run_id"])
        with patch("vicekrack.persistence.os.replace", side_effect=OSError("simulated")):
            with self.assertRaises(NetworkError):
                self.store.write(state)
        self.assertEqual(before, self.store.path(result["run_id"]).read_bytes())
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp")), [])

    def test_crash_after_all_stages_does_not_call_provider_again(self):
        real_write = self.store.write
        def fail_final(state):
            if state["status"] == "completed":
                raise NetworkError("storage_error", "simulated")
            real_write(state)
        with patch.object(self.store, "write", side_effect=fail_final):
            with self.assertRaises(NetworkError):
                self.start()
        run_id = self.store.list_runs()[0]["run_id"]
        self.assertEqual(self.store.read(run_id)["status"], "ready")
        self.assertEqual(self.resume(run_id)["status"], "completed")
        self.assertEqual(len(self.provider.calls), 3)

    def test_config_mismatch_no_calls(self):
        self.provider.failure = "missing_credentials"
        run_id = self.start()["run_id"]
        with self.assertRaises(NetworkError) as error:
            self.runs.resume(run_id, registry_path="config/agents.workflow-mixed.json", providers={"mock": self.provider})
        self.assertEqual(error.exception.code, "configuration_mismatch")
        self.assertEqual(len(self.provider.calls), 2)

    def test_corruption_and_invalid_history(self):
        run_id = self.start()["run_id"]
        original = self.store.path(run_id).read_text()
        for modification in ("bad-json", "history", "trace", "ids"):
            if modification == "bad-json":
                self.store.path(run_id).write_text("{")
            else:
                state = json.loads(original)
                if modification == "history":
                    state["history"][1]["agent"] = "researcher"
                elif modification == "trace":
                    state["trace"] = []
                else:
                    state["history"][1]["task_id"] = state["history"][0]["task_id"]
                self.store.path(run_id).write_text(json.dumps(state))
            with self.subTest(modification=modification), self.assertRaises(NetworkError) as error:
                self.store.inspect(run_id)
            self.assertEqual(error.exception.code, "invalid_state")
        self.assertEqual(self.store.list_runs()[0]["error"], "invalid_state")

    def test_missing_and_path_traversal(self):
        for run_id, code in (("a" * 32, "run_not_found"), ("../secrets", "invalid_run_id")):
            with self.assertRaises(NetworkError) as error:
                self.store.inspect(run_id)
            self.assertEqual(error.exception.code, code)

    def test_os_lock_blocks_another_process(self):
        run_id = self.start()["run_id"]
        code = "from vicekrack.persistence import RunStore; from vicekrack.errors import NetworkError; import sys\ntry:\n with RunStore(sys.argv[1]).lock(sys.argv[2]): print('acquired')\nexcept NetworkError as e: print(e.code)"
        with self.store.lock(run_id):
            result = subprocess.run([sys.executable, "-c", code, self.temp.name, run_id], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.stdout.strip(), "run_locked", result.stderr)
        with self.store.lock(run_id):
            pass

    def test_secret_fields_and_active_values_rejected(self):
        self.task["context"]["api_key"] = "synthetic"
        with self.assertRaises(NetworkError):
            self.start()
        del self.task["context"]["api_key"]
        self.task["instructions"] = "contains synthetic-credential-value"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-credential-value"}):
            with self.assertRaises(NetworkError):
                self.start()
        self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])

    def test_orphan_temp_file_is_not_a_run(self):
        (Path(self.temp.name) / "orphan.tmp").write_text("partial")
        self.assertEqual(self.store.list_runs(), [])

    def test_missing_capability_is_rejected_before_saving(self):
        del self.task["context"]["capability"]
        with self.assertRaises(NetworkError) as error:
            self.start()
        self.assertEqual(error.exception.code, "invalid_task")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.store.list_runs(), [])

    def test_disabled_orchestrator_cannot_start_saved_run(self):
        from vicekrack import Orchestrator
        runner = Orchestrator(registry_path="config/agents.workflow.json", providers={"mock": self.provider})
        runner.agents["orchestrator"]["enabled"] = False
        with patch("vicekrack.persistence.Orchestrator", return_value=runner):
            with self.assertRaises(NetworkError) as error:
                self.start()
        self.assertEqual(error.exception.code, "agent_disabled")
        self.assertEqual(self.provider.calls, [])

    def test_cli_commands(self):
        with patch("vicekrack.persistence.SavedRuns", return_value=self.runs):
            def command(args):
                output = io.StringIO()
                with patch("sys.argv", ["vicekrack"] + args), redirect_stdout(output):
                    code = main()
                return code, json.loads(output.getvalue())
            code, result = command(["run", "examples/workflow-task.json"])
            self.assertEqual(code, 0)
            run_id = result["run_id"]
            self.assertEqual(command(["inspect", run_id])[1]["status"], "completed")
            self.assertEqual(command(["list"])[1]["runs"][0]["run_id"], run_id)
            self.assertEqual(command(["resume", run_id])[1]["error"]["code"], "run_completed")
