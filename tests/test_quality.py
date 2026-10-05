"""Step 22: production quality report. Mocked media/poster measurements; no network.

Optional real-media integration tests run only with RUN_LOCAL_RENDER_TESTS=1.
"""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack import production
from vicekrack.__main__ import main
from vicekrack.errors import NetworkError
from vicekrack.narration import canonical_wav, load_narration
from vicekrack.orchestrator import read_json
from vicekrack.production import Pipeline, ProductionStore
from vicekrack.quality import ProbeFailed, ProbeUnavailable, QualityChecker, list_reports
from vicekrack.scout_cli import scout_once
from vicekrack.selection_cli import select_stories
from vicekrack.verification_cli import verify_stored

NOW = "2026-10-04T13:10:00Z"
GOOD_MEDIA = {"video_codec": "h264", "width": 1080, "height": 1920, "fps": 24.0, "duration": 15.0, "frames": 360,
              "audio_present": False, "audio_codec": None, "audio_duration": None}
BAND = {True: [255, 190, 85], False: [89, 214, 193]}


class ManifestRenderer:
    """Writes a package shaped like the real Step 13/14 renderer output (fake media bytes)."""

    def __call__(self, plan, *, allow_draft, directory, narration):
        folder = Path(directory) / f"{plan['plan_id']}-pkg"
        folder.mkdir(parents=True)
        video = folder / "preview.mp4"
        video.write_bytes(b"fake mp4 " + plan["plan_id"].encode())
        for scene in plan["scenes"]:
            (folder / f"scene-{scene['index']}.png").write_text(json.dumps(
                {"size": [1080, 1920], "colour": BAND[plan["blocked_for_production"]]}))
        manifest = {"contract": "preview_render", "version": "1.0", "plan_id": plan["plan_id"],
                    "input_sha256": plan["input_sha256"], "preview_only": True, "publishable": False,
                    "source_blocked_for_production": plan["blocked_for_production"], "width": 1080, "height": 1920,
                    "fps": 24, "duration_seconds": 15, "audio_present": narration is not None, "video": "preview.mp4",
                    "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "limitations": [],
                    "scenes": [{"index": s["index"], "method": s["selected_method"],
                                "start_seconds": s["beat"]["start_seconds"], "end_seconds": s["beat"]["end_seconds"],
                                "poster": f"scene-{s['index']}.png"} for s in plan["scenes"]]}
        if narration is not None:
            manifest["audio"] = load_narration(narration)[0]
        (folder / "manifest.json").write_text(json.dumps(manifest))
        return {"preview_file": str(video), "manifest_file": str(folder / "manifest.json"), "preview_only": True,
                "publishable": False, "source_blocked_for_production": plan["blocked_for_production"],
                "audio_present": narration is not None}


def fake_poster(path):
    data = json.loads(Path(path).read_text())
    return tuple(data["size"]), [tuple(data["colour"])] * 2


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        scout_once("config/scout-sources.mock.json", root=self.root, clock=lambda: "2026-10-04T12:00:00Z")
        verify_stored([], "config/verification.mock.json", verify_all=True, root=self.root,
                      clock=lambda: "2026-10-04T12:30:00Z")
        report, _ = select_stories([], "config/verification.mock.json", "config/editorial.mock.json", select_all=True,
                                   root=self.root, clock=lambda: "2026-10-04T13:00:00Z")
        self.run_id = report["selection_run_id"]
        self.record_id = next(e["record_id"] for e in report["entries"] if e["disposition"] == "select")
        self.media = dict(GOOD_MEDIA)
        self.now = NOW

    def produce(self, renderer=None, **kwargs):
        pipeline = Pipeline(root=self.root, clock=lambda: NOW, renderer=renderer or ManifestRenderer())
        self.result = pipeline.produce(self.run_id, self.record_id, **kwargs)
        self.pid = self.result["production_id"]
        self.folder = ProductionStore(self.root).folder(self.pid)
        return self.result

    def check(self, prober=None, poster=fake_poster):
        checker = QualityChecker(root=self.root, clock=lambda: self.now, prober=prober or (lambda path: dict(self.media)),
                                 poster_reader=poster)
        self.report, self.path = checker.run(self.pid)
        self.by_id = {c["check_id"]: c for c in self.report["checks"]}
        return self.report

    def state(self):
        return ProductionStore(self.root).read(self.pid)

    def artifact(self, stage, key):
        state = self.state()
        return self.folder / next(s for s in state["stages"] if s["name"] == stage)["artifacts"][key]

    def rewrite_json(self, stage, key, mutate, rehash=None):
        """Edit an artifact; with rehash=(stage, digest_key) also update the saved hash (consistent tamper)."""
        path = self.artifact(stage, key)
        document = read_json(path)
        mutate(document)
        path.write_text(json.dumps(document))
        if rehash:
            state = self.state()
            next(s for s in state["stages"] if s["name"] == rehash[0])["artifacts"][rehash[1]] = \
                hashlib.sha256(path.read_bytes()).hexdigest()
            ProductionStore(self.root).write(state)

    def assert_status(self, check_id, status, *reasons):
        self.assertEqual(self.by_id[check_id]["status"], status, self.by_id[check_id])
        for reason in reasons:
            self.assertIn(reason, self.by_id[check_id]["reasons"])
            self.assertIn(reason, self.report["reasons"])


class ValidRunTests(Base):
    def test_valid_run_passes(self):
        self.produce()
        report = self.check()
        self.assertEqual((report["result"], report["reasons"]), ("pass", []))
        self.assertEqual({c["status"] for c in report["checks"]}, {"pass"})
        self.assertEqual(report["scope"], {"technical_checks_only": True, "factual_accuracy_verified": False,
                                           "rights_cleared": False, "publishable": False, "permission_to_publish": False})
        self.assertEqual(self.path.parent, (self.root / "runtime/quality").resolve())
        self.assertEqual(self.by_id["video"]["details"]["frames"], 360)
        self.assertEqual(self.by_id["history"]["details"]["history_state"], "produced")

    def test_report_has_no_content_or_secrets(self):
        self.produce()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-live-credential"}):
            self.check()
        raw = self.path.read_text()
        for forbidden in ("synthetic-live-credential", "ANTHROPIC", "GTA VI", "Fixture", "Traceback", "ffmpeg version"):
            self.assertNotIn(forbidden, raw)

    def test_read_only_and_no_overwrite(self):
        self.produce()
        before = {p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}
        self.check()
        self.assertEqual({p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}, before)
        first = self.path
        self.check()  # Same second, same results: still a separate report; nothing is overwritten.
        self.assertNotEqual(first, self.path)
        self.assertTrue(first.is_file())
        self.assertEqual(len(list_reports(self.root)), 2)

    def test_locked_production(self):
        self.produce()
        with ProductionStore(self.root).lock(self.pid):
            with self.assertRaises(NetworkError) as raised:
                self.check()
        self.assertEqual(raised.exception.code, "production_locked")


class ArtifactTests(Base):
    def test_missing_artifacts(self):
        self.produce()
        self.artifact("creator", "script_path").unlink()
        report = self.check()
        self.assertEqual(report["result"], "fail")
        self.assert_status("artifacts", "fail", "creator_artifact_missing")
        self.assert_status("provenance", "unavailable", "artifacts_unavailable")

    def test_altered_artifacts(self):
        self.produce()
        self.rewrite_json("plan", "plan_path", lambda d: d.update(mode="draft"))
        self.artifact("preview", "preview_file").write_bytes(b"different video bytes")
        self.check()
        self.assert_status("artifacts", "fail", "plan_artifact_hash_mismatch", "preview_artifact_hash_mismatch")
        self.assertEqual(self.report["result"], "fail")

    def test_consistently_tampered_script_breaks_provenance(self):
        self.produce()
        self.rewrite_json("creator", "script_path", lambda d: d["claims"][0].update(text="An invented GTA VI fact."),
                          rehash=("creator", "script_sha256"))
        self.check()
        self.assert_status("provenance", "fail", "script_claims_differ_from_brief")

    def test_artifact_path_escape(self):
        self.produce()
        state = self.state()
        state["stages"][0]["artifacts"]["brief_path"] = "../../selection/history-gta.json"
        ProductionStore(self.root).write(state)
        self.check()
        self.assert_status("artifacts", "fail", "brief_artifact_outside_production")


class ManifestTests(Base):
    def edit_manifest(self, mutate):
        self.rewrite_json("preview", "manifest_file", mutate, rehash=("preview", "manifest_sha256"))

    def test_inconsistent_manifests(self):
        cases = {
            "manifest_scenes_not_matching_plan": lambda m: m["scenes"][0].update(end_seconds=4),
            "manifest_plan_mismatch": lambda m: m.update(plan_id="0" * 64),
            "manifest_video_hash_mismatch": lambda m: m.update(video_sha256="0" * 64),
            "manifest_format_wrong": lambda m: m.update(width=720),
            "manifest_publishable_flags_invalid": lambda m: m.update(publishable=True),
            "manifest_audio_flag_mismatch": lambda m: m.update(audio_present=True),
            "poster_missing": lambda m: m["scenes"][1].update(poster="scene-9.png"),
        }
        for code, mutate in cases.items():
            with self.subTest(code=code):
                self.reset()
                self.produce()
                self.edit_manifest(mutate)
                self.check()
                self.assertEqual(self.report["result"], "fail")
                self.assertIn(code, self.report["reasons"])

    def reset(self):
        for name in ("productions", "quality"):
            shutil.rmtree(self.root / "runtime" / name, ignore_errors=True)
        (self.root / "runtime/selection/history-gta.json").unlink(missing_ok=True)

    def test_watermark_wrong_or_unavailable(self):
        self.produce()
        poster = self.folder / Path(self.state()["stages"][4]["artifacts"]["preview_file"]).parent / "scene-2.png"
        poster.write_text(json.dumps({"size": [1080, 1920], "colour": [0, 0, 0]}))
        self.check()
        self.assert_status("draft_restrictions", "fail", "watermark_missing_or_wrong")
        def no_pillow(path):
            raise ProbeUnavailable()
        self.check(poster=no_pillow)
        self.assert_status("draft_restrictions", "needs_review", "watermark_check_unavailable")
        self.assertIn("poster_check_unavailable", self.report["reasons"])


class EvidenceTests(Base):
    def test_stale_evidence(self):
        self.produce()
        self.now = "2026-10-20T12:00:00Z"
        self.check()
        self.assertEqual(self.report["result"], "needs_review")
        self.assert_status("evidence_freshness", "needs_review", "evidence_stale")

    def test_tampered_or_missing_record(self):
        self.produce()
        record_path = self.root / "runtime/verification/records" / (self.state()["config"]["record_id"] + ".json")
        record = read_json(record_path)
        record["claims"][0]["status"] = "rejected"
        record_path.write_text(json.dumps(record))
        self.check()
        self.assert_status("provenance", "fail", "evidence_record_invalid")
        self.assert_status("evidence_freshness", "unavailable", "evidence_record_invalid")
        record_path.unlink()
        self.check()
        self.assert_status("provenance", "needs_review", "evidence_record_unavailable")
        self.assertEqual(self.report["result"], "needs_review")

    def test_policy_changed(self):
        self.produce()
        state = self.state()
        state["config"]["policy"]["sha256"] = "0" * 64
        ProductionStore(self.root).write(state)
        self.check()
        self.assert_status("provenance", "needs_review", "verification_policy_changed")


class DraftTests(Base):
    def produce_draft(self, **kwargs):
        real = production.select_brief

        def corroborated_brief(*args, **kw):
            # Keep the verified claim (Step 18 requires one) and add an unverified, draft-only claim.
            brief, entry = real(*args, **kw)
            extra = dict(brief["claims"][0], claim_id="c2", status="unverified",
                         text=brief["claims"][0]["text"].rstrip(".") + " according to early reports.")
            brief["claims"].append(extra)
            brief["verification"]["claims"].append(dict(brief["verification"]["claims"][0], claim_id="c2",
                                                        verification_status="corroborated"))
            return brief, entry

        with patch.object(production, "select_brief", corroborated_brief):
            return self.produce(allow_draft_preview=True, **kwargs)

    def test_draft_content_needs_review(self):
        self.assertEqual(self.produce_draft()["result"]["blocked_for_production"], True)
        self.check()
        self.assertNotEqual(self.report["result"], "pass")
        self.assert_status("draft_restrictions", "needs_review", "draft_content")
        self.assertIn("claim_not_matching_record", self.report["reasons"])  # Record says verified, brief says not.

    def test_draft_without_consent_or_inconsistent_flag(self):
        self.produce_draft()
        state = self.state()
        state["config"]["allow_draft_preview"] = False
        ProductionStore(self.root).write(state)
        self.check()
        self.assert_status("draft_restrictions", "fail", "draft_rendered_without_consent")
        self.rewrite_json("preview", "manifest_file", lambda m: m.update(source_blocked_for_production=False),
                          rehash=("preview", "manifest_sha256"))
        self.check()
        self.assertIn("manifest_draft_flag_mismatch", self.report["reasons"])


class MediaTests(Base):
    def test_video_mismatches(self):
        self.produce()
        self.media.update(width=720, height=1280, fps=30.0, duration=12.0, frames=300)
        self.check()
        self.assert_status("video", "fail", "video_dimensions_wrong", "video_fps_wrong", "video_duration_wrong",
                           "video_frame_count_wrong")
        self.assertIn("manifest_not_matching_media", self.report["reasons"])

    def test_audio_without_narration(self):
        self.produce()
        self.media.update(audio_present=True, audio_codec="aac", audio_duration=15.0)
        self.check()
        self.assert_status("audio", "fail", "audio_stream_unexpected")
        self.assertIn("manifest_audio_not_matching_media", self.report["reasons"])

    def narrated(self):
        voice = self.root / "voice.wav"
        voice.write_bytes(canonical_wav(1, 8000, b"\x00\x00" * 8000 * 3))
        self.produce(narration=voice)
        self.media.update(audio_present=True, audio_codec="aac", audio_duration=15.01)
        return voice

    def test_narrated_run_passes_and_mismatches(self):
        voice = self.narrated()
        self.check()
        self.assertEqual(self.report["result"], "pass")
        self.assertEqual(self.by_id["audio"]["details"]["source_duration_seconds"], 3.0)
        for update, code in (({"audio_present": False, "audio_codec": None, "audio_duration": None}, "audio_stream_missing"),
                             ({"audio_duration": 9.0}, "audio_duration_wrong"), ({"audio_duration": 15.5}, "audio_duration_wrong")):
            with self.subTest(code=code):
                self.media = {**GOOD_MEDIA, "audio_present": True, "audio_codec": "aac", "audio_duration": 15.01, **update}
                self.check()
                self.assert_status("audio", "fail", code)
        self.media = {**GOOD_MEDIA, "audio_present": True, "audio_codec": "aac", "audio_duration": 15.01}
        self.rewrite_json("preview", "manifest_file", lambda m: m["audio"].update(normalized_sha256="0" * 64),
                          rehash=("preview", "manifest_sha256"))
        self.check()
        self.assert_status("audio", "fail", "narration_not_matching_manifest")
        voice.write_bytes(canonical_wav(1, 8000, b"\x01\x00" * 8000 * 3))
        self.check()
        self.assertIn("narration_source_changed", self.report["reasons"])
        voice.unlink()
        self.check()
        self.assertIn("narration_source_unavailable", self.report["reasons"])

    def test_probe_unavailable_or_failed(self):
        self.produce()
        def unavailable(path):
            raise ProbeUnavailable()
        self.check(prober=unavailable)
        self.assertEqual(self.report["result"], "needs_review")
        self.assert_status("video", "unavailable", "media_probe_unavailable")
        self.assertIn("media_measurements_unavailable", self.report["reasons"])
        def broken(path):
            raise ProbeFailed("video_decode_failed")
        self.check(prober=broken)
        self.assert_status("video", "fail", "video_decode_failed")
        def crash(path):
            raise RuntimeError("secret /home/user path")
        self.check(prober=crash)
        self.assert_status("video", "fail", "video_unreadable")
        self.assertNotIn("secret", self.path.read_text())


class IncompleteAndStateTests(Base):
    def test_incomplete_run(self):
        from vicekrack.errors import NetworkError as NE

        class Failing(ManifestRenderer):
            def __call__(self, *a, **k):
                raise NE("render_failed", "x")

        self.produce(renderer=Failing())
        self.check()
        self.assertEqual(self.report["result"], "fail")
        self.assert_status("state", "fail", "production_incomplete")
        self.assertEqual(self.by_id["state"]["details"]["first_incomplete_stage"], "preview")
        for check_id in ("artifacts", "video", "audio", "history"):
            self.assert_status(check_id, "unavailable", "requires_completed_production")

    def test_invalid_state_and_missing_production(self):
        self.produce()
        raw = read_json(self.folder / "state.json")
        raw["stages"][2]["status"] = "pending"
        (self.folder / "state.json").write_text(json.dumps(raw))
        self.check()
        self.assert_status("state", "fail", "production_state_invalid")
        for production_id, code in (("prod-" + "0" * 24, "production_not_found"), ("../x", "invalid_production_id")):
            with self.subTest(code=code), self.assertRaises(NetworkError) as raised:
                QualityChecker(root=self.root).run(production_id)
            self.assertEqual(raised.exception.code, code)

    def test_history_not_produced(self):
        self.produce()
        history_path = self.root / "runtime/selection/history-gta.json"
        history = read_json(history_path)
        history["entries"][0]["state"] = "reserved"
        history_path.write_text(json.dumps(history))
        self.check()
        self.assert_status("history", "needs_review", "history_not_marked_produced")


class CliTests(Base):
    def test_commands(self):
        self.produce()
        for module in ("quality", "production", "selection_cli", "verification_cli"):
            patch(f"vicekrack.{module}.ROOT", self.root).start()
        patch("vicekrack.quality.probe_media", side_effect=lambda path: dict(GOOD_MEDIA)).start()
        patch("vicekrack.quality.poster_info", side_effect=fake_poster).start()
        patch("vicekrack.quality.utc_now", return_value=NOW).start()

        def run(*args):
            output = io.StringIO()
            with patch("sys.argv", ["vicekrack", *args]), redirect_stdout(output):
                code = main()
            return code, json.loads(output.getvalue())

        code, result = run("quality-report", self.pid)
        self.assertEqual((code, result["result"], result["scope"]["permission_to_publish"]), (0, "pass", False))
        self.assertTrue(Path(result["report_file"]).is_relative_to(self.root.resolve()))  # Temp dirs may be aliased.
        code, listed = run("quality-list")
        self.assertEqual((code, listed["reports"][0]["report_id"]), (0, result["report_id"]))
        code, bad = run("quality-report", "../etc")
        self.assertEqual((code, bad["error"]["code"]), (1, "invalid_production_id"))
        self.now = "2026-10-20T12:00:00Z"
        patch("vicekrack.quality.utc_now", return_value=self.now).start()
        code, stale = run("quality-report", self.pid)
        self.assertEqual((code, stale["result"]), (1, "needs_review"))


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "set RUN_LOCAL_RENDER_TESTS=1 for real media")
class RealMediaIntegrationTests(Base):
    def real_production(self, **kwargs):
        from vicekrack.preview import render_preview
        pipeline = Pipeline(root=self.root, clock=lambda: NOW, renderer=render_preview)
        self.result = pipeline.produce(self.run_id, self.record_id, **kwargs)
        self.pid = self.result["production_id"]
        self.folder = ProductionStore(self.root).folder(self.pid)

    def real_check(self):
        self.report, self.path = QualityChecker(root=self.root, clock=lambda: NOW).run(self.pid)
        self.by_id = {c["check_id"]: c for c in self.report["checks"]}

    def test_real_silent_and_narrated_media(self):
        self.real_production()
        self.real_check()
        self.assertEqual(self.report["result"], "pass", self.report["reasons"])
        self.assertEqual((self.by_id["video"]["details"]["frames"], self.by_id["audio"]["details"]["audio_present"]),
                         (360, False))
        silent = self.folder / self.state()["stages"][4]["artifacts"]["preview_file"]
        silent_bytes = silent.read_bytes()
        shutil.rmtree(self.root / "runtime/productions")
        (self.root / "runtime/selection/history-gta.json").unlink()
        voice = self.root / "voice.wav"
        voice.write_bytes(canonical_wav(1, 16000, b"\x00\x00" * 16000 * 2))
        self.real_production(narration=voice)
        self.real_check()
        self.assertEqual(self.report["result"], "pass", self.report["reasons"])
        self.assertGreaterEqual(self.by_id["audio"]["details"]["audio_duration"], 14.95)
        # Swap in the silent video and make every hash consistent: only real media measurement can catch it.
        video = self.folder / self.state()["stages"][4]["artifacts"]["preview_file"]
        video.write_bytes(silent_bytes)
        digest = hashlib.sha256(silent_bytes).hexdigest()
        self.rewrite_json("preview", "manifest_file", lambda m: m.update(video_sha256=digest),
                          rehash=("preview", "manifest_sha256"))
        state = self.state()
        state["stages"][4]["artifacts"]["video_sha256"] = digest
        ProductionStore(self.root).write(state)
        self.real_check()
        self.assertEqual(self.report["result"], "fail")
        self.assertIn("audio_stream_missing", self.report["reasons"])

    def test_real_truncated_video_fails(self):
        self.real_production()
        video = self.folder / self.state()["stages"][4]["artifacts"]["preview_file"]
        video.write_bytes(video.read_bytes()[:4000])
        state = self.state()
        state["stages"][4]["artifacts"]["video_sha256"] = hashlib.sha256(video.read_bytes()).hexdigest()
        ProductionStore(self.root).write(state)
        self.real_check()
        self.assertEqual(self.report["result"], "fail")
        self.assertTrue({"video_unreadable", "video_decode_failed", "manifest_video_hash_mismatch"} & set(self.report["reasons"]))


if __name__ == "__main__":
    unittest.main()
