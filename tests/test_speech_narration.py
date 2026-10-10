"""Step 46: Grok-generated narration (xAI text to speech) and its use in the video-production workflow.

Local fixtures only: a fake speech transport (no sockets), the Step 44/45 fake Grok video transport,
renderer and probe. No network and no credits. The real speech demo (real ffmpeg render and audio
measurements) runs only with RUN_LOCAL_RENDER_TESTS=1, sockets blocked.
"""

import hashlib
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from test_video_narration import NarrationBase, wav
from vicekrack import speech_jobs as sj
from vicekrack import speech_transport as st
from vicekrack import video_production as vp
from vicekrack.errors import NetworkError
from vicekrack.export import PreviewExporter
from vicekrack.narration import parse_wav
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.review import history, reviewable

KEY = "synthetic-test-value-not-a-key"
GTA = ROOT / "examples/short-script-gta.json"


def with_metadata(data):
    """Insert a LIST/INFO chunk (as many encoders do) before the data chunk of a canonical WAV."""
    info = b"INFO" + b"ISFT" + struct.pack("<I", 6) + b"maker\x00"
    body = data[12:36] + b"LIST" + struct.pack("<I", len(info)) + info + data[36:]
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


class FakeSpeech:
    """Stands in for POST /v1/tts. Each queued effect is an exception to raise or bytes to return."""

    def __init__(self, *effects, default=None):
        self.effects = list(effects)
        self.default = default if default is not None else with_metadata(wav(seconds=9.5, rate=24000))
        self.bodies = []

    def __call__(self, body, *, timeout_seconds, max_bytes):
        self.bodies.append(json.loads(json.dumps(body)))
        assert timeout_seconds > 0 and max_bytes > 0
        effect = self.effects.pop(0) if self.effects else self.default
        if isinstance(effect, BaseException):
            raise effect
        return {"content_type": "audio/wav", "audio": effect}


class SpeechBase(NarrationBase):
    def setUp(self):
        super().setUp()
        self.tts = FakeSpeech()
        self.script = read_json(GTA)

    def prepare(self, script=None, **kwargs):
        return sj.prepare(script or self.script, root=self.root, clock=self.tick, **kwargs)

    def send(self, job, transport=None, **kwargs):
        kwargs.setdefault("consent", job["consent_phrase"])
        kwargs.setdefault("allow_network", True)
        return sj.submit(job["job_id"], transport=transport or self.tts, root=self.root, clock=self.tick, **kwargs)

    def job_file(self, job, name="job.json"):
        return sj.folder(job["job_id"], self.root) / name

    def production_job(self, **kwargs):
        """A silent workflow makes the production; the speech job is prepared from its saved script."""
        self.to_review()
        script = sj.production_script(self.pid, self.root)
        return sj.prepare(script, source="production", production_id=self.pid, root=self.root, clock=self.tick,
                          **kwargs)


class GenerationTests(SpeechBase):
    def test_successful_generation_is_reviewable_bound_and_converted(self):
        job = self.prepare()
        # The spoken text: the narration beats in order, words unchanged, nothing else.
        beats = [b["narration"] for b in self.script["beats"]]
        self.assertEqual(job["text"], "\n".join(beats))
        self.assertEqual(job["text"].split(), " ".join(beats).split())
        for other in (self.script["title"], self.script["beats"][0]["visual"]["description"]):
            self.assertNotIn(other, job["text"])
        self.assertEqual(job["request"], {"text": job["text"], "voice_id": "eve", "language": "en",
                                          "output_format": {"codec": "wav", "sample_rate": 24000}})
        self.assertEqual(job["script"]["script_sha256"], sj.script_digest(self.script))
        self.assertEqual(job["consent_phrase"], f"paid-speech:{job['job_id']}")
        self.assertEqual((job["status"], job["attempts"], self.tts.bodies), ("prepared", [], []))   # offline
        self.assertEqual(sj.inspect(job["job_id"], self.root)["text"], job["text"])

        done = self.send(job)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.tts.bodies, [job["request"]])
        self.assertIsNone(done["provider_request_id"])                     # none documented, none invented
        self.assertEqual([a["outcome"] for a in done["attempts"]], ["received"])
        managed = self.job_file(job, "narration.wav").read_bytes()
        raw = self.job_file(job, done["raw_audio"]["file"]).read_bytes()
        self.assertIn(b"LIST", raw)
        self.assertNotIn(b"LIST", managed)                                  # metadata stripped
        channels, rate, pcm = parse_wav(managed)
        self.assertEqual((channels, rate, len(managed)), (1, 24000, 44 + len(pcm)))
        self.assertEqual(done["audio"]["sha256"], hashlib.sha256(managed).hexdigest())
        self.assertEqual(done["raw_audio"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(done["audio"]["duration_seconds"], 9.5)
        self.assertIsNotNone(done["audio"]["normalized_sha256"])
        text = self.job_file(job).read_text()
        self.assertNotIn(KEY, text)
        self.assertNotIn("Authorization", text)
        self.assertIs(done["publishable"], False)
        data, record = sj.managed_audio(job["job_id"], self.root)
        self.assertEqual(data, managed)

    def test_same_script_and_settings_are_one_job_and_settings_change_the_job(self):
        job = self.prepare()
        self.assertEqual(self.prepare()["job_id"], job["job_id"])
        self.send(job)
        self.assertEqual(self.prepare()["status"], "completed")             # prepare never resets a paid job
        for kwargs in ({"voice": "leo"}, {"language": "fr"}, {"sample_rate": 48000}, {"codec": "mp3"}):
            self.assertNotEqual(self.prepare(**kwargs)["job_id"], job["job_id"])
        changed = json.loads(json.dumps(self.script))
        changed["beats"][3]["narration"] = "Follow for more updates."
        self.assertNotEqual(self.prepare(changed)["job_id"], job["job_id"])
        self.assertEqual(len(self.tts.bodies), 1)

    def test_invalid_settings_and_text_are_refused_offline(self):
        for kwargs in ({"voice": "my-cloned-voice"}, {"language": "xx"}, {"codec": "flac"}, {"sample_rate": 12345},
                       {"bit_rate": 128000}, {"codec": "mp3", "bit_rate": 1}, {"sample_rate": "24000"}):
            with self.assertRaises(NetworkError) as caught:
                self.prepare(**kwargs)
            self.assertEqual(caught.exception.code, "invalid_speech_settings", kwargs)
        tagged = json.loads(json.dumps(self.script))
        tagged["beats"][1]["narration"] = "It is set in Leonida [laugh] for real."
        self.assertEqual(self.code(self.prepare, tagged), "speech_text_has_markup")
        broken = json.loads(json.dumps(self.script))
        del broken["beats"][0]
        with self.assertRaises(NetworkError):
            self.prepare(broken)
        self.assertFalse(sj.base(self.root).exists() and any(p.name.startswith("sp-") for p in sj.base(self.root).iterdir()))
        self.assertEqual(self.code(sj.inspect, "../escape", self.root), "invalid_speech_job_id")

    def test_missing_credential_sends_nothing(self):
        job = self.prepare()
        with patch.dict(os.environ, {"XAI_API_KEY": ""}):
            self.assertEqual(self.code(self.send, job), "missing_speech_credential")
        self.assertEqual(self.code(self.send, job, consent="paid-speech:wrong"), "speech_consent_required")
        self.assertEqual(self.code(self.send, job, allow_network=False), "network_not_allowed")
        self.assertEqual(self.tts.bodies, [])
        self.assertEqual(sj.inspect(job["job_id"], self.root)["status"], "prepared")
        with patch.dict(os.environ, {"XAI_API_KEY": ""}):
            self.assertEqual(self.code(st.transport, {"text": "x"}, timeout_seconds=1, max_bytes=10),
                             "missing_speech_credential")


class PaidRecoveryTests(SpeechBase):
    def test_timeouts_and_unknown_failures_are_uncertain_and_never_retried_automatically(self):
        for failure in (TimeoutError("private provider detail"),
                        NetworkError("speech_transport_failed", "x"), ValueError("private")):
            job = self.prepare(voice=("eve", "leo", "ara")[len(self.tts.bodies)])
            self.assertEqual(self.code(self.send, job, transport=FakeSpeech(failure)), "speech_submit_uncertain")
            record = sj.inspect(job["job_id"], self.root)
            self.assertEqual((record["status"], record["error_code"]), ("uncertain", "submission_outcome_unknown"))
            self.assertNotIn("private", self.job_file(job).read_text())
            self.tts.bodies.append(None)

    def test_uncertain_submission_needs_an_acknowledged_explicit_retry(self):
        job = self.prepare()
        transport = FakeSpeech(TimeoutError())
        self.code(self.send, job, transport=transport)
        self.assertEqual(self.code(self.send, job, transport=transport), "speech_submit_refused")
        self.assertEqual(self.code(self.send, job, transport=transport, retry_uncertain=True),
                         "duplicate_billing_ack_required")
        self.assertEqual(len(transport.bodies), 1)
        done = self.send(job, transport=transport, retry_uncertain=True, acknowledge_duplicate_billing=True)
        self.assertEqual(done["status"], "completed")
        self.assertEqual([a["outcome"] for a in done["attempts"]], ["uncertain", "received"])
        self.assertEqual(self.code(self.send, job, transport=transport, retry_uncertain=True,
                                   acknowledge_duplicate_billing=True), "speech_submit_refused")   # paid once
        self.assertEqual(len(transport.bodies), 2)

    def test_intent_is_saved_before_sending_and_an_interrupted_submission_is_recovered_as_uncertain(self):
        job = self.prepare()
        seen = []

        def crash(body, **kwargs):
            seen.append(sj.inspect(job["job_id"], self.root)["status"])     # what is on disk while sending
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.send(job, transport=crash)
        self.assertEqual(seen, ["submitting"])
        self.assertEqual(sj.inspect(job["job_id"], self.root)["status"], "submitting")
        self.assertEqual(self.code(self.send, job, retry_uncertain=True, acknowledge_duplicate_billing=True),
                         "speech_submit_refused")
        recovered = sj.recover(job["job_id"], root=self.root, clock=self.tick)
        self.assertEqual((recovered["status"], recovered["error_code"]), ("uncertain", "interrupted_submission"))
        self.assertEqual(self.tts.bodies, [])
        self.assertTrue(any("--acknowledge-duplicate-billing" in line for line in sj.view(recovered)["next"]))

    def test_definite_refusals_and_unsent_requests(self):
        job = self.prepare()
        self.assertEqual(self.code(self.send, job, transport=FakeSpeech(st.SpeechRejected(401))), "speech_rejected")
        record = sj.inspect(job["job_id"], self.root)
        self.assertEqual((record["status"], record["attempts"][-1]["http_status"]), ("rejected", 401))
        self.assertEqual(self.code(self.send, job, transport=FakeSpeech(NetworkError("speech_not_sent", "x"))),
                         "speech_not_sent")
        self.assertEqual(sj.inspect(job["job_id"], self.root)["status"], "rejected")
        self.assertEqual(self.send(job)["status"], "completed")             # a new explicit, consented attempt

    def test_concurrent_and_repeated_submissions(self):
        job = self.prepare()
        with sj._Locked(self.root):
            self.assertEqual(self.code(self.send, job), "speech_busy")
            self.assertEqual(self.code(self.prepare), "speech_busy")
            self.assertEqual(self.code(sj.recover, job["job_id"], root=self.root), "speech_busy")
        self.assertEqual(self.tts.bodies, [])
        self.send(job)
        self.assertEqual(self.code(self.send, job), "speech_submit_refused")
        self.assertEqual(len(self.tts.bodies), 1)

    def test_attempt_limit(self):
        job = self.prepare()
        transport = FakeSpeech(*[TimeoutError()] * 6)
        self.code(self.send, job, transport=transport)
        for _ in range(4):
            self.code(self.send, job, transport=transport, retry_uncertain=True, acknowledge_duplicate_billing=True)
        self.assertEqual(self.code(self.send, job, transport=transport, retry_uncertain=True,
                                   acknowledge_duplicate_billing=True), "speech_attempt_limit")
        self.assertEqual(len(transport.bodies), 5)

    def test_corrupted_job_records_are_refused(self):
        job = self.prepare()
        path = self.job_file(job)
        good = path.read_text()
        for broken in ("{", json.dumps({"contract": "grok_speech_job"})):
            path.write_text(broken)
            self.assertEqual(self.code(sj.inspect, job["job_id"], self.root), "speech_job_corrupt")
        edited = json.loads(good)
        edited["publishable"] = True
        path.write_text(json.dumps(edited))
        self.assertEqual(self.code(self.send, job), "speech_job_corrupt")
        self.assertEqual(self.tts.bodies, [])


class AudioValidationTests(SpeechBase):
    def outcome(self, audio, **prepare):
        job = self.prepare(**prepare)
        return self.send(job, transport=FakeSpeech(audio))

    def test_malformed_and_silent_audio_are_rejected(self):
        cases = {"speech_audio_malformed": [b"<html>error</html>", wav(seconds=2)[:30], b"RIFF\x00\x00\x00\x00WAVEjunk"],
                 "speech_audio_silent": [wav(seconds=3, value=0)]}
        voices = iter(("eve", "leo", "ara", "sal"))
        with patch.object(sj, "_ffmpeg", return_value=None):
            for code, payloads in cases.items():
                for payload in payloads:
                    record = self.outcome(payload, voice=next(voices))
                    self.assertEqual((record["status"], record["error_code"]), ("invalid_audio", code))
                    self.assertFalse(self.job_file(record, "narration.wav").exists())
                    with self.assertRaises(NetworkError) as caught:
                        sj.managed_audio(record["job_id"], self.root)
                    self.assertEqual(caught.exception.code, "speech_not_completed")
        mp3 = self.outcome(b"not an mp3 at all", codec="mp3")
        self.assertEqual((mp3["status"], mp3["error_code"]), ("invalid_audio", "speech_audio_malformed"))

    def test_mp3_without_a_local_decoder_waits_for_offline_recovery(self):
        fake_mp3 = b"ID3" + bytes(64)
        with patch.object(sj, "_ffmpeg", return_value=None):
            record = self.outcome(fake_mp3, codec="mp3")
            self.assertEqual((record["status"], record["error_code"]), ("received", "speech_decoder_unavailable"))
        with patch.object(sj, "_ffmpeg_decode", return_value=parse_wav(wav(seconds=4, rate=24000))):
            recovered = sj.recover(record["job_id"], root=self.root, clock=self.tick)
        self.assertEqual(recovered["status"], "completed")                  # converted locally, nothing resent

    def test_raw_audio_tampered_before_conversion(self):
        with patch.object(sj, "_ffmpeg", return_value=None):
            record = self.outcome(b"ID3" + bytes(64), codec="mp3")
        self.job_file(record, record["raw_audio"]["file"]).write_bytes(b"ID3changed")
        self.assertEqual(sj.recover(record["job_id"], root=self.root, clock=self.tick)["error_code"],
                         "speech_raw_audio_tampered")

    def test_overlong_speech_is_kept_and_never_used_or_regenerated(self):
        record = self.outcome(wav(seconds=16.25, rate=24000))
        self.assertEqual((record["status"], record["error_code"]), ("too_long", "narration_too_long"))
        self.assertEqual(record["audio"]["duration_seconds"], 16.25)
        kept = self.job_file(record, "narration.wav").read_bytes()
        self.assertEqual(len(parse_wav(kept)[2]), int(16.25 * 24000) * 2)  # every sample kept, nothing cut
        self.assertEqual(self.code(sj.managed_audio, record["job_id"], self.root), "speech_narration_too_long")
        self.assertEqual(self.code(self.send, record), "speech_submit_refused")   # no automatic new generation
        self.assertIn("Narration too long", sj.view(record)["next"][0])
        start = self.flow().start
        self.assertEqual(self.code(start, selection_run_id=self.run_id, record_id=self.record_id,
                                   speech_job=record["job_id"]), "speech_narration_too_long")
        self.nothing_created()


class TransportTests(unittest.TestCase):
    def setUp(self):
        patch.dict(os.environ, {"XAI_API_KEY": KEY}).start()
        self.addCleanup(patch.stopall)

    def opener(self, effect):
        opener = MagicMock()
        opener.open.side_effect = effect
        return patch.object(st, "build_opener", return_value=opener), opener

    def call(self, effect, max_bytes=1000):
        patcher, opener = self.opener(effect)
        with patcher:
            try:
                return st.transport({"text": "hi"}, timeout_seconds=5, max_bytes=max_bytes), opener
            except NetworkError as error:
                return error, opener

    def response(self, data, length=None):
        response = MagicMock()
        response.status = 200
        response.headers = {"Content-Type": "audio/wav; charset=binary", **({"Content-Length": str(length)} if length else {})}
        response.read.side_effect = [data, b""]
        response.__enter__.return_value = response
        return response

    def test_success_sends_the_key_only_in_the_header_to_the_fixed_url(self):
        result, opener = self.call(lambda request, timeout: self.response(b"RIFFdata"))
        self.assertEqual(result, {"content_type": "audio/wav", "audio": b"RIFFdata"})
        request = opener.open.call_args.args[0]
        self.assertEqual((request.full_url, request.get_method()), ("https://api.x.ai/v1/tts", "POST"))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertNotIn(KEY.encode(), request.data)

    def test_error_mapping_is_sanitized(self):
        def http(code):
            def raise_(request, timeout):
                raise HTTPError("https://api.x.ai/v1/tts", code, "private message", {}, io.BytesIO(b"private body"))
            return raise_
        for code in (400, 401, 404, 429):
            error, _ = self.call(http(code))
            self.assertIsInstance(error, st.SpeechRejected)
            self.assertEqual(error.http_status, code)
        for effect, expected in ((http(500), "speech_transport_failed"), (http(503), "speech_transport_failed"),
                                 (URLError(socket.gaierror()), "speech_not_sent"),
                                 (URLError(ConnectionRefusedError()), "speech_not_sent"),
                                 (URLError(TimeoutError()), "speech_transport_failed"),
                                 (TimeoutError("private"), "speech_transport_failed")):
            error, _ = self.call(effect)
            self.assertEqual(error.code, expected)
            self.assertNotIn("private", str(error))
            self.assertNotIn(KEY, str(error))

    def test_response_size_is_bounded(self):
        error, _ = self.call(lambda request, timeout: self.response(b"x" * 5000), max_bytes=1000)
        self.assertEqual(error.code, "speech_transport_failed")
        error, _ = self.call(lambda request, timeout: self.response(b"x", length=10 ** 9), max_bytes=1000)
        self.assertEqual(error.code, "speech_transport_failed")


class WorkflowTests(SpeechBase):
    def completed(self, job):
        record = self.send(job)
        self.assertEqual(record["status"], "completed")
        return record

    def test_speech_narration_reuses_clips_and_needs_fresh_quality_and_review(self):
        job = self.completed(self.production_job())
        silent_wid, silent_report = self.wid, self.report_id
        self.review()
        self.assertEqual(self.vp.resume(silent_wid)["status"], "ready_to_export")
        calls = (len(self.grok.posts), len(self.grok.status_checks), len(self.grok.downloads))

        view = self.flow().start(production_id=self.pid, speech_job=job["job_id"])
        self.vp, self.wid = self.flow(), view["workflow_id"]
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual(calls, (len(self.grok.posts), len(self.grok.status_checks), len(self.grok.downloads)))
        self.assertEqual(sorted(t["event"] for t in self.state()["trace"] if t["event"].startswith("reused_clip")),
                         [f"reused_clip_scene_{i}" for i in (1, 2, 3, 4)])
        managed = self.job_file(job, "narration.wav").read_bytes()
        self.assertEqual(self.renderer.narrations[-1], managed)               # the generated narration was mixed
        state = self.state()
        self.assertEqual(state["narration"]["speech"]["job_id"], job["job_id"])
        self.assertEqual(state["narration"]["speech"]["script_sha256"], job["script"]["script_sha256"])
        self.assertEqual(view["narration"]["speech"]["voice"], "eve")
        self.assertIn("AI-generated narration", view["narration"]["output"])

        # Old approvals do not authorize the narrated video.
        new_report = view["outputs"]["quality_report_id"]
        self.assertNotEqual(new_report, silent_report)
        self.assertEqual({r["report_id"]: r for r in reviewable(self.pid, self.root)}[silent_report]["binding"], "changed")
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "no_current_preview_approval")
        self.assertEqual(self.code(self.vp.export, silent_wid, purpose="approved_preview"), "production_preview_changed")
        with self.assertRaises(NetworkError):
            PreviewExporter(self.root, clock=self.tick).export(self.pid, silent_report, purpose="approved_preview")
        self.report_id = new_report
        self.review(supersedes=history(self.pid, self.root)["reviews"][0]["review_id"])
        exported = self.vp.export(self.wid, purpose="approved_preview")
        self.assertEqual((exported["status"], exported["export"]["publishable"]), ("exported", False))

    def test_script_mismatch_is_refused_before_any_paid_video_request(self):
        other = self.completed(self.prepare())                                  # from another script file
        self.to_review()
        posts = len(self.grok.posts)
        self.assertEqual(self.code(self.flow().start, production_id=self.pid, speech_job=other["job_id"]),
                         "speech_script_mismatch")
        self.assertEqual(vp.WorkflowStore(self.root).ids(), [self.wid])     # nothing created
        # From a selection, the production (and its script) exist only after the production stage:
        # the binding is checked there, before any video job is prepared.
        view = self.flow().start(selection_run_id=self.run_id, record_id=self.record_id, speech_job=other["job_id"])
        self.assertEqual(view["status"], "blocked")
        stages = {s["name"]: (s["status"], s["error_code"]) for s in view["stages"]}
        self.assertEqual(stages["scene_plan"], ("failed", "speech_script_mismatch"))
        self.assertEqual(stages["jobs"][0], "pending")
        self.assertTrue(all(s["job_id"] is None for s in view["scenes"]))
        self.assertEqual(len(self.grok.posts), posts)

    def test_production_script_changed_after_start_blocks_submission(self):
        job = self.completed(self.production_job())
        view = self.flow().start(production_id=self.pid, speech_job=job["job_id"])
        wid = view["workflow_id"]
        with patch.object(sj, "script_digest", return_value="0" * 64):
            for call in (lambda: self.flow().resume(wid),
                         lambda: self.flow().export(wid, purpose="review_copy"),
                         lambda: self.flow().submit(wid, 1, consent="paid-generate:x", allow_network=True)):
                self.assertEqual(self.code(call), "speech_script_mismatch")
            self.assertEqual(self.flow().inspect(wid)["integrity_problems"], ["speech_script_mismatch"])

    def test_tampered_speech_audio_is_refused(self):
        job = self.completed(self.production_job())
        path = self.job_file(job, "narration.wav")
        original = path.read_bytes()
        path.write_bytes(original[:100] + bytes([original[100] ^ 1]) + original[101:])
        self.assertEqual(self.code(self.flow().start, production_id=self.pid, speech_job=job["job_id"]),
                         "speech_audio_tampered")
        path.write_bytes(original)
        view = self.flow().start(production_id=self.pid, speech_job=job["job_id"])
        managed = self.wpath(vp.NARRATION_FILE, view["workflow_id"])
        managed.write_bytes(original[:100] + bytes([original[100] ^ 1]) + original[101:])   # the workflow's own copy
        self.assertEqual(self.code(self.flow().resume, view["workflow_id"]), "narration_tampered")

    def test_incomplete_job_conflicting_sources_and_unknown_job(self):
        job = self.production_job()
        start = self.flow().start
        self.assertEqual(self.code(start, production_id=self.pid, speech_job=job["job_id"]), "speech_not_completed")
        self.assertEqual(self.code(start, production_id=self.pid, speech_job="sp-" + "0" * 24), "speech_job_not_found")
        self.assertEqual(self.code(start, production_id=self.pid, speech_job=job["job_id"],
                                   narration=self.recording()), "narration_source_conflict")

    def test_local_and_silent_workflows_are_unchanged(self):
        self.to_review()
        self.assertNotIn("narration", self.state())
        view = self.flow().start(production_id=self.pid, narration=self.recording())
        self.assertNotIn("speech", self.state(view["workflow_id"])["narration"])
        self.assertEqual(view["narration"]["output"], "local narration mixed in; generated source audio muted")


class CliTests(SpeechBase):
    def cli(self, *argv):
        from vicekrack.speech_cli import main
        out = io.StringIO()
        with patch.object(sj, "base", lambda root=None: self.root / "runtime" / "speech-jobs"), \
                patch.object(sj, "http_transport", self.tts), redirect_stdout(out):
            code = main(list(argv))
        return code, json.loads(out.getvalue())

    def test_every_command_is_a_valid_choice(self):
        for command in ("speech-prepare", "speech-inspect", "speech-list", "speech-submit", "speech-recover"):
            result = subprocess.run([sys.executable, "-m", "vicekrack", command, "--help"], cwd=ROOT,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, (command, result.stderr))
        result = subprocess.run([sys.executable, "-m", "vicekrack", "video-production-start", "--help"], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertIn("--speech", result.stdout)

    def test_cli_prepare_inspect_submit_and_errors(self):
        code, job = self.cli("speech-prepare", "--script", str(GTA), "--voice", "Leo")
        self.assertEqual((code, job["status"], job["settings"]["voice"]), (0, "prepared", "leo"))
        self.assertTrue(any("(PAID)" in line for line in job["next"]))
        jid = job["job_id"]
        self.assertEqual(self.cli("speech-inspect", jid)[1]["text"], job["text"])
        self.assertEqual(self.cli("speech-list")[1]["speech_jobs"][0]["job_id"], jid)
        code, error = self.cli("speech-submit", jid, "--consent", "yes", "--allow-network")
        self.assertEqual((code, error["error"]["code"]), (1, "speech_consent_required"))
        self.assertEqual(self.tts.bodies, [])
        code, done = self.cli("speech-submit", jid, "--consent", f"paid-speech:{jid}", "--allow-network")
        self.assertEqual((code, done["status"]), (0, "completed"))
        self.assertTrue(done["next"][0].startswith("python -m vicekrack video-production-start"))
        self.assertEqual(self.cli("speech-recover", jid)[1]["status"], "completed")
        code, error = self.cli("speech-prepare", "--script", str(self.root / "missing.json"))
        self.assertEqual((code, error["error"]["code"]), (1, "script_not_found"))
        self.assertNotIn(str(self.root), json.dumps(error))
        self.tts.effects.append(wav(seconds=16, rate=24000))
        code, job = self.cli("speech-prepare", "--script", str(GTA), "--voice", "sal")
        code, long = self.cli("speech-submit", job["job_id"], "--consent", f"paid-speech:{job['job_id']}",
                              "--allow-network")
        self.assertEqual((code, long["status"]), (1, "too_long"))

    def test_cli_start_with_speech_and_demo_dispatch(self):
        job = self.send(self.production_job())
        from vicekrack.video_cli import main
        out = io.StringIO()
        flow = self.flow()
        with patch("vicekrack.video_production.VideoProduction", lambda: flow), redirect_stdout(out):
            self.assertEqual(main(["video-production-start", "--production", self.pid, "--speech", job["job_id"]]), 0)
        self.assertEqual(json.loads(out.getvalue())["narration"]["speech"]["job_id"], job["job_id"])
        out = io.StringIO()
        with patch("vicekrack.speech_demo.run_speech_demo", return_value={"demo": True}) as demo, redirect_stdout(out):
            self.assertEqual(main(["video-production-demo", "--speech", "--output", str(self.root / "demo")]), 0)
        demo.assert_called_once_with(self.root / "demo")

    def test_step46_script_exists(self):
        import importlib.util
        from vicekrack.speech_demo import run_speech_demo
        spec = importlib.util.spec_from_file_location("step46_demo", ROOT / "scripts/step46_demo.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.run_speech_demo, run_speech_demo)


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "set RUN_LOCAL_RENDER_TESTS=1 for real FFmpeg")
class RealDecoderTests(SpeechBase):
    def test_mp3_is_decoded_locally_with_metadata_removed(self):
        from vicekrack.preview import dependencies
        source = self.root / "tone.wav"
        source.write_bytes(wav(seconds=5, rate=24000, value=3000))
        mp3 = self.root / "tone.mp3"
        subprocess.run([dependencies()[3], "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-metadata",
                        "title=PRIVATE-TAG", "-c:a", "libmp3lame", "-b:a", "128k", str(mp3)], check=True, timeout=60)
        self.assertIn(b"PRIVATE-TAG", mp3.read_bytes())
        job = self.prepare(codec="mp3")
        record = self.send(job, transport=FakeSpeech(mp3.read_bytes()))
        self.assertEqual(record["status"], "completed")
        managed = self.job_file(job, "narration.wav").read_bytes()
        self.assertNotIn(b"PRIVATE-TAG", managed)
        self.assertEqual(managed[36:40], b"data")                         # canonical header, no other chunks
        self.assertAlmostEqual(record["audio"]["duration_seconds"], 5, delta=0.1)
        self.assertEqual(record["audio"]["sample_rate"], 24000)


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "set RUN_LOCAL_RENDER_TESTS=1 for the real demo")
class OfflineSpeechDemoTests(unittest.TestCase):
    def test_speech_demo_runs_end_to_end_with_the_network_blocked(self):
        from vicekrack.media_render import inspect_video
        from vicekrack.speech_demo import run_speech_demo
        with tempfile.TemporaryDirectory() as temp, \
                patch("socket.socket.connect", side_effect=AssertionError("network disabled in tests")):
            os.environ.pop("XAI_API_KEY", None)
            with patch("vicekrack.review._clock_now", return_value=datetime(2027, 3, 1, tzinfo=timezone.utc).timestamp()):
                summary = run_speech_demo(Path(temp) / "demo")
            self.assertNotIn("XAI_API_KEY", os.environ)
            self.assertEqual((summary["status"], summary["quality_result"]), ("exported", "pass"))
            self.assertTrue(summary["export_verified"], summary["export_problems"])
            self.assertIs(summary["publishable"], False)
            self.assertEqual(summary["paid_requests"], 0)
            self.assertIn("NOT a Grok voice", summary["speech_audio"])
            self.assertEqual(summary["mock_counts"], {"speech_calls": 4, "video_posts": 4, "video_downloads": 4})
            checks = {p["check"] for p in summary["proofs"]}
            for check in ("prepare_is_offline_and_reviewable", "timeout_leaves_speech_job_uncertain",
                          "speech_retry_without_duplicate_billing_ack", "acknowledged_retry_then_local_conversion",
                          "narration_too_long_kept_never_truncated", "narration_from_another_script_refused",
                          "changed_speech_audio_refused", "paid_clips_reused_no_new_video_request",
                          "silent_approval_does_not_authorize_narrated_video", "narrated_video_needs_its_own_review",
                          "simulated_review_recorded_by_demo_not_workflow",
                          "narration_mixed_padded_to_15s_clip_audio_muted"):
                self.assertIn(check, checks)
            video = Path(summary["video"])
            self.assertTrue(video.is_relative_to((Path(temp) / "demo").resolve()))
            info = inspect_video(video)
            self.assertEqual((info["width"], info["height"]), (1080, 1920))
            self.assertAlmostEqual(info["duration"], 15, delta=0.2)
            self.assertAlmostEqual(summary["audio"]["seconds"], 15, delta=0.1)
            self.assertEqual((Path(summary["export_package"]) / "media/preview.mp4").read_bytes(), video.read_bytes())


if __name__ == "__main__":
    unittest.main()
