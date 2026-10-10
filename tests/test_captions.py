"""Step 47: narration-aligned captions.

Local fixtures only: a fake timestamped speech transport (no sockets), the Step 44-46 fake Grok
transport, a fake renderer that validates captions like the real one and produces a different
video when captions are burned in, and a fake media probe. No network and no credits. The real
captioned demo (real ffmpeg render, frame and audio checks) runs only with RUN_LOCAL_RENDER_TESTS=1.
"""

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_speech_narration import GTA, SpeechBase
from test_video_narration import NarratingRenderer, wav
from vicekrack import captions as cp
from vicekrack import speech_jobs as sj
from vicekrack import video_production as vp
from vicekrack.errors import NetworkError
from vicekrack.export import PreviewExporter, verify_package
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.preview import _check_captions
from vicekrack.review import history, reviewable

CAPTIONED = b"|captioned:"


def envelope(text, seconds=10.0, rate=24000, times=None, chars=None, duration=None):
    """The documented with_timestamps reply: base64 audio + per-character [start, end] seconds."""
    span = seconds - 0.4
    times = times if times is not None else [[round(0.2 + span * i / len(text), 3), round(0.2 + span * (i + 1) / len(text), 3)]
                                             for i in range(len(text))]
    body = {"audio": base64.b64encode(wav(seconds=seconds, rate=rate)).decode(), "content_type": "audio/wav",
            "duration": seconds if duration is None else duration,
            "audio_timestamps": {"graph_chars": list(text) if chars is None else chars, "graph_times": times}}
    return json.dumps(body).encode()


class TimedSpeech:
    def __init__(self, seconds=10.0, **kwargs):
        self.seconds, self.kwargs, self.bodies = seconds, kwargs, []

    def __call__(self, body, *, timeout_seconds, max_bytes):
        self.bodies.append(body)
        return {"content_type": "application/json",
                "audio": envelope(body["text"], self.seconds, body["output_format"]["sample_rate"], **self.kwargs)}


class CaptioningRenderer(NarratingRenderer):
    """Like the real renderer: captions are validated against the narration, recorded in the manifest and
    burned into the video (so a captioned preview is a different file)."""

    def __init__(self):
        super().__init__()
        self.tracks = []

    def __call__(self, plan, **kwargs):
        track = kwargs.pop("captions", None)
        info = _check_captions(track, kwargs.get("media"), kwargs.get("narration")) if track is not None else None
        if kwargs.get("media") is not None:
            self.tracks.append(track)
        result = super().__call__(plan, **kwargs)
        if info is not None:
            folder = Path(result["manifest_file"]).parent
            video = folder / "preview.mp4"
            video.write_bytes(video.read_bytes() + CAPTIONED + info["track_sha256"].encode())
            manifest = json.loads((folder / "manifest.json").read_text())
            manifest.update(video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
                            video_bytes=video.stat().st_size, captions=info)
            (folder / "manifest.json").write_text(json.dumps(manifest))
        return result


class CaptionBase(SpeechBase):
    def setUp(self):
        super().setUp()
        self.renderer = CaptioningRenderer()
        self.timed = TimedSpeech()

    def timed_job(self, script=None, transport=None, **kwargs):
        job = self.prepare(script, with_timestamps=True, **kwargs) if script is not None else \
            sj.prepare(self.prod_script, source="production", production_id=self.pid, with_timestamps=True,
                       root=self.root, clock=self.tick, **kwargs)
        return self.send(job, transport=transport or self.timed)

    def with_production(self):
        self.to_review()                                   # a silent workflow: production + four paid clips
        self.prod_script = sj.production_script(self.pid, self.root)
        self.silent_wid = self.wid
        return self.prod_script

    def track(self, timing="provider", job=None, script=None):
        return cp.prepare((job or self.job)["job_id"], timing=timing, script=script, root=self.root)

    def captioned(self, timing="provider"):
        self.with_production()
        self.job = self.timed_job()
        self.caption = self.track(timing)
        view = self.flow().start(production_id=self.pid, speech_job=self.job["job_id"],
                                 captions=self.caption["caption_id"])
        self.vp, self.wid = self.flow(), view["workflow_id"]
        self.report_id = view["outputs"]["quality_report_id"]
        return view

    def build(self, script=None, record=None, data=None, method="provider", timestamps=None):
        record = record or sj.inspect(self.job["job_id"], self.root)
        data = data if data is not None else sj.managed_audio(self.job["job_id"], self.root)[0]
        if method == "provider" and timestamps is None:
            timestamps = sj.timestamps(self.job["job_id"], self.root)
        return cp.build_track(script or self.script, record, data, method=method, timestamps=timestamps)


class TextAndBindingTests(CaptionBase):
    def setUp(self):
        super().setUp()
        self.job = self.timed_job(self.script)

    def test_captions_are_exactly_the_spoken_narration(self):
        track = self.track(script=self.script)
        self.assertEqual(" ".join(c["text"] for c in track["cues"]).split(),
                         " ".join(b["narration"] for b in self.script["beats"]).split())
        self.assertEqual([c["index"] for c in track["cues"]], list(range(1, len(track["cues"]) + 1)))
        for cue in track["cues"]:
            self.assertLessEqual(len(cue["lines"]), 2)
            self.assertTrue(all(len(line) <= 26 for line in cue["lines"]))
            self.assertEqual(" ".join(cue["lines"]), cue["text"])
            beat = self.script["beats"][cue["beat"] - 1]["narration"]
            self.assertIn(cue["text"], " ".join(beat.split()))                # never across beats
        for word in ("Title", self.script["title"]):
            self.assertNotIn(word, [c["text"] for c in track["cues"]])
        source = track["source"]
        self.assertEqual(source["script_sha256"], sj.script_digest(self.script))
        self.assertEqual(source["narration_sha256"], self.job["audio"]["sha256"])
        self.assertEqual(source["speech_job_id"], self.job["job_id"])
        self.assertEqual(track["language"], "en")
        self.assertIs(track["publishable"], False)

    def test_script_audio_and_text_mismatches_are_refused(self):
        other = read_json(ROOT / "examples/short-script-cooking.json")
        self.assertEqual(self.code(self.build, other), "caption_script_mismatch")
        record = sj.inspect(self.job["job_id"], self.root)
        self.assertEqual(self.code(self.build, data=wav(seconds=10, value=77)), "caption_narration_mismatch")
        edited = dict(record, text=record["text"].replace("Vice", "Vise"))
        self.assertEqual(self.code(self.build, record=edited), "caption_script_mismatch")
        self.assertEqual(self.code(cp.prepare, self.job["job_id"], timing="provider", root=self.root),
                         "caption_script_required")                      # a file-based job needs the same file
        self.assertEqual(self.code(cp.prepare, self.job["job_id"], timing="provider", script=other, root=self.root),
                         "caption_script_mismatch")
        self.assertEqual(self.code(cp.prepare, self.job["job_id"], timing="later", script=self.script, root=self.root),
                         "caption_timing_choice_required")

    def test_incomplete_speech_jobs_cannot_be_captioned(self):
        job = self.prepare(voice="leo")
        self.assertEqual(self.code(cp.prepare, job["job_id"], timing="estimated", script=self.script, root=self.root),
                         "speech_not_completed")


class TimingTests(CaptionBase):
    def setUp(self):
        super().setUp()
        self.job = self.timed_job(self.script)

    def test_provider_timing_is_validated_and_labelled(self):
        track = self.track(script=self.script)
        timing = track["timing"]
        self.assertEqual((timing["method"], timing["cue_boundaries"]),
                         ("provider_character_timestamps", "provider_word_boundaries"))
        self.assertIs(timing["word_synchronized"], False)
        self.assertIs(timing["requires_manual_timing_review"], False)
        self.assertEqual(timing["timestamps_sha256"], sj.inspect(self.job["job_id"], self.root)["timestamps"]["sha256"])
        stamps = sj.timestamps(self.job["job_id"], self.root)["data"]["graph_times"]
        text = self.job["text"]
        first = track["cues"][0]
        self.assertEqual(first["start_ms"], round(stamps[0][0] * 1000))
        second = track["cues"][1]
        self.assertEqual(second["start_ms"], round(stamps[text.index(second["text"].split()[0])][0] * 1000))
        ends = [c["end_ms"] for c in track["cues"]]
        self.assertLessEqual(ends[-1], 10000)                                 # within the measured narration
        for a, b in zip(track["cues"], track["cues"][1:]):
            self.assertLessEqual(a["end_ms"], b["start_ms"])

    def test_estimated_timing_is_explicit_and_never_called_synchronized(self):
        track = self.track("estimated", script=self.script)
        self.assertEqual(track["timing"]["method"], "estimated_phrase")
        self.assertEqual(track["timing"]["cue_boundaries"], "estimated_from_text_length")
        self.assertIs(track["timing"]["requires_manual_timing_review"], True)
        self.assertIs(track["timing"]["word_synchronized"], False)
        self.assertIsNone(track["timing"]["timestamps_sha256"])
        self.assertIn("ESTIMATED", track["timing"]["note"])
        self.assertIn("estimated phrase timing", cp.vtt(track).decode())
        self.assertIn("warning", cp.view(track))
        self.assertEqual((track["cues"][0]["start_ms"], track["cues"][-1]["end_ms"]), (0, 10000))
        relabelled = json.loads(json.dumps(track))
        relabelled["timing"]["requires_manual_timing_review"] = False
        self.assertEqual(self.code(cp.validate_track, relabelled), "captions_corrupt")

    def test_provider_timing_needs_saved_timestamps(self):
        job = self.send(self.prepare(voice="sal"))                          # no --with-timestamps
        self.assertIsNone(sj.timestamps(job["job_id"], self.root))
        self.assertEqual(self.code(cp.prepare, job["job_id"], timing="provider", script=self.script, root=self.root),
                         "provider_timing_unavailable")
        self.assertEqual(cp.prepare(job["job_id"], timing="estimated", script=self.script, root=self.root)
                         ["timing"]["method"], "estimated_phrase")
        self.assertEqual(len(self.tts.bodies), 1)                           # nothing paid again

    def test_invalid_provider_timestamps_are_refused(self):
        stamps = sj.timestamps(self.job["job_id"], self.root)
        text = self.job["text"]

        def variant(mutate):
            changed = json.loads(json.dumps(stamps))
            mutate(changed["data"])
            return changed

        def swap(d):
            d["graph_times"][5], d["graph_times"][6] = d["graph_times"][6], d["graph_times"][5]
        cases = {
            "caption_timing_out_of_order": swap,
            "caption_timing_negative": lambda d: d["graph_times"].__setitem__(0, [-0.5, 0.1]),
            "caption_timing_out_of_range": lambda d: d["graph_times"].__setitem__(-1, [9.9, 12.5]),
            "caption_timing_invalid": lambda d: d["graph_times"].__setitem__(3, [0.9, 0.5]),
            "caption_timing_text_mismatch": lambda d: d["graph_chars"].__setitem__(4, "X"),
        }
        for code, mutate in cases.items():
            self.assertEqual(self.code(self.build, timestamps=variant(mutate)), code, code)
        self.assertEqual(self.code(self.build, timestamps=variant(lambda d: d.update(duration=12.0))),
                         "caption_timing_out_of_range")
        self.assertEqual(self.code(self.build, timestamps=variant(lambda d: d["graph_times"].pop())),
                         "caption_timing_invalid")
        self.assertEqual(self.code(self.build, timestamps=variant(lambda d: d["graph_times"].__setitem__(2, ["a", 1]))),
                         "caption_timing_invalid")
        self.assertEqual(len(text), len(stamps["data"]["graph_chars"]))

    def test_cue_rules(self):
        timing = cp.load_config()["timing"]
        good = [{"index": 1, "start_ms": 0, "end_ms": 1000}, {"index": 2, "start_ms": 1000, "end_ms": 2000}]
        cp.validate_cues(good, 5000, timing)
        cases = {
            "caption_cues_overlap": [{"index": 1, "start_ms": 0, "end_ms": 1500}, {"index": 2, "start_ms": 1000, "end_ms": 2000}],
            "caption_cues_out_of_order": [{"index": 1, "start_ms": 2000, "end_ms": 3000}, {"index": 2, "start_ms": 0, "end_ms": 1000}],
            "caption_cue_negative": [{"index": 1, "start_ms": -10, "end_ms": 1000}],
            "caption_cue_out_of_range": [{"index": 1, "start_ms": 0, "end_ms": 5001}],
            "caption_cue_too_short": [{"index": 1, "start_ms": 0, "end_ms": 100}],
        }
        for code, cues in cases.items():
            with self.assertRaises(NetworkError) as caught:
                cp.validate_cues(cues, 5000, timing)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(NetworkError) as caught:
            cp.validate_cues([{"index": 1, "start_ms": 0, "end_ms": 15500}], 16000, timing)
        self.assertEqual(caught.exception.code, "caption_cue_out_of_range")     # never past the 15-second video
        with self.assertRaises(NetworkError) as caught:
            cp.validate_cues([{"index": 2, "start_ms": 0, "end_ms": 1000}], 5000, timing)
        self.assertEqual(caught.exception.code, "caption_cues_out_of_order")


class TextSafetyTests(CaptionBase):
    def scripted(self, narration, beat=1):
        script = json.loads(json.dumps(self.script))
        script["beats"][beat]["narration"] = narration
        return script

    def test_overflow_unsupported_and_unsafe_text_are_refused_not_dropped(self):
        long_word = self.scripted("It is set in Supercalifragilisticexpialidocious land.")
        self.job = self.timed_job(long_word)
        self.assertEqual(self.code(self.build, long_word), "caption_word_too_long")
        accented = self.scripted("It is set in Léonida, a fictional Florida.")
        self.job = self.timed_job(accented)
        self.assertEqual(self.code(self.build, accented), "caption_unsupported_character")
        for text in ("a <b> c", "a --> b", "a\x07b"):
            self.assertEqual(self.code(cp.check_characters, text), "caption_unsafe_text")

    def test_sidecars_escape_safely(self):
        script = self.scripted("Rockstar & Take-Two set it in Leonida.")
        self.job = self.timed_job(script)
        track = self.build(script)
        srt, vtt = cp.srt(track).decode(), cp.vtt(track).decode()
        self.assertIn("Rockstar & Take-Two", srt)
        self.assertIn("Rockstar &amp; Take-Two", vtt)
        self.assertNotIn("Rockstar & Take", vtt)
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertRegex(srt, r"^1\n\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}\n")
        blocks = [b for b in srt.strip().split("\n\n")]
        self.assertEqual(len(blocks), len(track["cues"]))                    # no blank line inside a cue

    def test_layout_avoids_preview_warnings(self):
        style = cp.load_config()["style"]
        for estimated in (False, True):
            cp.check_layout(style, estimated)
            left, top, right, bottom = cp.caption_box(style, estimated)
            for x0, y0, x1, y1 in cp.RESERVED:
                self.assertFalse(left < x1 and x0 < right and top < y1 and y0 < bottom)
        self.assertEqual(self.code(cp.check_layout, dict(style, box_bottom=1500), False), "caption_layout_collision")
        self.assertEqual(self.code(cp.check_layout, dict(style, box_left=0), False), "caption_layout_unsafe")


class StorageTests(CaptionBase):
    def setUp(self):
        super().setUp()
        self.job = self.timed_job(self.script)
        self.caption = self.track(script=self.script)

    def test_files_are_written_once_and_tampering_is_detected(self):
        folder = cp.folder(self.caption["caption_id"], self.root)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["captions.srt", "captions.vtt", "track.json"])
        track, files = cp.load(self.caption["caption_id"], self.root)
        self.assertEqual(track, self.caption)
        self.assertEqual(hashlib.sha256(files["captions.srt"]).hexdigest(), track["sidecars"]["srt_sha256"])
        self.assertEqual(self.track(script=self.script)["caption_id"], self.caption["caption_id"])    # same content, same ID
        for name, change in (("captions.srt", lambda b: b.replace(b"-->", b"->", 1)),
                             ("captions.vtt", lambda b: b + b"\n"),
                             ("track.json", lambda b: b.replace(b'"end_ms":', b'"end_ms": ', 1))):
            original = (folder / name).read_bytes()
            (folder / name).write_bytes(change(original))
            self.assertEqual(self.code(cp.load, self.caption["caption_id"], self.root), "captions_tampered", name)
            (folder / name).write_bytes(original)
        (folder / "captions.vtt").unlink()
        self.assertEqual(self.code(cp.load, self.caption["caption_id"], self.root), "captions_tampered")
        self.assertEqual(self.code(cp.load, "../x", self.root), "invalid_caption_id")
        self.assertEqual(self.code(cp.load, "cap-" + "0" * 24, self.root), "captions_not_found")

    def test_speech_timestamps_are_hash_checked(self):
        record = sj.inspect(self.job["job_id"], self.root)
        path = sj.folder(self.job["job_id"], self.root) / record["timestamps"]["file"]
        path.write_bytes(path.read_bytes().replace(b"0.2", b"0.3", 1))
        self.assertEqual(self.code(sj.timestamps, self.job["job_id"], self.root), "speech_timestamps_tampered")
        self.assertEqual(self.code(cp.prepare, self.job["job_id"], timing="provider", script=self.script,
                                   root=self.root), "speech_timestamps_tampered")

    def test_timestamped_request_and_malformed_envelope(self):
        self.assertTrue(self.job["request"]["with_timestamps"])
        self.assertNotEqual(self.prepare(voice="eve")["job_id"], self.job["job_id"])    # a different request
        bad = self.prepare(voice="leo", with_timestamps=True)
        broken = self.send(bad, transport=lambda body, **k: {"content_type": "application/json", "audio": b"{not json"})
        self.assertEqual((broken["status"], broken["error_code"]), ("invalid_audio", "speech_response_malformed"))
        self.assertEqual(self.code(self.send, bad), "speech_submit_refused")      # never resent automatically


class WorkflowTests(CaptionBase):
    def test_captioned_preview_is_bound_reviewed_and_exported(self):
        view = self.captioned()
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual(view["captions"]["caption_id"], self.caption["caption_id"])
        self.assertIs(view["captions"]["burned_in"], True)
        self.assertEqual(self.renderer.tracks[-1], self.caption)
        preview = self.preview()
        folder = self.root / "runtime/productions" / self.pid
        for key, name in (("captions_path", "track.json"), ("captions_srt_path", "captions.srt"),
                          ("captions_vtt_path", "captions.vtt")):
            self.assertEqual((folder / preview[key]).read_bytes(), cp.files_for(self.caption)[name])
        report = self.report()
        self.assertEqual(report["result"], "pass")
        roles = {row["role"] for row in report["binding"]["artifacts"]}
        self.assertTrue({"captions", "captions_srt", "captions_vtt", "narration", "video"} <= roles)
        check = next(c for c in report["checks"] if c["check_id"] == "manifest_consistency")
        self.assertEqual(check["details"]["caption_timing"], "provider_character_timestamps")

        self.review()
        exported = self.vp.export(self.wid, purpose="approved_preview")
        package = Path(exported["export"]["path"])
        self.assertEqual(verify_package(package)["status"], "consistent")
        manifest = json.loads((package / "package.json").read_text())
        paths = {row["path"]: row for row in manifest["files"]}
        self.assertEqual({p for p in paths if p.startswith("captions/")},
                         {"captions/captions.json", "captions/captions.srt", "captions/captions.vtt"})
        self.assertEqual((package / "media/preview.mp4").read_bytes(), (folder / preview["preview_file"]).read_bytes())
        self.assertEqual((package / "captions/captions.srt").read_bytes(), cp.srt(self.caption))
        self.assertIs(manifest["captions"]["burned_in_video"], True)
        self.assertIs(manifest["captions"]["sidecars_optional"], True)
        self.assertIs(manifest["restrictions"]["publishable"], False)
        page = (package / "index.html").read_text()
        self.assertIn("BURNED INTO media/preview.mp4", page)
        self.assertIn("OPTIONAL sidecar", page)

    def test_export_package_tampering_is_detected(self):
        self.captioned()
        self.review()
        package = Path(self.vp.export(self.wid, purpose="approved_preview")["export"]["path"])
        srt = package / "captions/captions.srt"
        original = srt.read_bytes()
        srt.write_bytes(original.replace(b"-->", b"-> ", 1))
        self.assertIn("hash_mismatch", verify_package(package)["problems"])
        # Consistently rewritten manifest: still caught by the report binding and the track.
        manifest_path = package / "package.json"
        manifest = json.loads(manifest_path.read_text())
        row = next(r for r in manifest["files"] if r["path"] == "captions/captions.srt")
        row.update(bytes=srt.stat().st_size, sha256=hashlib.sha256(srt.read_bytes()).hexdigest())
        manifest_path.write_text(json.dumps(manifest))
        problems = verify_package(package)["problems"]
        self.assertIn("payload_not_bound_by_report", problems)
        self.assertIn("captions_sidecar_mismatch", problems)
        srt.write_bytes(original)
        (package / "captions/captions.vtt").unlink()
        manifest["files"] = [r for r in manifest["files"] if r["path"] != "captions/captions.vtt"]
        manifest_path.write_text(json.dumps(manifest))
        self.assertIn("captions_incomplete", verify_package(package)["problems"])

    def test_changed_captions_invalidate_the_old_approval(self):
        self.captioned()
        first_wid, first_report = self.wid, self.report_id
        self.review()
        self.assertEqual(self.vp.resume(first_wid)["status"], "ready_to_export")
        estimated = self.track("estimated")
        posts, speech_calls = len(self.grok.posts), len(self.timed.bodies)
        view = self.flow().start(production_id=self.pid, speech_job=self.job["job_id"],
                                 captions=estimated["caption_id"])
        self.assertNotEqual(view["workflow_id"], first_wid)
        self.assertEqual((len(self.grok.posts), len(self.timed.bodies)), (posts, speech_calls))   # nothing paid again
        self.assertEqual({r["report_id"]: r for r in reviewable(self.pid, self.root)}[first_report]["binding"], "changed")
        self.assertFalse(history(self.pid, self.root)["reviews"][0]["current_preview_approval"])
        self.assertEqual(self.code(self.flow().export, first_wid, purpose="approved_preview"), "production_preview_changed")
        with self.assertRaises(NetworkError):
            PreviewExporter(self.root, clock=self.tick).export(self.pid, first_report, purpose="approved_preview")
        # Estimated timing is never a pass: approval needs the needs_review acknowledgment.
        self.wid, self.report_id = view["workflow_id"], view["outputs"]["quality_report_id"]
        report = self.report()
        self.assertEqual(report["result"], "needs_review")
        self.assertIn("caption_timing_estimated", report["reasons"])
        row = next(r for r in reviewable(self.pid, self.root) if r["report_id"] == self.report_id)
        self.assertIn("needs_review_result", row["applicable_acknowledgments"])

    def test_resume_reuses_paid_work_and_adopts_an_interrupted_render(self):
        self.with_production()
        self.job = self.timed_job()
        caption = self.track()
        posts, downloads, speech_calls = len(self.grok.posts), len(self.grok.downloads), len(self.timed.bodies)
        from vicekrack.media_production import render_production
        real = render_production

        def crash(*args, **kwargs):
            real(*args, **kwargs)
            raise KeyboardInterrupt
        with patch("vicekrack.media_production.render_production", crash), self.assertRaises(KeyboardInterrupt):
            self.flow().start(production_id=self.pid, speech_job=self.job["job_id"], captions=caption["caption_id"])
        wid = next(w for w in vp.WorkflowStore(self.root).ids() if w != self.silent_wid)
        renders = len(self.renderer.tracks)
        view = self.flow().resume(wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual(len(self.renderer.tracks), renders)                   # adopted, not rendered twice
        self.assertEqual((len(self.grok.posts), len(self.grok.downloads), len(self.timed.bodies)),
                         (posts, downloads, speech_calls))
        self.assertIn("adopted_existing_revision", [t["event"] for t in self.state(wid)["trace"]])

    def test_managed_caption_copy_and_kept_files_are_integrity_checked(self):
        self.captioned()
        managed = self.wpath("captions/captions.vtt")
        original = managed.read_bytes()
        managed.write_bytes(original + b"\n")
        for call in (lambda: self.vp.resume(self.wid), lambda: self.vp.export(self.wid, purpose="review_copy")):
            self.assertEqual(self.code(call), "captions_tampered")
        self.assertEqual(self.vp.inspect(self.wid)["integrity_problems"], ["captions_tampered"])
        managed.write_bytes(original)
        kept = self.root / "runtime/productions" / self.pid / self.preview()["captions_srt_path"]
        kept.write_bytes(kept.read_bytes() + b"\n")
        self.assertEqual({r["report_id"]: r for r in reviewable(self.pid, self.root)}[self.report_id]["binding"],
                         "changed")
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="review_copy"), "binding_not_matching")

    def test_captions_require_matching_speech_narration(self):
        self.with_production()
        self.job = self.timed_job()
        caption = self.track()
        start = self.flow().start
        self.assertEqual(self.code(start, production_id=self.pid, captions=caption["caption_id"]),
                         "captions_need_speech_narration")
        self.assertEqual(self.code(start, production_id=self.pid, narration=self.recording(),
                                   captions=caption["caption_id"]), "captions_need_speech_narration")
        other = self.send(sj.prepare(self.prod_script, source="production", production_id=self.pid, voice="leo",
                                     root=self.root, clock=self.tick))
        self.assertEqual(self.code(start, production_id=self.pid, speech_job=other["job_id"],
                                   captions=caption["caption_id"]), "captions_script_mismatch")
        self.assertEqual(self.code(start, production_id=self.pid, speech_job=self.job["job_id"],
                                   captions="cap-" + "0" * 24), "captions_not_found")
        self.assertEqual(vp.WorkflowStore(self.root).ids(), [self.silent_wid])          # nothing created

    def test_caption_free_workflows_are_unchanged(self):
        self.with_production()
        self.job = self.timed_job()
        view = self.flow().start(production_id=self.pid, speech_job=self.job["job_id"])
        self.assertEqual(view["captions"], {"present": False})
        self.assertNotIn("captions", self.state(view["workflow_id"]))
        self.assertNotIn("captions_path", self.preview())
        state = self.state(view["workflow_id"])
        self.assertEqual(view["workflow_id"], vp.workflow_id_for(state["origin"], state["generation"],
                                                                 state["narration"]["source_sha256"]))
        self.assertEqual(self.report(view["outputs"]["quality_report_id"])["result"], "pass")
        silent = self.state(self.silent_wid)
        self.assertEqual(self.silent_wid, vp.workflow_id_for(silent["origin"], silent["generation"]))


class RenderGuardTests(CaptionBase):
    def test_renderer_refuses_captions_without_media_or_matching_narration(self):
        self.job = self.timed_job(self.script)
        track = self.track(script=self.script)
        narration = self.root / "n.wav"
        narration.write_bytes(sj.managed_audio(self.job["job_id"], self.root)[0])
        self.assertEqual(self.code(_check_captions, track, None, narration), "captions_need_media")
        self.assertEqual(self.code(_check_captions, track, {"m": 1}, None), "captions_need_narration")
        other = self.root / "o.wav"
        other.write_bytes(wav(seconds=3))
        self.assertEqual(self.code(_check_captions, track, {"m": 1}, other), "captions_narration_mismatch")
        self.assertEqual(_check_captions(track, {"m": 1}, narration)["caption_id"], track["caption_id"])
        from vicekrack.media_production import _captions_override
        data = cp.files_for(track)["track.json"]
        good = {"data": data, "sha256": hashlib.sha256(data).hexdigest()}
        self.assertEqual(self.code(_captions_override, dict(good, sha256="0" * 64), {"sha256": "x"}), "captions_changed")
        self.assertEqual(self.code(_captions_override, good, None), "captions_need_narration")
        self.assertEqual(self.code(_captions_override, good, {"sha256": "0" * 64}), "captions_narration_mismatch")

    def test_overlay_filter_never_shows_two_cues_on_one_frame(self):
        graph = cp.overlay_filter([("a.png", 0, 1500), ("b.png", 1500, 3000)])
        self.assertIn("enable='gte(t,0.000)*lt(t,1.500)'", graph)
        self.assertIn("enable='gte(t,1.500)*lt(t,3.000)'", graph)
        self.assertTrue(graph.endswith("format=yuv420p[out]"))


class CliTests(CaptionBase):
    def cli(self, *argv):
        from vicekrack.captions_cli import main
        out = io.StringIO()
        with patch.object(cp, "base", lambda root=None: self.root / "runtime" / "captions"), \
                patch.object(sj, "base", lambda root=None: self.root / "runtime" / "speech-jobs"), redirect_stdout(out):
            code = main(list(argv))
        return code, json.loads(out.getvalue())

    def test_every_command_is_a_valid_choice(self):
        for command in ("captions-prepare", "captions-inspect", "captions-list"):
            result = subprocess.run([sys.executable, "-m", "vicekrack", command, "--help"], cwd=ROOT,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, (command, result.stderr))
        result = subprocess.run([sys.executable, "-m", "vicekrack", "speech-prepare", "--help"], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertIn("--with-timestamps", result.stdout)

    def test_cli_prepare_inspect_list(self):
        self.job = self.timed_job(self.script)
        code, view = self.cli("captions-prepare", "--speech", self.job["job_id"], "--timing", "estimated",
                              "--script", str(GTA))
        self.assertEqual((code, view["timing"]["method"]), (0, "estimated_phrase"))
        self.assertIn("warning", view)
        self.assertIn("--captions " + view["caption_id"], view["next"][0])
        code, shown = self.cli("captions-inspect", view["caption_id"], "--sidecar", "srt")
        self.assertTrue(shown["sidecar_text"].startswith("1\n00:00:00,000 --> "))
        self.assertEqual(self.cli("captions-list")[1]["captions"][0]["caption_id"], view["caption_id"])
        code, error = self.cli("captions-prepare", "--speech", self.job["job_id"], "--timing", "provider")
        self.assertEqual((code, error["error"]["code"]), (1, "caption_script_required"))

    def test_cli_start_with_captions_and_demo_dispatch(self):
        self.with_production()
        self.job = self.timed_job()
        caption = self.track()
        from vicekrack.video_cli import main
        out = io.StringIO()
        flow = self.flow()
        with patch("vicekrack.video_production.VideoProduction", lambda: flow), redirect_stdout(out):
            self.assertEqual(main(["video-production-start", "--production", self.pid, "--speech", self.job["job_id"],
                                   "--captions", caption["caption_id"]]), 0)
        self.assertEqual(json.loads(out.getvalue())["captions"]["caption_id"], caption["caption_id"])
        out = io.StringIO()
        with patch("vicekrack.caption_demo.run_caption_demo", return_value={"demo": True}) as demo, redirect_stdout(out):
            self.assertEqual(main(["video-production-demo", "--captions", "--output", str(self.root / "demo")]), 0)
        demo.assert_called_once_with(self.root / "demo")

    def test_step47_script_exists(self):
        import importlib.util
        from vicekrack.caption_demo import run_caption_demo
        spec = importlib.util.spec_from_file_location("step47_demo", ROOT / "scripts/step47_demo.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.run_caption_demo, run_caption_demo)


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "set RUN_LOCAL_RENDER_TESTS=1 for real rendering")
class RealRenderTests(CaptionBase):
    def test_cue_images_measure_text_and_refuse_overflow(self):
        from vicekrack.preview import dependencies
        self.job = self.timed_job(self.script)
        track = self.track("estimated", script=self.script)
        images = cp.cue_images(track, self.root, dependencies())
        self.assertEqual([(s, e) for _, s, e in images], [(c["start_ms"], c["end_ms"]) for c in track["cues"]])
        from PIL import Image
        with Image.open(self.root / images[0][0]) as image:
            self.assertEqual(image.size, (1080, 1920))
            self.assertEqual(image.getpixel((540, 100))[3], 0)               # warning band left untouched
            self.assertEqual(image.getpixel((540, 1600))[3], 0)              # bottom title box left untouched
            self.assertGreater(image.getpixel((80, 1390))[3], 200)           # caption box
        wide = json.loads(json.dumps(track))
        wide["cues"][0]["lines"] = ["W" * 26]
        self.assertEqual(self.code(cp.cue_images, wide, self.root, dependencies()), "caption_overflow")



@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "set RUN_LOCAL_RENDER_TESTS=1 for the real demo")
class OfflineCaptionDemoTests(unittest.TestCase):
    def test_caption_demo_renders_with_the_network_blocked(self):
        from PIL import Image
        from vicekrack.caption_demo import run_caption_demo
        from vicekrack.media_render import inspect_video
        with tempfile.TemporaryDirectory() as temp, \
                patch("socket.socket.connect", side_effect=AssertionError("network disabled in tests")):
            os.environ.pop("XAI_API_KEY", None)
            with patch("vicekrack.review._clock_now", return_value=datetime(2027, 3, 1, tzinfo=timezone.utc).timestamp()):
                summary = run_caption_demo(Path(temp) / "demo")
            self.assertNotIn("XAI_API_KEY", os.environ)
            self.assertTrue(summary["export_verified"], summary["export_problems"])
            self.assertEqual((summary["quality_result"], summary["publishable"]), ("pass", False))
            self.assertIn("NOT a Grok voice", summary["speech_audio"])
            self.assertEqual(summary["mock_counts"], {"speech_calls": 1, "video_posts": 4, "video_downloads": 4})
            checks = {p["check"] for p in summary["proofs"]}
            for check in ("captions_repeat_the_spoken_text_exactly", "out_of_order_provider_timing_refused",
                          "captions_from_another_script_refused", "changed_caption_sidecar_refused",
                          "paid_clips_and_speech_reused_no_new_request", "caption_change_invalidates_earlier_approval",
                          "estimated_timing_is_needs_review", "estimated_captions_need_their_own_review"):
                self.assertIn(check, checks)
            info = inspect_video(Path(summary["video"]))
            self.assertEqual((info["width"], info["height"]), (1080, 1920))
            self.assertAlmostEqual(info["duration"], 15, delta=0.2)
            self.assertAlmostEqual(summary["audio"]["seconds"], 15, delta=0.1)
            for sidecar in summary["sidecars"]:
                self.assertTrue(Path(sidecar).is_file())
            cue_frame, after = (Image.open(summary["frames"][0]).convert("RGB"),
                                Image.open(summary["frames"][3]).convert("RGB"))
            points = [(x, y) for x in (100, 300, 780, 980) for y in (1240, 1390)]      # inside the caption box

            def dark(image):                                                   # the near-black caption box
                return sum(all(c < 70 for c in image.getpixel(p)) for p in points) >= 6
            self.assertTrue(dark(cue_frame))                                   # caption box present while spoken
            self.assertFalse(dark(after))                                      # gone after the narration ends
            for image in (cue_frame, after):
                r, g, b = image.getpixel((900, 100))
                self.assertTrue(abs(r - 89) < 30 and abs(g - 214) < 30 and abs(b - 193) < 30)   # warning band kept


if __name__ == "__main__":
    unittest.main()
