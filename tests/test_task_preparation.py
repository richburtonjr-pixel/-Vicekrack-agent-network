import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from vicekrack.__main__ import main
from vicekrack.orchestrator import ROOT, Orchestrator, read_json
from vicekrack.errors import NetworkError
from vicekrack.persistence import RunStore, SavedRuns
from vicekrack.task_preparation import create_task, validate_task


class TaskPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.task = read_json(ROOT / "examples/workflow-task.json")

    def create(self, **kwargs):
        return create_task("Summarize supplied evidence", ["First note", "Second note"], directory=self.folder, **kwargs)

    def test_create_unique_valid_tasks_and_end_to_end_mock_run(self):
        first, second = self.create(), self.create()
        self.assertNotEqual(first["task_id"], second["task_id"])
        task = read_json(Path(first["task_file"]))
        self.assertEqual(task["context"]["design_notes"], ["First note", "Second note"])
        self.assertEqual(task["created_at"], task["updated_at"])
        outcome = SavedRuns(RunStore(self.folder / "runs")).start(task)
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_no_provider_client_or_execution_during_preparation(self):
        with patch("vicekrack.orchestrator.Orchestrator.run", side_effect=AssertionError("No execution")), patch("vicekrack.openai_provider.OpenAI", side_effect=AssertionError("No client")), patch("vicekrack.anthropic_provider.Anthropic", side_effect=AssertionError("No client")):
            result = self.create(registry="config/agents.workflow-mixed.json")
        self.assertEqual([row["provider"] for row in result["route"]], ["openai", "anthropic", "anthropic"])
        self.assertNotIn("Summarize supplied evidence", json.dumps(result))

    def test_invalid_notes_and_instructions_never_written(self):
        for instructions, notes in [(" ", ["valid"]), ("valid", []), ("valid", [" "])]:
            with self.subTest(instructions=instructions, notes=notes), self.assertRaises(NetworkError):
                create_task(instructions, notes, directory=self.folder)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_secret_values_and_fields_never_written(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-private-value"}):
            with self.assertRaises(NetworkError):
                create_task("synthetic-private-value", ["note"], directory=self.folder)
        self.task["context"]["api_key"] = "synthetic"
        with self.assertRaises(NetworkError):
            validate_task(self.task)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_existing_file_cannot_be_overwritten(self):
        with patch("vicekrack.task_preparation.uuid4", return_value="fixed-id"):
            first = self.create()
            before = Path(first["task_file"]).read_bytes()
            with self.assertRaises(NetworkError) as error:
                self.create()
        self.assertEqual(error.exception.code, "task_write_failed")
        self.assertEqual(Path(first["task_file"]).read_bytes(), before)
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_publication_failure_removes_temp(self):
        with patch("vicekrack.task_preparation.os.link", side_effect=OSError("synthetic")), self.assertRaises(NetworkError):
            self.create()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_validation_is_read_only(self):
        before = json.dumps(self.task)
        result = validate_task(self.task)
        self.assertTrue(result["valid"])
        self.assertEqual(before, json.dumps(self.task))

    def test_invalid_stage_budget_and_disabled_agent(self):
        for mutation in ("budget", "disabled", "provider", "model"):
            runner = Orchestrator(registry_path="config/agents.workflow.json")
            if mutation == "budget": runner.workflow["max_steps"] = 2
            if mutation == "disabled": runner.agents["analyst"]["enabled"] = False
            if mutation == "provider": runner.agents["analyst"]["execution"]["adapter"] = "unknown"
            if mutation == "model": runner.agents["analyst"]["execution"].update(adapter="openai", model=None)
            with self.subTest(mutation=mutation), patch("vicekrack.task_preparation.Orchestrator", return_value=runner), self.assertRaises(NetworkError):
                validate_task(self.task)

    def command(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["vicekrack", *args]), redirect_stdout(output):
            code = main()
        return code, json.loads(output.getvalue())

    def test_cli_validate_and_missing_file(self):
        self.assertTrue(self.command("validate-task", str(ROOT / "examples/workflow-task.json"))[1]["valid"])
        self.assertEqual(self.command("validate-task", str(self.folder / "absent.json"))[0], 1)

    def test_file_input_cli(self):
        request, notes = self.folder / "request.txt", self.folder / "notes.txt"
        request.write_text("Summarize these notes", encoding="utf-8")
        notes.write_text("First note\n\nSecond note", encoding="utf-8")
        with patch("vicekrack.task_preparation.ROOT", self.folder):
            code, result = self.command("create-task", "--instructions-file", str(request), "--notes-file", str(notes))
        self.assertEqual(code, 0)
        task = read_json(Path(result["task_file"]))
        self.assertEqual(task["context"]["design_notes"], ["First note", "Second note"])

    def test_reject_runtime_trace_or_wrong_workflow(self):
        self.task["execution_trace"] = []
        with self.assertRaises(NetworkError): validate_task(self.task)
        del self.task["execution_trace"]
        self.task["context"]["workflow"] = "unknown"
        with self.assertRaises(NetworkError): validate_task(self.task)
