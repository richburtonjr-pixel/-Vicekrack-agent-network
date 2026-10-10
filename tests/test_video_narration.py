"""Step 45: optional local narration in the controlled video-production workflow.

Local fixtures only (the Step 44 harness): a fake Grok transport (no sockets), a fake renderer that,
like the real encoder, produces a different video when narration is mixed in, and a fake media probe
that reports the audio stream it finds. No network and no credits. The real narrated demo (real
ffmpeg render and audio measurements) runs only with RUN_LOCAL_RENDER_TESTS=1, sockets blocked.
"""

import hashlib
import io
import json
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_video_production import Base, WorkflowRenderer
from vicekrack import video_production as vp
from vicekrack.errors import NetworkError
from vicekrack.export import PreviewExporter, verify_package
from vicekrack.media_production import render_production
from vicekrack.narration import MAX_BYTES, load_narration
from vicekrack.production import ProductionStore
from vicekrack.review import history, reviewable

NARRATED = b"|narrated:"


def wav(seconds=6.0, rate=16000, channels=1, value=1200, frames=None, bits=16):
    """A 16-bit PCM WAV (or another bit depth, to be refused) with a constant non-silent sample."""
    block = channels * bits // 8
    count = int(round(seconds * rate)) if frames is None else frames
    sample = (struct.pack("<h", value) * channels) if bits == 16 else bytes([128 + value % 100]) * block
    pcm = sample * count
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, channels, rate,
                       rate * block, block, bits, b"data", len(pcm)) + pcm


class NarratingRenderer(WorkflowRenderer):
    """Like the real renderer: narration is mixed into the video, so a narrated preview is a different file."""

    def __init__(self):
        super().__init__()
        self.narrations = []                       # the exact bytes the renderer was given, per revision

    def __call__(self, plan, **kwargs):
        narration = kwargs.get("narration")
        data = Path(narration).read_bytes() if narration is not None else None
        if kwargs.get("media") is not None:
            self.narrations.append(data)
        result = super().__call__(plan, **kwargs)
        if data is not None:
            folder = Path(result["manifest_file"]).parent
            video = folder / "preview.mp4"
            video.write_bytes(video.read_bytes() + NARRATED + hashlib.sha256(data).hexdigest().encode())
            manifest = json.loads((folder / "manifest.json").read_text())
            manifest.update(video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(), video_bytes=video.stat().st_size)
            (folder / "manifest.json").write_text(json.dumps(manifest))
        return result


class NarrationBase(Base):
    def setUp(self):
        super().setUp()
        self.renderer = NarratingRenderer()
        self.inputs = self.root / "user-recordings"
        self.inputs.mkdir()

    def probe(self, path):
        """Report an audio stream exactly when the rendered video carries narration (as ffprobe would)."""
        info = dict(self.media_info)
        if NARRATED in Path(path).read_bytes():
            info.update(audio_present=True, audio_codec="aac", audio_duration=15.01)
        return info

    def flow(self, config_root=None, prober=None):
        return super().flow(config_root=config_root, prober=prober or self.probe)

    def recording(self, name="voice.wav", data=None, **kwargs):
        path = self.inputs / name
        path.write_bytes(data if data is not None else wav(**kwargs))
        return path

    def wpath(self, name, wid=None):
        return self.root / "runtime/video-production" / (wid or self.wid) / name

    def preview(self):
        return next(s for s in ProductionStore(self.root).read(self.pid)["stages"] if s["name"] == "preview")["artifacts"]

    def narrated_to_review(self, **kwargs):
        self.narration = self.recording(**kwargs)
        self.start(narration=self.narration)
        self.submit_all()
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_review", view)
        self.pid, self.report_id = view["production_id"], view["outputs"]["quality_report_id"]
        return view

    def report(self, report_id=None):
        from vicekrack.quality import load_report
        return load_report(report_id or self.report_id, self.root)

    def nothing_created(self):
        folder = self.root / "runtime/video-production"
        self.assertFalse(folder.exists() and any(folder.iterdir()), "a refused start must create no workflow")
        self.assertEqual(self.grok.posts, [])


class NarratedWorkflowTests(NarrationBase):
    def test_narrated_preview_is_checked_reviewed_and_exported(self):
        view = self.narrated_to_review(seconds=6.5)
        source = self.narration.read_bytes()
        digest = hashlib.sha256(source).hexdigest()

        # Managed copy: the exact bytes, hashed; the user's path is never stored or printed.
        self.assertEqual(self.wpath(vp.NARRATION_FILE).read_bytes(), source)
        state = self.state()
        self.assertEqual(state["narration"]["source_sha256"], digest)
        self.assertEqual(state["narration"]["source_bytes"], len(source))
        self.assertEqual(state["narration"]["source_duration_seconds"], 6.5)
        self.assertEqual(state["narration"]["padded_duration_seconds"], 15)
        self.assertEqual(state["narration"]["duration_policy"], "pad_shorter_with_silence_reject_longer")
        text = json.dumps(state) + json.dumps(view)
        self.assertNotIn(str(self.inputs), text)
        self.assertNotIn("voice.wav", text)
        self.assertEqual(view["narration"]["present"], True)
        self.assertIn("generated source audio muted", view["narration"]["output"])

        # Mixed into the media-backed preview: the renderer got the managed bytes, clips stay muted.
        self.assertEqual(self.renderer.narrations, [source])
        artifacts = self.preview()
        self.assertEqual(artifacts["narration_sha256"], digest)
        self.assertTrue(artifacts["audio_present"])
        folder = ProductionStore(self.root).folder(self.pid)
        self.assertEqual((folder / artifacts["narration_path"]).read_bytes(), source)
        media = json.loads(self.wpath("media-manifest.json").read_text())
        self.assertIs(media["mute_source_audio"], True)
        manifest = json.loads((folder / artifacts["manifest_file"]).read_text())
        self.assertEqual(manifest["audio"]["normalized_sha256"], load_narration(self.narration)[0]["normalized_sha256"])

        # Quality is bound to the narrated preview, including the narration bytes.
        report = self.report()
        self.assertEqual(report["result"], "pass", report["reasons"])
        audio = next(c for c in report["checks"] if c["check_id"] == "audio")
        self.assertEqual(audio["status"], "pass")
        self.assertEqual(audio["details"]["narration_source"], "media_revision")
        roles = {row["role"]: row for row in report["binding"]["artifacts"]}
        self.assertEqual(roles["narration"]["sha256"], digest)
        self.assertEqual(roles["narration"]["ref"], artifacts["narration_path"].replace("\\", "/"))
        self.assertEqual(roles["video"]["sha256"], artifacts["video_sha256"])
        self.assertEqual(report["binding"]["status"], "bound")

        self.review()
        self.assertEqual(self.vp.resume(self.wid)["status"], "ready_to_export")
        exported = self.vp.export(self.wid, purpose="approved_preview")
        self.assertEqual(exported["status"], "exported")
        self.assertIs(exported["export"]["publishable"], False)
        package = Path(exported["export"]["path"])
        self.assertEqual(verify_package(package)["status"], "consistent")
        self.assertEqual((package / "media/preview.mp4").read_bytes(), (folder / artifacts["preview_file"]).read_bytes())
        self.assertIs(json.loads((package / "package.json").read_text())["restrictions"]["publishable"], False)
        self.assertFalse(list(package.rglob("*.wav")))                   # the narration source is never exported

        calls = (len(self.grok.posts), len(self.grok.status_checks), len(self.grok.downloads), self.renderer.revisions)
        self.assertEqual(self.vp.resume(self.wid, allow_network=True)["status"], "exported")
        self.assertEqual(calls, (len(self.grok.posts), len(self.grok.status_checks), len(self.grok.downloads),
                                 self.renderer.revisions))
        vp.validate_state(self.state())

    def test_silent_output_is_unchanged_when_narration_is_omitted(self):
        view = self.to_review()
        state = self.state()
        self.assertNotIn("narration", state)                              # Step 44 state, byte for byte in shape
        self.assertEqual(view["narration"], {"present": False, "output": "silent (generated source audio muted)"})
        self.assertEqual(self.wid, vp.workflow_id_for(state["origin"], state["generation"]))   # Step 44 IDs kept
        self.assertEqual(self.renderer.narrations, [None])
        artifacts = self.preview()
        self.assertFalse(artifacts["audio_present"])
        self.assertNotIn("narration_path", artifacts)
        self.assertNotIn("narration_sha256", artifacts)
        self.assertFalse(self.wpath("narration").exists())
        report = self.report()
        self.assertEqual(report["result"], "pass")
        self.assertNotIn("narration", {row["role"] for row in report["binding"]["artifacts"]})
        audio = next(c for c in report["checks"] if c["check_id"] == "audio")
        self.assertFalse(audio["details"]["narration_configured"])

    def test_narration_is_part_of_the_workflow_identity(self):
        first = self.recording("a.wav", seconds=3)
        second = self.recording("b.wav", seconds=4)
        self.start(narration=first)
        narrated_id = self.wid
        self.assertEqual(self.code(self.flow().start, selection_run_id=self.run_id, record_id=self.record_id,
                                   narration=first), "workflow_exists")
        other = self.flow().start(selection_run_id=self.run_id, record_id=self.record_id, narration=second)
        silent = self.flow().start(selection_run_id=self.run_id, record_id=self.record_id)
        self.assertEqual(len({narrated_id, other["workflow_id"], silent["workflow_id"]}), 3)
        self.assertEqual(self.grok.posts, [])                                 # starting never spends money


class InvalidNarrationTests(NarrationBase):
    def refused(self, path, code):
        self.assertEqual(self.code(self.flow().start, selection_run_id=self.run_id, record_id=self.record_id,
                                   narration=path), code)
        self.nothing_created()

    def test_missing_invalid_silent_and_overlong_narration_is_refused_before_anything_exists(self):
        self.refused(self.inputs / "not-there.wav", "narration_not_found")
        self.refused(self.inputs, "narration_unreadable")                       # a folder, not a file
        self.refused(self.recording("empty.wav", data=b""), "narration_empty")
        self.refused(self.recording("song.mp3", data=b"ID3" + b"\0" * 200), "narration_unsupported_format")
        self.refused(self.recording("8bit.wav", bits=8), "narration_unsupported_format")
        self.refused(self.recording("cut.wav", data=wav(seconds=2)[:-101]), "narration_corrupt")
        self.refused(self.recording("silence.wav", value=0), "narration_silent")
        self.refused(self.recording("long.wav", seconds=16), "narration_too_long")
        big = self.recording("big.wav", data=b"RIFF" + b"\0" * (MAX_BYTES + 1))
        self.refused(big, "narration_too_large")
        self.assertFalse((self.root / "runtime/video-jobs").exists() and any((self.root / "runtime/video-jobs").glob("vid-*")))

    def test_errors_never_echo_the_path_or_contents(self):
        path = self.recording("PRIVATE-name.wav", seconds=16)
        with self.assertRaises(NetworkError) as caught:
            self.flow().start(selection_run_id=self.run_id, record_id=self.record_id, narration=path)
        self.assertNotIn("PRIVATE", caught.exception.message)
        self.assertNotIn(str(self.inputs), caught.exception.message)


class DurationTests(NarrationBase):
    def test_shorter_narration_is_padded_with_silence_to_the_fifteen_second_preview(self):
        self.narrated_to_review(seconds=1.25, rate=8000)
        meta = self.state()["narration"]
        self.assertEqual((meta["source_duration_seconds"], meta["padded_duration_seconds"]), (1.25, 15))
        manifest = json.loads((ProductionStore(self.root).folder(self.pid) / self.preview()["manifest_file"]).read_text())
        self.assertEqual(manifest["audio"]["padded_duration_seconds"], 15)
        self.assertEqual(manifest["audio"]["source_duration_seconds"], 1.25)

    def test_exactly_fifteen_seconds_stereo_is_accepted(self):
        self.narrated_to_review(seconds=15, rate=48000, channels=2)
        meta = self.state()["narration"]
        self.assertEqual((meta["source_duration_seconds"], meta["channels"], meta["sample_rate"]), (15.0, 2, 48000))

    def test_one_sample_longer_than_fifteen_seconds_is_refused_never_cut_off(self):
        path = self.recording(frames=15 * 16000 + 1)
        self.assertEqual(self.code(self.flow().start, selection_run_id=self.run_id, record_id=self.record_id,
                                   narration=path), "narration_too_long")
        self.nothing_created()

    def test_audio_of_the_wrong_length_fails_the_quality_check(self):
        self.narration = self.recording(seconds=5)
        self.start(narration=self.narration)
        self.submit_all()
        short = lambda path: dict(self.probe(path), audio_duration=11.0)           # e.g. an encoder that cut speech
        self.vp = self.flow(prober=short)
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "quality_failed")
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "quality_required")


class ChangedAudioTests(NarrationBase):
    def test_changed_or_missing_managed_narration_blocks_resume_submit_and_export(self):
        self.narrated_to_review()
        managed = self.wpath(vp.NARRATION_FILE)
        original = managed.read_bytes()
        managed.write_bytes(original[:-2] + b"\x01\x02")                     # one sample changed
        for action in (lambda: self.vp.resume(self.wid), lambda: self.vp.export(self.wid, purpose="review_copy"),
                       lambda: self.vp.export(self.wid, purpose="approved_preview"),
                       lambda: self.vp.submit(self.wid, 1, consent=f"paid-generate:{self.jobs[1]}", allow_network=True)):
            self.assertEqual(self.code(action), "narration_tampered")
        self.assertEqual(self.vp.inspect(self.wid)["integrity_problems"], ["narration_tampered"])
        managed.unlink()
        self.assertEqual(self.code(self.vp.resume, self.wid), "narration_tampered")
        managed.write_bytes(original)                                        # exact bytes restored: usable again
        self.assertEqual(self.vp.resume(self.wid)["status"], "waiting_for_review")
        self.assertEqual(len(self.grok.posts), 4)

    def test_editing_the_original_file_after_start_has_no_effect(self):
        self.narration = self.recording(seconds=4)
        source = self.narration.read_bytes()
        self.start(narration=self.narration)
        self.narration.write_bytes(wav(seconds=9, value=-3000))              # the managed copy is the authority
        self.submit_all()
        self.assertEqual(self.vp.resume(self.wid, allow_network=True)["status"], "waiting_for_review")
        self.assertEqual(self.renderer.narrations, [source])
        self.narration.unlink()
        self.assertEqual(self.vp.resume(self.wid)["status"], "waiting_for_review")

    def test_kept_narration_changed_inside_the_production_breaks_the_binding(self):
        self.narrated_to_review()
        self.review()
        kept = ProductionStore(self.root).folder(self.pid) / self.preview()["narration_path"]
        kept.write_bytes(kept.read_bytes() + b"\0\0")
        row = next(r for r in reviewable(self.pid, self.root) if r["report_id"] == self.report_id)
        self.assertEqual(row["binding"], "changed")
        self.assertFalse(history(self.pid, self.root)["reviews"][0]["current_preview_approval"])
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "no_current_preview_approval")
        with self.assertRaises(NetworkError):
            PreviewExporter(self.root, clock=self.tick).export(self.pid, self.report_id, purpose="approved_preview")

    def test_a_preview_rendered_without_the_narration_is_refused(self):
        class DropsNarration(NarratingRenderer):
            def __call__(self, plan, **kwargs):
                kwargs["narration"] = None
                return super().__call__(plan, **kwargs)
        self.renderer = DropsNarration()
        self.narration = self.recording()
        self.start(narration=self.narration)
        self.submit_all()
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "failed")
        self.assertEqual(next(s for s in view["stages"] if s["name"] == "preview_revision")["error_code"],
                         "invalid_render_output")
        self.assertEqual(view["outputs"]["quality_report_id"], None)

    def test_revision_bytes_must_match_their_hash(self):
        self.narrated_to_review()
        manifest = json.loads(self.wpath("media-manifest.json").read_text())
        data = self.narration.read_bytes()
        with self.assertRaises(NetworkError) as caught:
            render_production(self.pid, manifest, self.wpath("media"), root=self.root, clock=self.tick,
                              renderer=self.renderer, narration={"data": data, "sha256": "0" * 64})
        self.assertEqual(caught.exception.code, "narration_changed")


class RecoveryTests(NarrationBase):
    def test_interrupted_narrated_revision_is_adopted_not_rendered_twice(self):
        self.narration = self.recording()
        self.start(narration=self.narration)
        self.submit_all()
        real = render_production

        def crash_after_render(*args, **kwargs):
            real(*args, **kwargs)
            raise KeyboardInterrupt("simulated crash after the narrated revision was saved")
        with patch("vicekrack.media_production.render_production", crash_after_render), \
                self.assertRaises(KeyboardInterrupt):
            self.vp.resume(self.wid, allow_network=True)
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual(self.renderer.revisions, 1)
        self.assertEqual((len(self.grok.posts), len(self.grok.downloads)), (4, 4))
        self.assertIn("adopted_existing_revision", [t["event"] for t in self.state()["trace"]])

    def test_resume_verifies_narration_and_never_regenerates_or_resubmits(self):
        self.narration = self.recording()
        self.start(narration=self.narration)
        for scene in (1, 2):
            self.submit(scene)
        with patch.object(vp.VideoProduction, "_stage_generation", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(self.vp.resume(self.wid, allow_network=True)["status"], "waiting_for_consent")
        self.assertEqual(len(self.grok.posts), 2)                            # resume never submits
        self.submit(3)
        self.submit(4)
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual((len(self.grok.posts), len(self.grok.downloads), self.renderer.revisions), (4, 4, 1))
        self.assertEqual(self.code(self.vp.submit, self.wid, 1, consent=f"paid-generate:{self.jobs[1]}",
                                   allow_network=True), "generation_complete")


class ReuseAndApprovalTests(NarrationBase):
    def silent_exported(self):
        self.to_review()
        self.review()
        self.assertEqual(self.vp.export(self.wid, purpose="approved_preview")["status"], "exported")
        return self.wid, self.report_id

    def test_narrated_workflow_reuses_paid_clips_and_old_approval_does_not_authorize_it(self):
        silent_wid, silent_report = self.silent_exported()
        calls = (len(self.grok.posts), len(self.grok.status_checks), len(self.grok.downloads))
        self.narration = self.recording()
        view = self.flow().start(selection_run_id=self.run_id, record_id=self.record_id, narration=self.narration)
        self.vp, self.wid = self.flow(), view["workflow_id"]
        # Same story and settings: the four paid jobs are reused; nothing is submitted or downloaded again.
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual(calls, (len(self.grok.posts), len(self.grok.status_checks), len(self.grok.downloads)))
        events = [t["event"] for t in self.state()["trace"]]
        self.assertEqual(sorted(e for e in events if e.startswith("reused_clip")),
                         [f"reused_clip_scene_{i}" for i in (1, 2, 3, 4)])
        self.assertEqual(self.renderer.revisions, 2)                          # the narrated video is a new render
        new_report = view["outputs"]["quality_report_id"]
        self.assertNotEqual(new_report, silent_report)

        # The silent approval does not authorize the narrated video, by any route.
        rows = {r["report_id"]: r for r in reviewable(self.pid, self.root)}
        self.assertEqual(rows[silent_report]["binding"], "changed")
        self.assertFalse(history(self.pid, self.root)["reviews"][0]["current_preview_approval"])
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "no_current_preview_approval")
        self.assertEqual(self.code(self.vp.export, silent_wid, purpose="approved_preview"), "production_preview_changed")
        with self.assertRaises(NetworkError):
            PreviewExporter(self.root, clock=self.tick).export(self.pid, silent_report, purpose="approved_preview")

        self.report_id = new_report
        self.review(supersedes=history(self.pid, self.root)["reviews"][0]["review_id"])
        self.assertEqual(self.vp.export(self.wid, purpose="approved_preview")["status"], "exported")

    def test_missing_shared_clip_is_never_downloaded_or_paid_for_again(self):
        silent_wid, _ = self.silent_exported()
        clip = self.wpath(f"media/{self.jobs[3]}.mp4", silent_wid)
        clip.write_bytes(clip.read_bytes() + b"changed")
        calls = (len(self.grok.posts), len(self.grok.downloads))
        view = self.flow().start(selection_run_id=self.run_id, record_id=self.record_id, narration=self.recording())
        self.assertEqual(view["status"], "failed")
        self.assertEqual(next(s for s in view["stages"] if s["name"] == "downloads")["error_code"], "clip_unavailable")
        self.assertEqual(calls, (len(self.grok.posts), len(self.grok.downloads)))

    def test_interrupted_clip_reuse_resumes_instead_of_reporting_tampering(self):
        self.silent_exported()
        with patch.object(vp.VideoProduction, "_adopt_clips", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.flow().start(selection_run_id=self.run_id, record_id=self.record_id, narration=self.recording())
        wid = next(w for w in vp.WorkflowStore(self.root).ids() if w != self.wid)
        view = self.flow().resume(wid)
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual(view["integrity_problems"], [])

    def test_a_later_revision_with_other_narration_invalidates_the_approval(self):
        self.narrated_to_review()
        self.review()
        self.assertEqual(self.vp.resume(self.wid)["status"], "ready_to_export")
        other = wav(seconds=3, value=-900)
        manifest = json.loads(self.wpath("media-manifest.json").read_text())
        render_production(self.pid, manifest, self.wpath("media"), root=self.root, clock=self.tick,
                          renderer=self.renderer, narration={"data": other, "sha256": hashlib.sha256(other).hexdigest()})
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "production_preview_changed")
        self.assertFalse(history(self.pid, self.root)["reviews"][0]["current_preview_approval"])

    def test_same_media_with_the_same_narration_is_not_rendered_twice(self):
        self.narrated_to_review()
        manifest = json.loads(self.wpath("media-manifest.json").read_text())
        data = self.narration.read_bytes()
        with self.assertRaises(NetworkError) as caught:
            render_production(self.pid, manifest, self.wpath("media"), root=self.root, clock=self.tick,
                              renderer=self.renderer, narration={"data": data, "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(caught.exception.code, "media_already_attached")


class CliTests(NarrationBase):
    def cli(self, *argv):
        from vicekrack.video_cli import main
        out = io.StringIO()
        flow = self.flow()
        with patch("vicekrack.video_production.VideoProduction", lambda: flow), redirect_stdout(out):
            code = main(list(argv))
        return code, json.loads(out.getvalue())

    def test_cli_start_with_narration_and_sanitized_errors(self):
        path = self.recording("PRIVATE-take.wav", seconds=20)
        code, error = self.cli("video-production-start", "--selection", self.run_id, "--record", self.record_id,
                               "--narration", str(path))
        self.assertEqual((code, error["error"]["code"]), (1, "narration_too_long"))
        self.assertNotIn("PRIVATE", json.dumps(error))
        code, view = self.cli("video-production-start", "--selection", self.run_id, "--record", self.record_id,
                              "--narration", str(self.recording(seconds=2)))
        self.assertEqual((code, view["status"], view["narration"]["present"]), (0, "waiting_for_consent", True))

    def test_cli_narrated_demo_dispatch(self):
        from vicekrack.video_cli import main
        out = io.StringIO()
        with patch("vicekrack.video_production_demo.run_demo", return_value={"demo": True}) as demo, redirect_stdout(out):
            self.assertEqual(main(["video-production-demo", "--narrated", "--output", str(self.root / "demo")]), 0)
        demo.assert_called_once_with(self.root / "demo", narrated=True)

    def test_step45_script_exists(self):
        import importlib.util
        from vicekrack.orchestrator import ROOT
        from vicekrack.video_production_demo import run_demo
        spec = importlib.util.spec_from_file_location("step45_demo", ROOT / "scripts/step45_demo.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.run_demo, run_demo)


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "set RUN_LOCAL_RENDER_TESTS=1 for the real demo")
class OfflineNarratedDemoTests(unittest.TestCase):
    def test_narrated_demo_produces_a_playable_mp4_with_the_network_blocked(self):
        from vicekrack.media_render import inspect_video
        from vicekrack.quality import probe_media
        from vicekrack.video_production_demo import run_demo
        with tempfile.TemporaryDirectory() as temp, \
                patch("socket.socket.connect", side_effect=AssertionError("network disabled in tests")):
            os.environ.pop("XAI_API_KEY", None)
            with patch("vicekrack.review._clock_now", return_value=datetime(2027, 3, 1, tzinfo=timezone.utc).timestamp()):
                summary = run_demo(Path(temp) / "demo", narrated=True)
            self.assertNotIn("XAI_API_KEY", os.environ)
            self.assertEqual((summary["status"], summary["quality_result"]), ("exported", "pass"))
            self.assertTrue(summary["export_verified"], summary["export_problems"])
            self.assertIs(summary["publishable"], False)
            self.assertEqual(summary["paid_requests"], 0)
            self.assertEqual(summary["mock_provider_counts"], {"posts": 5, "status_checks": 5, "downloads": 4})
            checks = {p["check"] for p in summary["proofs"]}
            for check in ("overlong_narration_refused_never_truncated", "all_silent_narration_refused",
                          "refused_narration_created_nothing", "changed_narration_refused_on_resume",
                          "narration_mixed_source_audio_muted", "resume_after_export_repeats_nothing"):
                self.assertIn(check, checks)
            video = Path(summary["video"])
            info, media = inspect_video(video), probe_media(video)
            self.assertEqual((info["width"], info["height"]), (1080, 1920))
            self.assertAlmostEqual(info["duration"], 15, delta=0.2)
            self.assertTrue(media["audio_present"])
            self.assertAlmostEqual(media["audio_duration"], 15, delta=0.1)
            self.assertGreater(summary["narration"]["speech_peak_db"], -30)
            self.assertLess(summary["narration"]["after_narration_peak_db"], -60)
            package_video = Path(summary["export_package"]) / "media/preview.mp4"
            self.assertEqual(package_video.read_bytes(), video.read_bytes())


if __name__ == "__main__":
    unittest.main()
