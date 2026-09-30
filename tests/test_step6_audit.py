"""Regression cases discovered by inspecting Step 6, beyond its original test suite."""

import tempfile
import unittest
from unittest.mock import patch

from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.persistence import RunStore, SavedRuns, validate_state


class Step6AuditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RunStore(directory.name)
        result = SavedRuns(self.store).start(read_json(ROOT / "examples/workflow-task.json"))
        self.run_id = result["run_id"]
        self.state = self.store.read(self.run_id)

    def test_inspection_does_not_instantiate_default_orchestrator(self):
        with patch("vicekrack.persistence.Orchestrator", side_effect=NetworkError("invalid_configuration", "broken default")):
            self.assertEqual(self.store.inspect(self.run_id)["status"], "completed")

    def test_empty_configuration_cannot_pass_ready_state_validation(self):
        self.state.update(status="ready", history=[], trace=[], outcome=None, config={})
        with self.assertRaises(NetworkError):
            validate_state(self.state, self.run_id)

    def test_boolean_state_version_is_not_a_version_number(self):
        self.state["version"] = True
        with self.assertRaises(NetworkError):
            validate_state(self.state, self.run_id)

    def test_created_timestamp_must_be_full_utc_datetime(self):
        self.state["created_at"] = "2026-09-30Z"
        with self.assertRaises(NetworkError):
            validate_state(self.state, self.run_id)

    def test_handoff_original_request_must_match_child(self):
        from vicekrack.workflow import make_child
        from vicekrack.handoff import validate_handoff
        child = make_child(self.state["task"], self.state["history"][:1], "analyst", "analysis", 2,
                           self.state["task"]["updated_at"])
        child["context"]["handoff"]["original_request"]["instructions"] = "A conflicting request"
        with self.assertRaises(NetworkError) as error:
            validate_handoff(child, "analyst")
        self.assertEqual(error.exception.code, "invalid_handoff")
