"""The same task uses either real SDK through configuration, with offline transports."""

import io
import json
import os
import unittest
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from unittest.mock import patch

import httpx2
from anthropic import Anthropic
from openai import OpenAI

from vicekrack import Orchestrator
from vicekrack.__main__ import main
from vicekrack.orchestrator import ROOT, read_json


class ProviderSelectionTests(unittest.TestCase):
    def test_configuration_alone_switches_provider(self):
        requests = []

        def respond(request):
            requests.append(request)
            text = '{"summary":"Summary from supplied notes."}'
            if request.url.host == "api.openai.com":
                body = {"id": "resp_test", "object": "response", "status": "completed",
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
            else:
                self.assertEqual(request.url.host, "api.anthropic.com")
                body = {"id": "msg_test", "type": "message", "role": "assistant",
                        "stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}
            return httpx2.Response(200, json=body)

        def factory(sdk):
            return lambda **options: sdk(**options, http_client=httpx2.Client(
                transport=httpx2.MockTransport(respond)))

        task = read_json(ROOT / "examples/research-task.json")
        original = deepcopy(task)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "fake-openai", "ANTHROPIC_API_KEY": "fake-anthropic",
                                     "ANTHROPIC_CONFIG_DIR": directory}, clear=True), \
             patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")), \
             patch("vicekrack.openai_provider.OpenAI", side_effect=factory(OpenAI)), \
             patch("vicekrack.anthropic_provider.Anthropic", side_effect=factory(Anthropic)):
            for provider in ("openai", "anthropic"):
                runner = Orchestrator(registry_path=f"config/agents.{provider}.json")
                result = runner.run(task)
                runner.validate(result)
                self.assertEqual(result["status"], "completed")
                child = result["result"]["data"]["delegated_task"]
                self.assertEqual(child["result"]["data"]["provider"], provider)
                self.assertEqual(child["recipient"], "researcher")
                self.assertEqual(task, original)
        self.assertEqual(len(requests), 2)
        first, second = [json.loads(request.content) for request in requests]
        self.assertEqual(json.loads(first["input"]), json.loads(second["messages"][0]["content"]))

    def test_cli_missing_keys_for_both_providers(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider), patch.dict(os.environ, {}, clear=True), \
                 patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")), \
                 patch("sys.argv", ["vicekrack", "examples/research-task.json", "--registry", f"config/agents.{provider}.json"]):
                output = io.StringIO()
                errors = io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(main(), 1)
                result = json.loads(output.getvalue())
                self.assertEqual(result["error"]["code"], "missing_credentials")
                self.assertEqual(errors.getvalue(), "")

    def test_env_template_contains_only_blank_credentials(self):
        self.assertEqual((ROOT / ".env.example").read_text().splitlines(),
                         ["OPENAI_API_KEY=", "ANTHROPIC_API_KEY="])
