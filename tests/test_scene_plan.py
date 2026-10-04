import io
import json
import os
import socket
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout
from vicekrack.__main__ import main
from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.scene_plan import build_scene_plan, validate_scene_plan, DEFAULT_CAPABILITIES
from vicekrack.scene_cli import save_plan
import test_short_script


class ScenePlanTests(unittest.TestCase):
    def setUp(self):
        self.script = read_json(ROOT / "examples/short-script-cooking.json")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def command(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["vicekrack", *args]), patch("vicekrack.scene_cli.ROOT", self.folder), redirect_stdout(output):
            # CLI's explicit default file must remain the real registry while output uses temp.
            if args[0] == "plan-short" and "--capabilities" not in args:
                with patch("sys.argv", ["vicekrack", *args, "--capabilities", str(ROOT / "config/visual-capabilities.json")]):
                    code = main()
            else:
                code = main()
        return code, json.loads(output.getvalue())

    def test_deterministic_immutable_complete_plan(self):
        self.script["parent_task_id"] = "parent-001"
        before = deepcopy(self.script)
        first = build_scene_plan(self.script)
        self.assertEqual(first, build_scene_plan(self.script))
        self.assertEqual(self.script, before)
        self.assertEqual(first["script"], self.script)
        self.assertEqual(first["parent_task_id"], "parent-001")
        self.assertEqual(first["frame"], {"width":1080,"height":1920,"aspect_ratio":"9:16"})
        self.assertEqual([s["beat"] for s in first["scenes"]], self.script["beats"])
        self.assertFalse(first["blocked_for_production"])
        self.assertFalse(first["assets_produced"])
        validate_scene_plan(first)

    def test_default_config_and_fallback_decisions(self):
        self.assertEqual(read_json(ROOT / "config/visual-capabilities.json"), DEFAULT_CAPABILITIES)
        plan = build_scene_plan(self.script)
        first = plan["scenes"][0]
        self.assertEqual(first["selected_method"], "text_card")
        self.assertEqual(first["considered_methods"], [
            {"method":"generated_image","status":"skipped","reason":"not_configured_available"},
            {"method":"text_card","status":"selected","reason":"configured_available"}])
        self.assertEqual(plan["scenes"][1]["selected_method"], "motion_graphics")

    def test_mixed_capabilities_preserve_declared_priority(self):
        config = {"available_methods":["text_card","animated_image","generated_image"]}
        plan = build_scene_plan(self.script, config)
        self.assertEqual(plan["scenes"][0]["selected_method"], "generated_image")
        self.assertEqual(plan["scenes"][2]["selected_method"], "animated_image")
        self.assertEqual(plan, build_scene_plan(self.script, {"available_methods":list(reversed(config["available_methods"]))}))
        self.assertFalse(any(s["assets_produced"] for s in plan["scenes"]))

    def test_no_available_method_is_clear_error(self):
        with self.assertRaises(NetworkError) as error:
            build_scene_plan(self.script, {"available_methods":[]})
        self.assertEqual(error.exception.code, "no_visual_method")

    def test_capability_configuration_is_strict(self):
        for config in ({}, [], {"available_methods":"text_card"}, {"available_methods":[True]},
                       {"available_methods":["text_card","text_card"]},
                       {"available_methods":["browser"]}, {"available_methods":[],"extra":True}):
            with self.subTest(config=config), self.assertRaises(NetworkError): build_scene_plan(self.script, config)

    def test_gta_gate_and_explicit_draft(self):
        script = read_json(ROOT / "examples/short-script-gta.json")
        before = deepcopy(script)
        with self.assertRaises(NetworkError) as error: build_scene_plan(script)
        self.assertEqual(error.exception.code, "unverified_claims")
        draft = build_scene_plan(script, draft=True)
        self.assertTrue(draft["blocked_for_production"])
        self.assertEqual(draft["unverified_claim_ids"], [c["claim_id"] for c in script["claims"] if c["status"] != "verified"])
        self.assertEqual(script, before)
        validate_scene_plan(draft)

    def test_verified_draft_still_blocked(self):
        plan = build_scene_plan(self.script, draft=True)
        self.assertTrue(plan["blocked_for_production"])
        self.assertEqual(plan["unverified_claim_ids"], [])
        with self.assertRaises(NetworkError): build_scene_plan(self.script, draft="yes")

    def test_input_changes_change_hash(self):
        plan = build_scene_plan(self.script)
        self.script["title"] = "Changed title"
        self.assertNotEqual(plan["input_sha256"], build_scene_plan(self.script)["input_sha256"])

    def test_schema_and_semantic_tampering_rejected(self):
        for change in ("hash","method","timing","mode","extra"):
            plan = build_scene_plan(self.script)
            if change == "hash": plan["input_sha256"] = "0"*64
            if change == "method": plan["scenes"][0]["selected_method"] = "motion_graphics"
            if change == "timing": plan["scenes"][0]["beat"]["end_seconds"] = 4
            if change == "mode": plan["blocked_for_production"] = True
            if change == "extra": plan["extra"] = True
            with self.subTest(change=change), self.assertRaises(NetworkError): validate_scene_plan(plan)

    def test_saved_plan_no_overwrite_or_partial_publication(self):
        plan = build_scene_plan(self.script)
        with patch("vicekrack.scene_cli.uuid4", return_value=SimpleNamespace(hex="fixed")):
            path = save_plan(plan,self.folder)
            before=path.read_bytes()
            with self.assertRaises(NetworkError): save_plan(plan,self.folder)
            self.assertEqual(path.read_bytes(),before)
        with patch("vicekrack.scene_cli.os.link", side_effect=OSError("private failure")), self.assertRaises(NetworkError):
            save_plan(plan,self.folder)
        self.assertEqual(list(self.folder.glob("*.tmp")),[])
        validate_scene_plan(read_json(path))

    def test_cli_draft_and_production_gate(self):
        fixture = str(ROOT / "examples/short-script-gta.json")
        self.assertEqual(self.command("validate-short-script",fixture)[0],0)
        self.assertEqual(self.command("validate-short-script",fixture,"--require-verified")[1]["error"]["code"],"unverified_claims")
        self.assertEqual(self.command("plan-short",fixture)[0],1)
        self.assertFalse((self.folder / "runtime/plans").exists())
        code,result=self.command("plan-short",fixture,"--draft")
        self.assertEqual(code,0)
        self.assertTrue(result["blocked_for_production"])
        self.assertNotIn("narration",result)
        validate_scene_plan(read_json(Path(result["plan_file"])))

    def test_no_provider_or_network_calls(self):
        with patch("vicekrack.openai_provider.OpenAI",side_effect=AssertionError("No client")), patch("vicekrack.anthropic_provider.Anthropic",side_effect=AssertionError("No client")), patch("socket.create_connection",side_effect=AssertionError("No network")):
            validate_scene_plan(build_scene_plan(self.script))

    def test_corrupt_missing_and_sensitive_inputs(self):
        path=self.folder / "bad.json"
        path.write_text("{private content",encoding="utf-8")
        code,result=self.command("plan-short",str(path))
        self.assertEqual(code,1)
        self.assertNotIn("private content",json.dumps(result))
        self.assertEqual(self.command("validate-short-script",str(self.folder / "missing.json"))[0],1)
        with patch.dict(os.environ,{"OPENAI_API_KEY":"synthetic-private-value"}):
            self.script["title"]="synthetic-private-value"
            with self.assertRaises(NetworkError) as error: build_scene_plan(self.script)
            self.assertEqual(error.exception.code,"sensitive_state")
            self.assertNotIn("synthetic-private-value",str(error.exception))

    def test_short_script_cleanup_preserves_other_started_guards(self):
        guard=patch.object(socket,"create_connection",side_effect=OSError("Network blocked"))
        mocked=guard.start()
        try:
            suite=unittest.TestSuite([test_short_script.ShortScriptTests("test_gta_example_is_valid")])
            result=unittest.TextTestRunner(stream=io.StringIO()).run(suite)
            self.assertTrue(result.wasSuccessful())
            self.assertIs(socket.create_connection,mocked)
        finally:
            guard.stop()
