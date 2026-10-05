"""Step 21: controlled production pipeline. Mocked rendering and Creator; no network.

The optional real-encoder integration test runs only with RUN_LOCAL_RENDER_TESTS=1.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from vicekrack import production
from vicekrack.__main__ import main
from vicekrack.creator import MockScriptDrafter
from vicekrack.errors import NetworkError
from vicekrack.narration import canonical_wav
from vicekrack.orchestrator import read_json
from vicekrack.production import Pipeline, ProductionStore, inspect_production, list_productions
from vicekrack.scout_cli import scout_once
from vicekrack.selection import load_profile
from vicekrack.selection_cli import load_history, select_stories
from vicekrack.verification_cli import verify_stored

SCOUTED, VERIFIED, SELECTED, NOW = ("2026-10-04T12:00:00Z", "2026-10-04T12:30:00Z", "2026-10-04T13:00:00Z",
                                    "2026-10-04T13:10:00Z")
PAID = {"creator": "config/creator.anthropic.json"}


class FakeRenderer:
    """Stands in for the Step 13/14 renderer: writes a tiny package under the given folder."""

    def __init__(self, failures=()):
        self.failures, self.calls = list(failures), []

    def __call__(self, plan, *, allow_draft, directory, narration):
        self.calls.append({"plan_id": plan["plan_id"], "allow_draft": allow_draft, "narration": narration})
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        folder = Path(directory) / f"{plan['plan_id']}-{len(self.calls)}"
        folder.mkdir(parents=True)
        (folder / "preview.mp4").write_bytes(b"fake mp4 " + plan["plan_id"].encode())
        manifest = {"plan_id": plan["plan_id"], "publishable": False, "preview_only": True,
                    "audio_present": narration is not None}
        (folder / "manifest.json").write_text(json.dumps(manifest))
        return {"preview_file": str(folder / "preview.mp4"), "manifest_file": str(folder / "manifest.json"),
                "preview_only": True, "publishable": False,
                "source_blocked_for_production": plan["blocked_for_production"], "audio_present": narration is not None}


class CountingDrafter:
    def __init__(self, failures=()):
        self.failures, self.calls = list(failures), 0

    def draft(self, *, request, model):
        self.calls += 1
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        return MockScriptDrafter().draft(request=request, model=None)


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        scout_once("config/scout-sources.mock.json", root=self.root, clock=lambda: SCOUTED)
        verify_stored([], "config/verification.mock.json", verify_all=True, root=self.root, clock=lambda: VERIFIED)
        report, _ = select_stories([], "config/verification.mock.json", "config/editorial.mock.json", select_all=True,
                                   root=self.root, clock=lambda: SELECTED)
        self.run_id = report["selection_run_id"]
        self.record_id = next(e["record_id"] for e in report["entries"] if e["disposition"] == "select")
        self.rejected_id = next(e["record_id"] for e in report["entries"] if e["disposition"] == "reject")
        self.now = NOW

    def pipeline(self, renderer=None, drafter=None):
        self.renderer = renderer or FakeRenderer()
        return Pipeline(root=self.root, clock=lambda: self.now, renderer=self.renderer, drafter=drafter)

    def produce(self, pipeline=None, **kwargs):
        return (pipeline or self.pipeline()).produce(self.run_id, self.record_id, **kwargs)

    def state(self, production_id):
        return ProductionStore(self.root).read(production_id)

    def history_entries(self):
        profile, _ = load_profile("config/editorial.mock.json")
        return load_history(profile, self.root)[0]["entries"]


class SuccessTests(Base):
    def test_full_pipeline(self):
        result = self.produce()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["stages"].values()), {"completed"})
        self.assertEqual((result["result"]["publishable"], result["result"]["preview_only"], result["published"]),
                         (False, True, False))
        state = self.state(result["production_id"])
        folder = ProductionStore(self.root).folder(result["production_id"])
        for stage, key in (("brief", "brief_path"), ("creator", "script_path"), ("plan", "plan_path")):
            artifacts = next(s for s in state["stages"] if s["name"] == stage)["artifacts"]
            self.assertTrue((folder / artifacts[key]).is_file())
        brief = read_json(folder / state["stages"][0]["artifacts"]["brief_path"])
        script = read_json(folder / state["stages"][1]["artifacts"]["script_path"])
        plan = read_json(folder / state["stages"][3]["artifacts"]["plan_path"])
        self.assertEqual(brief["verification"]["record_ids"], [self.record_id])
        self.assertEqual(script["claims"], brief["claims"])
        self.assertEqual((plan["script"], plan["mode"], plan["blocked_for_production"]), (script, "production", False))
        self.assertEqual(state["config"]["creator"], {**state["config"]["creator"], "adapter": "mock", "paid": False})
        self.assertEqual([(t["stage"], t["event"]) for t in state["trace"]],
                         [(name, event) for name in production.STAGES for event in ("started", "completed")])
        entries = [e for e in self.history_entries() if e.get("production_id") == result["production_id"]]
        self.assertEqual([e["state"] for e in entries], ["produced"])

    def test_state_and_trace_hold_no_content_or_secrets(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-live-credential"}):
            result = self.produce()
        raw = (ProductionStore(self.root).folder(result["production_id"]) / "state.json").read_text()
        for forbidden in ("synthetic-live-credential", "OPENAI_API_KEY", "GTA VI trailer", "Fixture", "instructions",
                          "narration", "Traceback"):
            self.assertNotIn(forbidden, raw.replace('"narration": null', ""))

    def test_inspect_and_list(self):
        result = self.produce()
        view = inspect_production(result["production_id"], self.root)
        self.assertEqual(view["status"], "completed")
        rows = list_productions(self.root)
        self.assertEqual([(r["production_id"], r["status"], r["next_stage"]) for r in rows],
                         [(result["production_id"], "completed", None)])


class DuplicateAndConcurrencyTests(Base):
    def test_duplicate_production_refused(self):
        first = self.produce()
        with self.assertRaises(NetworkError) as raised:
            self.produce()
        self.assertEqual(raised.exception.code, "production_exists")
        with self.assertRaises(NetworkError) as raised:
            self.pipeline().resume(first["production_id"])
        self.assertEqual(raised.exception.code, "production_completed")
        report, _ = select_stories([], "config/verification.mock.json", "config/editorial.mock.json", select_all=True,
                                   root=self.root, clock=lambda: NOW)
        entry = next(e for e in report["entries"] if e["record_id"] == self.record_id)
        self.assertEqual((entry["disposition"], entry["reasons"][0]), ("reject", "duplicate_of_previous"))

    def test_concurrent_execution_blocked(self):
        result = self.produce(self.pipeline(FakeRenderer([NetworkError("render_failed", "x")])))
        store = ProductionStore(self.root)
        with store.lock(result["production_id"]):
            with self.assertRaises(NetworkError) as raised:
                self.pipeline().resume(result["production_id"])
            self.assertEqual(raised.exception.code, "production_locked")
            with self.assertRaises(NetworkError):
                inspect_production(result["production_id"], self.root)
        self.assertEqual(self.pipeline().resume(result["production_id"])["status"], "completed")

    def test_ineligible_story_never_starts(self):
        with self.assertRaises(NetworkError) as raised:
            self.pipeline().produce(self.run_id, self.rejected_id)
        self.assertEqual(raised.exception.code, "not_selected")
        self.assertEqual(list_productions(self.root), [])
        for args, code in (((("../x"), self.record_id), "invalid_selection_id"), ((self.run_id, "ver-x"), "invalid_record_id"),
                           (("sel-" + "0" * 24, self.record_id), "selection_not_found")):
            with self.subTest(code=code), self.assertRaises(NetworkError) as raised:
                self.pipeline().produce(*args)
            self.assertEqual(raised.exception.code, code)


class FailureAndResumeTests(Base):
    def test_stop_on_failure_and_resume_without_repeating(self):
        drafter = CountingDrafter()
        result = self.produce(self.pipeline(FakeRenderer([NetworkError("render_failed", "x")]), drafter))
        self.assertEqual((result["status"], result["error"]), ("failed", {"stage": "preview", "code": "render_failed"}))
        self.assertEqual(result["stages"], {"brief": "completed", "creator": "completed", "validate": "completed",
                                            "plan": "completed", "preview": "failed"})
        reserved = [e for e in self.history_entries() if e.get("production_id") == result["production_id"]]
        self.assertEqual([e["state"] for e in reserved], ["reserved"])  # Not recorded as a finished video.
        state_before = self.state(result["production_id"])
        resumed = self.pipeline(drafter=drafter).resume(result["production_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(drafter.calls, 1)
        self.assertEqual(len(self.renderer.calls), 1)
        state = self.state(result["production_id"])
        self.assertEqual([s["attempts"] for s in state["stages"]], [1, 1, 1, 1, 2])
        self.assertEqual(state["stages"][0]["artifacts"], state_before["stages"][0]["artifacts"])
        self.assertEqual([e["state"] for e in self.history_entries() if e.get("production_id") == result["production_id"]],
                         ["produced"])

    def test_failure_in_middle_stage(self):
        drafter = CountingDrafter([NetworkError("invalid_creator_output", "x")])
        result = self.produce(self.pipeline(drafter=drafter))
        self.assertEqual((result["status"], result["error"]["stage"]), ("failed", "creator"))
        self.assertEqual(result["stages"]["validate"], "pending")
        self.assertEqual(self.renderer.calls, [])
        self.assertEqual(self.pipeline(drafter=drafter).resume(result["production_id"])["status"], "completed")
        self.assertEqual(drafter.calls, 2)

    def test_unexpected_errors_are_sanitized(self):
        result = self.produce(self.pipeline(FakeRenderer([RuntimeError("secret /home/user sk-" + "a" * 30)])))
        self.assertEqual(result["error"], {"stage": "preview", "code": "stage_error"})
        raw = (ProductionStore(self.root).folder(result["production_id"]) / "state.json").read_text()
        self.assertNotIn("secret", raw)
        self.assertNotIn("/home/user", raw)

    def test_retry_budget(self):
        renderer = FakeRenderer([NetworkError("render_failed", "x")] * 5)
        result = self.produce(self.pipeline(renderer))
        for _ in range(2):
            result = Pipeline(root=self.root, clock=lambda: NOW, renderer=renderer).resume(result["production_id"])
        self.assertEqual(self.state(result["production_id"])["stages"][4]["attempts"], 3)
        result = Pipeline(root=self.root, clock=lambda: NOW, renderer=renderer).resume(result["production_id"])
        self.assertEqual(result["error"], {"stage": "preview", "code": "retry_exhausted"})
        self.assertEqual(len(renderer.calls), 3)

    def test_local_interruption_reruns_without_consent(self):
        result = self.produce(self.pipeline(FakeRenderer([NetworkError("render_failed", "x")])))
        renderer = FakeRenderer([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            Pipeline(root=self.root, clock=lambda: NOW, renderer=renderer).resume(result["production_id"])
        self.assertEqual(inspect_production(result["production_id"], self.root)["status"], "interrupted")
        resumed = self.pipeline().resume(result["production_id"])
        self.assertEqual(resumed["status"], "completed")
        events = [(t["stage"], t["event"]) for t in self.state(result["production_id"])["trace"]]
        self.assertIn(("preview", "interrupted"), events)

    def test_own_reservation_does_not_block_brief_recovery(self):
        real = production._write_reservation

        def write_then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise NetworkError("history_write_failed", "x")

        with patch.object(production, "_write_reservation", write_then_fail):
            result = self.produce()
        self.assertEqual(result["error"], {"stage": "brief", "code": "history_write_failed"})
        self.assertEqual(len([e for e in self.history_entries() if e.get("production_id") == result["production_id"]]), 1)
        report, _ = select_stories([], "config/verification.mock.json", "config/editorial.mock.json", select_all=True,
                                   root=self.root, clock=lambda: NOW)
        self.assertEqual(next(e for e in report["entries"] if e["record_id"] == self.record_id)["disposition"], "reject")
        resumed = self.pipeline().resume(result["production_id"])
        self.assertEqual(resumed["status"], "completed")
        mine = [e for e in self.history_entries() if e.get("production_id") == result["production_id"]]
        self.assertEqual([e["state"] for e in mine], ["produced"])
        self.assertEqual(len(self.history_entries()), 1)


class IntegrityTests(Base):
    def failed_at_preview(self):
        result = self.produce(self.pipeline(FakeRenderer([NetworkError("render_failed", "x")])))
        return result["production_id"], ProductionStore(self.root).folder(result["production_id"])

    def assert_resume_fails(self, production_id, code):
        with self.assertRaises(NetworkError) as raised:
            self.pipeline().resume(production_id)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.renderer.calls, [])

    def test_tampered_artifacts(self):
        for stage, key, mutate in (
                ("creator", "script_path", lambda d: d["beats"][0].update(narration="Edited narration text.")),
                ("brief", "brief_path", lambda d: d["claims"][0].update(status="verified", text="A new fact")),
                ("plan", "plan_path", lambda d: d.update(blocked_for_production=True))):
            with self.subTest(stage=stage):
                self.tearDown_productions()
                production_id, folder = self.failed_at_preview()
                state = self.state(production_id)
                path = folder / next(s for s in state["stages"] if s["name"] == stage)["artifacts"][key]
                document = read_json(path)
                mutate(document)
                path.write_text(json.dumps(document))
                self.assert_resume_fails(production_id, "artifact_tampered")

    def tearDown_productions(self):
        import shutil
        base = self.root / "runtime/productions"
        if base.exists():
            shutil.rmtree(base)
        history = self.root / "runtime/selection/history-gta.json"
        history.unlink(missing_ok=True)

    def test_missing_artifact_and_path_escape(self):
        production_id, folder = self.failed_at_preview()
        state = self.state(production_id)
        (folder / state["stages"][1]["artifacts"]["script_path"]).unlink()
        self.assert_resume_fails(production_id, "artifact_missing")
        self.tearDown_productions()
        production_id, folder = self.failed_at_preview()
        state = self.state(production_id)
        state["stages"][0]["artifacts"]["brief_path"] = "../../selection/history-gta.json"
        ProductionStore(self.root).write(state)
        self.assert_resume_fails(production_id, "artifact_tampered")

    def test_tampered_state_and_configuration(self):
        production_id, folder = self.failed_at_preview()
        state = self.state(production_id)
        state["config"]["capabilities"]["sha256"] = "0" * 64
        ProductionStore(self.root).write(state)
        self.assert_resume_fails(production_id, "configuration_mismatch")
        raw = read_json(folder / "state.json")
        raw["stages"][1]["status"] = "pending"
        (folder / "state.json").write_text(json.dumps(raw))
        self.assert_resume_fails(production_id, "invalid_production_state")
        raw["stages"][1]["status"] = "completed"
        raw["config"]["candidate_id"] = "cand-" + "0" * 24
        (folder / "state.json").write_text(json.dumps(raw))
        self.assert_resume_fails(production_id, "invalid_production_state")

    def test_stale_evidence_and_selection(self):
        production_id, _ = self.failed_at_preview()
        self.now = "2026-10-20T12:00:00Z"
        self.assert_resume_fails(production_id, "stale_evidence")
        self.tearDown_productions()
        self.now = "2026-10-06T13:00:00Z"
        with self.assertRaises(NetworkError) as raised:
            self.produce()
        self.assertEqual(raised.exception.code, "stale_selection")
        self.assertEqual(list_productions(self.root), [])


class PaidCreatorTests(Base):
    def test_paid_call_needs_consent(self):
        with self.assertRaises(NetworkError) as raised:
            self.produce(paths=PAID)
        self.assertEqual(raised.exception.code, "paid_consent_required")
        self.assertEqual(list_productions(self.root), [])
        drafter = CountingDrafter()
        result = self.produce(self.pipeline(drafter=drafter), paths=PAID, allow_paid=True)
        self.assertEqual((result["status"], drafter.calls), ("completed", 1))
        state = self.state(result["production_id"])
        self.assertEqual((state["config"]["creator"]["adapter"], state["config"]["creator"]["paid"]), ("anthropic", True))
        self.assertEqual(state["stages"][1]["artifacts"]["provider"], "anthropic")

    def test_uncertain_paid_request(self):
        drafter = CountingDrafter([NetworkError("provider_timeout", "x")])
        result = self.produce(self.pipeline(drafter=drafter), paths=PAID, allow_paid=True)
        self.assertEqual((result["status"], result["error"]), ("uncertain", {"stage": "creator", "code": "provider_timeout"}))
        for kwargs, code in (({"allow_paid": True}, "uncertain_stage"), ({"retry_uncertain": True}, "paid_consent_required")):
            with self.subTest(kwargs=kwargs), self.assertRaises(NetworkError) as raised:
                self.pipeline(drafter=drafter).resume(result["production_id"], **kwargs)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(drafter.calls, 1)
        resumed = self.pipeline(drafter=drafter).resume(result["production_id"], allow_paid=True, retry_uncertain=True)
        self.assertEqual((resumed["status"], drafter.calls), ("completed", 2))

    def test_pre_request_failure_is_not_uncertain(self):
        drafter = CountingDrafter([NetworkError("missing_credentials", "x")])
        result = self.produce(self.pipeline(drafter=drafter), paths=PAID, allow_paid=True)
        self.assertEqual((result["status"], result["error"]["code"]), ("failed", "missing_credentials"))
        with self.assertRaises(NetworkError) as raised:
            self.pipeline(drafter=drafter).resume(result["production_id"])
        self.assertEqual(raised.exception.code, "paid_consent_required")
        self.assertEqual(self.pipeline(drafter=drafter).resume(result["production_id"], allow_paid=True)["status"], "completed")

    def test_crash_during_paid_request_is_uncertain(self):
        drafter = CountingDrafter([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.produce(self.pipeline(drafter=drafter), paths=PAID, allow_paid=True)
        production_id = list_productions(self.root)[0]["production_id"]
        self.assertEqual(inspect_production(production_id, self.root)["status"], "uncertain")
        with self.assertRaises(NetworkError) as raised:
            self.pipeline(drafter=drafter).resume(production_id, allow_paid=True)
        self.assertEqual(raised.exception.code, "uncertain_stage")
        self.assertEqual(self.state(production_id)["stages"][1]["status"], "uncertain")
        resumed = self.pipeline(drafter=drafter).resume(production_id, allow_paid=True, retry_uncertain=True)
        self.assertEqual(resumed["status"], "completed")


class NarrationTests(Base):
    def wav(self, seconds=1):
        path = self.root / "voice.wav"
        path.write_bytes(canonical_wav(1, 8000, b"\x00\x00" * 8000 * seconds))
        return path

    def test_narration_passed_and_hash_checked(self):
        path = self.wav()
        result = self.produce(self.pipeline(FakeRenderer([NetworkError("render_failed", "x")])), narration=path)
        self.assertEqual(self.renderer.calls[0]["narration"], path.resolve())
        state = self.state(result["production_id"])
        self.assertEqual(state["config"]["narration"]["path"], str(path.resolve()))
        path.write_bytes(canonical_wav(1, 8000, b"\x01\x00" * 8000))
        with self.assertRaises(NetworkError) as raised:
            self.pipeline().resume(result["production_id"])
        self.assertEqual(raised.exception.code, "narration_changed")

    def test_invalid_narration_rejected_before_start(self):
        bad = self.root / "bad.wav"
        bad.write_bytes(b"not a wav file at all")
        with self.assertRaises(NetworkError) as raised:
            self.produce(narration=bad)
        self.assertTrue(raised.exception.code.startswith("narration_"))
        self.assertEqual(list_productions(self.root), [])


class CliTests(Base):
    def command(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["vicekrack", *args]), redirect_stdout(output):
            code = main()
        return code, json.loads(output.getvalue())

    def test_commands(self):
        for module in ("scout_cli", "verification_cli", "selection_cli", "production"):
            patch(f"vicekrack.{module}.ROOT", self.root).start()
        patch("vicekrack.production.utc_now", return_value=NOW).start()
        patch("vicekrack.verified_brief.utc_now", return_value=NOW).start()
        render = FakeRenderer([NetworkError("render_failed", "x")])
        patch("vicekrack.preview.render_preview", render).start()
        code, result = self.command("produce", self.run_id, self.record_id)
        self.assertEqual((code, result["status"], result["error"]["code"]), (1, "failed", "render_failed"))
        code, again = self.command("produce", self.run_id, self.record_id)
        self.assertEqual((code, again["error"]["code"]), (1, "production_exists"))
        code, listed = self.command("production-list")
        self.assertEqual((code, listed["productions"][0]["next_stage"]), (0, "preview"))
        code, shown = self.command("production-inspect", result["production_id"])
        self.assertEqual((code, shown["status"]), (0, "failed"))
        code, resumed = self.command("production-resume", result["production_id"])
        self.assertEqual((code, resumed["status"]), (0, "completed"))
        code, paid = self.command("produce", self.run_id, self.record_id, "--creator-config", "config/creator.openai.json")
        self.assertEqual((code, paid["error"]["code"]), (1, "paid_consent_required"))
        code, bad = self.command("production-inspect", "../etc")
        self.assertEqual((code, bad["error"]["code"]), (1, "invalid_production_id"))


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "set RUN_LOCAL_RENDER_TESTS=1 for real encoding")
class RealRenderIntegrationTest(Base):
    def test_real_preview_with_narration(self):
        from vicekrack.preview import render_preview
        voice = self.root / "voice.wav"
        voice.write_bytes(canonical_wav(1, 16000, b"\x00\x00" * 16000 * 3))
        pipeline = Pipeline(root=self.root, clock=lambda: NOW, renderer=render_preview)
        result = pipeline.produce(self.run_id, self.record_id, narration=voice)
        self.assertEqual(result["status"], "completed")
        manifest = read_json(Path(result["result"]["manifest_file"]))
        self.assertEqual((manifest["publishable"], manifest["preview_only"], manifest["audio_present"]), (False, True, True))
        self.assertGreater(Path(result["result"]["preview_file"]).stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
