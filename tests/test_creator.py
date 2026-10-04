"""Step 15: Story Brief contract, Creator stage, provider drafting and CLI. No network."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import httpx2
from anthropic import Anthropic
from openai import OpenAI

from vicekrack.__main__ import main
from vicekrack.creator import (
    AI_DISCLOSURE, CREATOR_INSTRUCTIONS, DRAFT_SCHEMA, MockScriptDrafter, build_request,
    draft_short_script, drafter_for, load_creator_config,
)
from vicekrack.creator_cli import save_script
from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.scene_plan import build_scene_plan, validate_scene_plan
from vicekrack.short_script import validate_short_script
from vicekrack.story_brief import validate_story_brief

STAMP = "2026-10-04T12:00:00Z"


def brief(name="cooking"):
    return read_json(ROOT / f"examples/story-brief-{name}.json")


def mock_draft(source):
    return MockScriptDrafter().draft(request=build_request(source), model=None)


class FixedDrafter:
    def __init__(self, output):
        self.output = output
        self.requests = []

    def draft(self, *, request, model):
        self.requests.append((deepcopy(request), model))
        if isinstance(self.output, Exception):
            raise self.output
        return deepcopy(self.output)


class NetworkGuard(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        # A regression can never silently turn these tests into live requests.
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()


class StoryBriefTests(NetworkGuard):
    def assert_invalid(self, value, code="invalid_story_brief"):
        with self.assertRaises(NetworkError) as raised:
            validate_story_brief(value)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_examples_are_valid_and_gated(self):
        for name in ("gta", "cooking"):
            validate_story_brief(brief(name))
        validate_story_brief(brief("cooking"), require_verified_claims=True)
        with self.assertRaises(NetworkError) as raised:
            validate_story_brief(brief("gta"), require_verified_claims=True)
        self.assertEqual(raised.exception.code, "unverified_claims")

    def test_structural_and_reference_rules(self):
        base = brief("gta")
        cases = []
        value = deepcopy(base); value["claims"][1]["claim_id"] = "c1"; cases.append(value)
        value = deepcopy(base); value["claims"][0]["source_ids"] = ["missing"]; cases.append(value)
        value = deepcopy(base); value["claims"][0].update(status="verified", source_ids=[]); cases.append(value)
        value = deepcopy(base); value["sources"][1]["source_id"] = "rockstar-trailer-1"; cases.append(value)
        value = deepcopy(base); value["sources"][0]["url"] = "http://example.com/x"; cases.append(value)
        value = deepcopy(base); value["sources"][0]["url"] = "https://user@example.com/x"; cases.append(value)
        value = deepcopy(base); value["provenance"]["created_at"] = "2026-02-30T12:00:00Z"; cases.append(value)
        value = deepcopy(base); value["claims"] = [dict(base["claims"][0], claim_id=f"c{i}") for i in range(9)]; cases.append(value)
        value = deepcopy(base); value["extra"] = True; cases.append(value)
        value = deepcopy(base); value["format"] = "vertical_short_60s"; cases.append(value)
        value = deepcopy(base); value["constraints"]["tone"] = "  "; cases.append(value)
        value = deepcopy(base); value["constraints"]["disclosures"] = ["x"] * 5; cases.append(value)
        value = deepcopy(base); value["brief_id"] = "Upper Case"; cases.append(value)
        value = deepcopy(base); value["topic"] = "x" * 201; cases.append(value)
        cases += [None, [], {}, {**base, "topic": float("nan")}]
        for case in cases:
            with self.subTest(case=str(case)[:60]):
                error = self.assert_invalid(case)
                self.assertNotIn("Vice City", error.message)

    def test_credentials_rejected_before_diagnostics(self):
        value = brief()
        value["constraints"]["api_key"] = "synthetic"
        self.assert_invalid(value, "sensitive_state")
        value = brief()
        value["topic"] = "sk-" + "a" * 30
        self.assert_invalid(value, "sensitive_state")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-active-value"}):
            value = brief()
            value["angle"] = "contains synthetic-active-value"
            self.assert_invalid(value, "sensitive_state")


class CreatorTests(NetworkGuard):
    def test_mock_draft_is_valid_and_keeps_brief_facts(self):
        source = brief("gta")
        source["parent_task_id"] = "parent-001"
        before = deepcopy(source)
        script = draft_short_script(source, created_at=STAMP)
        validate_short_script(script)
        self.assertEqual(source, before)
        self.assertEqual(script["claims"], source["claims"])
        self.assertEqual(script["sources"], source["sources"])
        self.assertEqual({claim["status"] for claim in script["claims"]}, {"unverified"})
        self.assertEqual([(b["beat"], b["start_seconds"], b["end_seconds"]) for b in script["beats"]],
                         [("hook", 0, 3), ("context", 3, 7), ("key_info", 7, 12), ("payoff", 12, 15)])
        self.assertEqual(script["parent_task_id"], "parent-001")
        self.assertEqual(script["angle"], source["angle"])
        self.assertEqual(script["disclosures"], source["constraints"]["disclosures"])
        self.assertEqual(script["provenance"], {"created_by": "creator", "provider": "mock",
                                                "model": None, "created_at": STAMP})
        for beat in script["beats"]:
            self.assertEqual(beat["visual"]["avoid"], source["constraints"]["avoid"])
        self.assertTrue(script["script_id"].startswith(source["brief_id"] + "-"))
        self.assertEqual(script, draft_short_script(source, created_at=STAMP))

    def test_mock_rejects_model(self):
        with self.assertRaises(NetworkError) as raised:
            draft_short_script(brief(), model="some-model")
        self.assertEqual(raised.exception.code, "unsupported_model")

    def test_end_to_end_with_existing_scene_planner(self):
        plan = build_scene_plan(draft_short_script(brief("cooking"), created_at=STAMP))
        validate_scene_plan(plan)
        self.assertFalse(plan["blocked_for_production"])
        gta = draft_short_script(brief("gta"), created_at=STAMP)
        with self.assertRaises(NetworkError) as raised:
            build_scene_plan(gta)
        self.assertEqual(raised.exception.code, "unverified_claims")
        draft_plan = build_scene_plan(gta, draft=True)
        self.assertTrue(draft_plan["blocked_for_production"])
        self.assertEqual(draft_plan["unverified_claim_ids"], ["c1", "c2", "c3"])

    def test_request_contains_only_writer_inputs(self):
        source = brief("cooking")
        request = build_request(source)
        self.assertNotIn("url", json.dumps(request))
        self.assertNotIn("provenance", request)
        self.assertEqual([w["max_words"] for w in request["beat_windows"]], [10, 14, 17, 10])
        self.assertEqual([c["claim_id"] for c in request["claims"]], ["c1", "c2"])

    def assert_rejected(self, draft, code="invalid_creator_output", source=None):
        with self.assertRaises(NetworkError) as raised:
            draft_short_script(source or brief(), drafter=FixedDrafter(draft), created_at=STAMP)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("Parboiling", raised.exception.message)
        return raised.exception

    def test_drafter_output_breaking_contract_is_rejected(self):
        good = mock_draft(brief())
        mutations = {
            "reversed": lambda d: d["beats"].reverse(),
            "three_beats": lambda d: d["beats"].pop(),
            "five_beats": lambda d: d["beats"].append(deepcopy(d["beats"][0])),
            "unknown_claim": lambda d: d["beats"][2]["claim_ids"].append("c99"),
            "duplicate_claim": lambda d: d["beats"][2]["claim_ids"].append("c1"),
            "unused_claim": lambda d: d["beats"][2].update(claim_ids=["c1"]),
            "no_key_claim": lambda d: (d["beats"][0].update(claim_ids=["c1", "c2"]), d["beats"][2].update(claim_ids=[])),
            "long_narration": lambda d: d["beats"][0].update(narration="word " * 11),
            "blank_title": lambda d: d.update(title=" "),
            "remote_final_fallback": lambda d: d["beats"][0]["visual"].update(fallback_methods=["sourced_media"]),
            "generative_without_prompt": lambda d: d["beats"][0]["visual"].update(preferred_method="ai_video_clip"),
            "sourced_without_source": lambda d: d["beats"][0]["visual"].update(preferred_method="sourced_media"),
            "unknown_visual_source": lambda d: d["beats"][0]["visual"].update(source_ids=["nowhere"]),
            "injected_claims": lambda d: d.update(claims=[{"claim_id": "c1", "status": "verified"}]),
            "injected_timing": lambda d: d["beats"][0].update(start_seconds=1),
            "unknown_method": lambda d: d["beats"][0]["visual"].update(preferred_method="hologram"),
            "blank_on_screen": lambda d: d["beats"][0].update(on_screen_text=" "),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                draft = deepcopy(good)
                mutate(draft)
                self.assert_rejected(draft)
        for value in (None, [], "text", {"title": "x", "music_mood": None, "beats": float("nan")}):
            with self.subTest(value=value):
                self.assert_rejected(value)

    def test_drafter_errors(self):
        error = self.assert_rejected(RuntimeError("sensitive diagnostic"), "creator_failed")
        self.assertNotIn("sensitive", error.message)
        self.assert_rejected(NetworkError("provider_timeout", "Timed out."), "provider_timeout")

    def test_invalid_brief_never_reaches_drafter(self):
        drafter = FixedDrafter(mock_draft(brief()))
        value = brief()
        value["claims"][0]["source_ids"] = ["missing"]
        with self.assertRaises(NetworkError):
            draft_short_script(value, drafter=drafter)
        self.assertEqual(drafter.requests, [])

    def test_generative_visual_adds_ai_disclosure_once(self):
        draft = mock_draft(brief())
        draft["beats"][0]["visual"].update(preferred_method="generated_image",
                                           generation_prompt="Original golden roast potatoes, vertical")
        script = draft_short_script(brief(), drafter=FixedDrafter(draft), created_at=STAMP)
        self.assertEqual(script["disclosures"], [AI_DISCLOSURE])
        source = brief()
        source["constraints"]["disclosures"] = ["Includes AI-generated imagery."]
        script = draft_short_script(source, drafter=FixedDrafter(draft), created_at=STAMP)
        self.assertEqual(script["disclosures"], ["Includes AI-generated imagery."])

    def test_avoid_constraints_always_applied(self):
        source = brief("gta")
        draft = mock_draft(source)
        draft["beats"][0]["visual"]["avoid"] = ["neon signs"]
        script = draft_short_script(source, drafter=FixedDrafter(draft), created_at=STAMP)
        self.assertEqual(script["beats"][0]["visual"]["avoid"], source["constraints"]["avoid"] + ["neon signs"])
        self.assertEqual(script["beats"][1]["visual"]["avoid"], source["constraints"]["avoid"])

    def test_adapter_selection(self):
        self.assertIsInstance(drafter_for("mock"), MockScriptDrafter)
        for adapter in ("unknown", None):
            with self.assertRaises(NetworkError) as raised:
                drafter_for(adapter)
            self.assertEqual(raised.exception.code, "adapter_unavailable")
        with self.assertRaises(NetworkError) as raised:
            drafter_for("openai", providers={"openai": object()})
        self.assertEqual(raised.exception.code, "adapter_unavailable")
        with self.assertRaises(NetworkError):
            draft_short_script(brief(), adapter="unknown")


class ProviderDraftingTests(NetworkGuard):
    """Both real SDKs through in-memory transports; no credits or network required."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-placeholder", "ANTHROPIC_API_KEY": "unit-test-placeholder",
                                "ANTHROPIC_CONFIG_DIR": directory.name}, clear=True).start()
        self.requests, self.options, self.clients = [], [], []
        self.status = 200
        self.source = brief()
        self.text = json.dumps(mock_draft(self.source))
        self.openai = patch("vicekrack.openai_provider.OpenAI", side_effect=self.factory(OpenAI)).start()
        self.anthropic = patch("vicekrack.anthropic_provider.Anthropic", side_effect=self.factory(Anthropic)).start()

    def factory(self, sdk):
        def build(**options):
            self.options.append(options)

            def respond(request):
                self.requests.append(request)
                if request.url.host == "api.openai.com":
                    body = {"id": "resp_test", "object": "response", "status": "completed", "model": "m",
                            "output": [{"type": "message", "content": [{"type": "output_text", "text": self.text}]}]}
                else:
                    body = {"id": "msg_test", "type": "message", "role": "assistant", "model": "m",
                            "stop_reason": "end_turn", "content": [{"type": "text", "text": self.text}],
                            "usage": {"input_tokens": 1, "output_tokens": 1}}
                if self.status != 200:
                    body = {"error": {"message": "sensitive diagnostic"}}
                return httpx2.Response(self.status, json=body)
            client = sdk(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))
            self.clients.append(client)
            return client
        return build

    def draft(self, adapter):
        model = "gpt-4.1-mini" if adapter == "openai" else "claude-sonnet-4-6"
        return draft_short_script(self.source, adapter=adapter, model=model, created_at=STAMP)

    def test_openai_request_shape_and_result(self):
        script = self.draft("openai")
        self.assertEqual(script["provenance"]["provider"], "openai")
        self.assertEqual(script["provenance"]["model"], "gpt-4.1-mini")
        self.assertEqual(script["claims"], self.source["claims"])
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
        body = json.loads(request.content)
        self.assertEqual(body["instructions"], CREATOR_INSTRUCTIONS)
        self.assertEqual(json.loads(body["input"]), build_request(self.source))
        self.assertEqual(body["text"]["format"], {"type": "json_schema", "name": "short_script_draft",
                                                  "strict": True, "schema": DRAFT_SCHEMA})
        self.assertFalse(body["store"])
        self.assertEqual(body["max_output_tokens"], 2000)
        self.assertNotIn("tools", body)
        self.assertEqual(self.options[0]["max_retries"], 0)
        self.assertTrue(self.clients[0].is_closed())
        self.assertEqual(len(self.requests), 1)

    def test_anthropic_request_shape_and_result(self):
        script = self.draft("anthropic")
        self.assertEqual(script["provenance"]["provider"], "anthropic")
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.anthropic.com/v1/messages")
        body = json.loads(request.content)
        self.assertEqual(body["system"], CREATOR_INSTRUCTIONS)
        self.assertEqual(json.loads(body["messages"][0]["content"]), build_request(self.source))
        self.assertEqual(body["output_config"]["format"], {"type": "json_schema", "schema": DRAFT_SCHEMA})
        self.assertEqual(body["max_tokens"], 2000)
        self.assertNotIn("tools", body)
        self.assertEqual(self.options[0]["max_retries"], 0)
        self.assertTrue(self.clients[0].is_closed())

    def test_schema_and_semantic_rejections(self):
        for adapter in ("openai", "anthropic"):
            with self.subTest(adapter=adapter):
                tampered = mock_draft(self.source)
                tampered["claims"] = []
                self.text = json.dumps(tampered)
                with self.assertRaises(NetworkError) as raised:
                    self.draft(adapter)
                self.assertEqual(raised.exception.code, "invalid_provider_response")
                too_long = mock_draft(self.source)
                too_long["beats"][0]["narration"] = "word " * 30
                self.text = json.dumps(too_long)
                with self.assertRaises(NetworkError) as raised:
                    self.draft(adapter)
                self.assertEqual(raised.exception.code, "invalid_creator_output")
                self.text = "not json"
                with self.assertRaises(NetworkError) as raised:
                    self.draft(adapter)
                self.assertEqual(raised.exception.code, "invalid_provider_response")

    def test_errors_are_sanitized_and_not_retried(self):
        for adapter in ("openai", "anthropic"):
            for status, code in ((401, "provider_authentication_error"), (429, "provider_rate_limit"),
                                 (500, "provider_api_error")):
                with self.subTest(adapter=adapter, status=status):
                    self.requests.clear()
                    self.status = status
                    with self.assertRaises(NetworkError) as raised:
                        self.draft(adapter)
                    self.assertEqual(raised.exception.code, code)
                    self.assertNotIn("sensitive", raised.exception.message)
                    self.assertNotIn("unit-test-placeholder", raised.exception.message)
                    self.assertEqual(len(self.requests), 1)

    def test_missing_credentials_or_model_before_client(self):
        for adapter, name in (("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
            with self.subTest(adapter=adapter), patch.dict(os.environ, {name: " "}):
                with self.assertRaises(NetworkError) as raised:
                    self.draft(adapter)
                self.assertEqual(raised.exception.code, "missing_credentials")
            with self.assertRaises(NetworkError) as raised:
                draft_short_script(self.source, adapter=adapter, model=None)
            self.assertEqual(raised.exception.code, "missing_model")
        self.openai.assert_not_called()
        self.anthropic.assert_not_called()


class CreatorCliTests(NetworkGuard):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def command(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["vicekrack", *args]), patch("vicekrack.creator_cli.ROOT", self.folder), \
             redirect_stdout(output):
            code = main()
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_validate_brief(self):
        code, result, _ = self.command("validate-brief", str(ROOT / "examples/story-brief-gta.json"))
        self.assertEqual(code, 0)
        self.assertEqual(result["claims_unverified"], 3)
        self.assertFalse(result["externally_fact_checked"])
        code, result, _ = self.command("validate-brief", str(ROOT / "examples/story-brief-gta.json"), "--require-verified")
        self.assertEqual((code, result["error"]["code"]), (1, "unverified_claims"))
        code, result, _ = self.command("validate-brief", str(self.folder / "absent.json"))
        self.assertEqual((code, result["error"]["code"]), (1, "invalid_input_or_storage"))

    def test_draft_short_saves_valid_script_without_echoing_content(self):
        for name, gate, hint in (("cooking", "declared_verified", "plan-short SCRIPT_FILE"),
                                 ("gta", "blocked_unverified_claims", "plan-short SCRIPT_FILE --draft")):
            with self.subTest(name=name):
                code, result, raw = self.command("draft-short", str(ROOT / f"examples/story-brief-{name}.json"))
                self.assertEqual(code, 0)
                self.assertEqual((result["production_gate"], result["next_command"]), (gate, hint))
                self.assertEqual((result["provider"], result["assets_produced"], result["published"]), ("mock", False, False))
                path = Path(result["script_file"])
                self.assertEqual(path.parent, self.folder / "runtime/scripts")
                script = read_json(path)
                validate_short_script(script)
                self.assertEqual(script["script_id"], result["script_id"])
                self.assertNotIn(brief(name)["topic"], raw)
        self.assertEqual(list((self.folder / "runtime/scripts").glob("*.tmp")), [])

    def test_save_never_overwrites(self):
        script = draft_short_script(brief(), created_at=STAMP)
        with patch("vicekrack.creator_cli.uuid4") as fixed:
            fixed.return_value.hex = "fixed"
            first = save_script(script, self.folder)
            before = first.read_bytes()
            with self.assertRaises(NetworkError) as raised:
                save_script(script, self.folder)
        self.assertEqual(raised.exception.code, "script_write_failed")
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_real_provider_config_without_key_fails_before_client(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("vicekrack.openai_provider.OpenAI", side_effect=AssertionError("No client")) as client:
            code, result, _ = self.command("draft-short", str(ROOT / "examples/story-brief-gta.json"),
                                           "--config", "config/creator.openai.json")
        self.assertEqual((code, result["error"]["code"]), (1, "missing_credentials"))
        client.assert_not_called()
        self.assertFalse((self.folder / "runtime/scripts").exists())

    def test_shipped_configs(self):
        expected = {"config/creator.json": ("mock", None),
                    "config/creator.openai.json": ("openai", "gpt-4.1-mini"),
                    "config/creator.anthropic.json": ("anthropic", "claude-sonnet-4-6")}
        for path, (adapter, model) in expected.items():
            execution = load_creator_config(path)["execution"]
            self.assertEqual((execution["adapter"], execution["model"]), (adapter, model))

    def test_invalid_configs(self):
        (self.folder / "agents").mkdir()
        (self.folder / "agents/creator.md").write_text("# Creator\n")
        good = {"config_version": "1.0", "agent": "creator", "definition": "agents/creator.md",
                "execution": {"adapter": "mock", "model": None}}
        variants = [
            {**good, "execution": {"adapter": "mock", "model": "x"}},
            {**good, "execution": {"adapter": "openai", "model": None}},
            {**good, "execution": {"adapter": "openai", "model": "  "}},
            {**good, "execution": {"adapter": "local-llm", "model": "x"}},
            {**good, "execution": {"adapter": "mock", "model": None, "api_key": "x"}},
            {**good, "definition": "../outside.md"},
            {**good, "definition": "agents/missing.md"},
            {**good, "agent": "researcher"},
            {**good, "extra": True},
            [],
        ]
        (self.folder / "good.json").write_text(json.dumps(good))
        self.assertEqual(load_creator_config("good.json", root=self.folder), good)
        for index, variant in enumerate(variants):
            with self.subTest(index=index):
                (self.folder / f"bad{index}.json").write_text(json.dumps(variant))
                with self.assertRaises(NetworkError) as raised:
                    load_creator_config(f"bad{index}.json", root=self.folder)
                self.assertEqual(raised.exception.code, "invalid_configuration")
        code, result, _ = self.command("draft-short", str(ROOT / "examples/story-brief-gta.json"), "--config", "../x.json")
        self.assertEqual((code, result["error"]["code"]), (1, "invalid_configuration"))


if __name__ == "__main__":
    unittest.main()
