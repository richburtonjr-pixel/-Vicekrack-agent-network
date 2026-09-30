"""SDK requests use an in-memory HTTP transport; no credits or network required."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import patch

import httpx2
from openai import OpenAI

from vicekrack import Orchestrator
from vicekrack.__main__ import main
from vicekrack.errors import NetworkError
from vicekrack.openai_provider import OpenAIResearchProvider
from vicekrack.orchestrator import ROOT, read_json


def response_body(text='{"summary":"Provider separation keeps routing reusable."}', **overrides):
    body = {
        "id": "resp_test", "object": "response", "created_at": 1,
        "status": "completed", "model": "gpt-4.1-mini",
        "output": [{"id": "msg_test", "type": "message", "status": "completed",
                    "role": "assistant", "content": [
                        {"type": "output_text", "text": text, "annotations": []}]}],
    }
    body.update(overrides)
    return body


class OpenAIProviderTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-placeholder"}, clear=True).start()
        # A regression can never silently turn these tests into live requests.
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()
        self.requests = []
        self.options = []
        self.clients = []
        self.body = response_body()
        self.status = 200
        self.transport_error = None
        self.factory_patch = patch("vicekrack.openai_provider.OpenAI", side_effect=self.factory).start()

    def factory(self, **options):
        self.options.append(options)
        def respond(request):
            self.requests.append(request)
            if self.transport_error:
                raise self.transport_error("sensitive diagnostic", request=request)
            return httpx2.Response(self.status, json=self.body)
        client = OpenAI(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))
        self.clients.append(client)
        return client

    def research(self):
        return OpenAIResearchProvider().research(
            instructions="Summarize supplied notes.", notes=["A supplied note."], model="gpt-4.1-mini")

    def assert_error(self, code):
        with self.assertRaises(NetworkError) as raised:
            self.research()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("sensitive", raised.exception.message)
        self.assertNotIn("unit-test-placeholder", raised.exception.message)

    def test_success_and_real_sdk_request_shape(self):
        result = self.research()
        self.assertEqual(result["data"]["provider"], "openai")
        self.assertEqual(result["summary"], "Provider separation keeps routing reusable.")
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
        self.assertEqual(request.method, "POST")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "gpt-4.1-mini")
        self.assertEqual(json.loads(body["input"])["design_notes"], ["A supplied note."])
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(body["store"])
        self.assertEqual(body["max_output_tokens"], 1200)
        self.assertNotIn("tools", body)
        self.assertEqual(self.options[0]["timeout"], 30)
        self.assertEqual(self.options[0]["max_retries"], 0)
        self.assertTrue(self.clients[0].is_closed())

    def test_full_orchestration_uses_selected_registry(self):
        runner = Orchestrator(registry_path="config/agents.openai.json")
        task = read_json(ROOT / "examples/research-task.json")
        original = deepcopy(task)
        result = runner.run(task)
        runner.validate(result)
        self.assertEqual(task, original)
        self.assertEqual(result["status"], "completed")
        child = result["result"]["data"]["delegated_task"]
        self.assertEqual(child["recipient"], "researcher")
        self.assertEqual(child["parent_task_id"], task["task_id"])
        self.assertEqual(child["result"]["data"]["provider"], "openai")
        self.assertEqual(len(self.requests), 1)

    def test_cli_openai_configuration(self):
        with patch("sys.argv", ["vicekrack", "examples/research-task.json", "--registry", "config/agents.openai.json"]):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "completed")
        self.assertNotIn("unit-test-placeholder", output.getvalue())

    def test_default_remains_mock_even_with_credentials(self):
        result = Orchestrator().run(read_json(ROOT / "examples/research-task.json"))
        self.assertEqual(result["status"], "completed")
        self.factory_patch.assert_not_called()

    def test_missing_credentials_before_client_creation(self):
        for key in ("", "   "):
            with self.subTest(key=key), patch.dict(os.environ, {"OPENAI_API_KEY": key}):
                self.assert_error("missing_credentials")
        self.factory_patch.assert_not_called()

    def test_missing_model(self):
        with self.assertRaises(NetworkError) as error:
            OpenAIResearchProvider().research(instructions="Research", notes=["Note"], model=None)
        self.assertEqual(error.exception.code, "missing_model")
        self.factory_patch.assert_not_called()

    def test_timeout_setting(self):
        with patch.dict(os.environ, {"OPENAI_TIMEOUT_SECONDS": "2.5"}):
            self.research()
        self.assertEqual(self.options[0]["timeout"], 2.5)

    def test_bad_timeout_settings(self):
        for value in ("", "abc", "0", "-1", "nan", "inf", "301"):
            with self.subTest(value=value), patch.dict(os.environ, {"OPENAI_TIMEOUT_SECONDS": value}):
                self.assert_error("invalid_provider_configuration")
        self.factory_patch.assert_not_called()

    def test_transport_timeout_no_retry(self):
        self.transport_error = httpx2.ReadTimeout
        self.assert_error("provider_timeout")
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.clients[0].is_closed())

    def test_connection_error_no_retry(self):
        self.transport_error = httpx2.ConnectError
        self.assert_error("provider_connection_error")
        self.assertEqual(len(self.requests), 1)

    def test_api_errors_redacted_and_not_retried(self):
        for status, code in ((401, "provider_authentication_error"), (403, "provider_permission_error"),
                             (429, "provider_rate_limit"), (400, "provider_api_error"),
                             (404, "provider_api_error"), (500, "provider_api_error")):
            with self.subTest(status=status):
                self.requests.clear()
                self.status, self.body = status, {"error": {"message": "sensitive diagnostic"}}
                self.assert_error(code)
                self.assertEqual(len(self.requests), 1)

    def test_invalid_response_content(self):
        for text in ("not JSON", "", "null", "[]", "{}", '{"summary":42}',
                     '{"summary":" "}', '{"summary":"ok","extra":true}'):
            with self.subTest(text=text):
                self.body = response_body(text)
                self.assert_error("invalid_provider_response")

    def test_invalid_response_envelopes(self):
        for body in ({}, response_body(output=[]), response_body(output=None),
                     response_body(status="failed"), response_body(status="queued")):
            with self.subTest(body=body):
                self.body = body
                self.assert_error("invalid_provider_response")

    def test_incomplete_response(self):
        self.body = response_body(status="incomplete")
        self.assert_error("incomplete_provider_response")

    def test_refusal(self):
        self.body["output"][0]["content"] = [{"type": "refusal", "refusal": "sensitive diagnostic"}]
        self.assert_error("provider_refusal")

    def test_parent_gets_valid_failure(self):
        self.transport_error = httpx2.ReadTimeout
        runner = Orchestrator(registry_path="config/agents.openai.json")
        result = runner.run(read_json(ROOT / "examples/research-task.json"))
        runner.validate(result)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "provider_timeout")
        self.assertNotIn("sensitive", json.dumps(result))

    def test_unsupported_provider(self):
        runner = Orchestrator()
        runner.agents["researcher"]["execution"]["adapter"] = "unsupported"
        result = runner.run(read_json(ROOT / "examples/research-task.json"))
        self.assertEqual(result["error"]["code"], "adapter_unavailable")
        self.factory_patch.assert_not_called()

    def test_invalid_task_never_calls_provider(self):
        runner = Orchestrator(registry_path="config/agents.openai.json")
        with self.assertRaises(NetworkError):
            runner.run({})
        self.factory_patch.assert_not_called()

    def test_invalid_registry_path(self):
        with self.assertRaises(NetworkError) as error:
            Orchestrator(registry_path="../outside.json")
        self.assertEqual(error.exception.code, "invalid_configuration")
        self.factory_patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
