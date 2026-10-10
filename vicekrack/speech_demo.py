"""Step 46 offline demo: Grok-generated narration in the video-production workflow, with NO network and
NO credits.

Everything runs in a fresh demo folder (default runtime/video-production-demo/speech-<run>/), which
is the root for every store. Every socket connection is blocked. The xAI speech API is replaced by
a MOCK that returns SYNTHETIC TEST AUDIO (tone bursts written by Python, with an extra metadata
chunk). It is NOT a Grok voice sample and must never be presented as one.

1. mock story -> verification -> selection -> a silent Step 44 workflow (mock Grok clips, real
   local render, quality report) and a SIMULATED approval of that silent preview;
2. validated script -> `speech_jobs.prepare` from the production's saved script (offline);
   wrong consent and missing --allow-network are refused; the first mocked request "times out",
   so the job is uncertain; a plain resubmit and a retry without the duplicate-billing
   acknowledgment are refused; the acknowledged retry receives the synthetic audio, which is
   converted locally into a metadata-free 16-bit PCM WAV;
3. proofs: an over-15-second response is kept but marked too long and refused by the workflow;
   a speech job made from a different script is refused (speech_script_mismatch); a changed
   managed WAV is refused (speech_audio_tampered);
4. `video-production-start --production P --speech JOB`: the four paid clips are REUSED (no new
   video request), the narrated preview is rendered for real, a fresh quality report runs, the old
   silent approval no longer authorizes anything, then a SIMULATED review and a verified export.
"""

import json
import secrets
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .errors import NetworkError
from .orchestrator import ROOT
from .video_production_demo import (DEMO_NOTE, DEMO_REVIEWER, MockGrok, _expect_refusal, _story, demo_clock,
                                    loudness, network_blocked, placeholder_credential, review_time,
                                    synthetic_narration)

SYNTHETIC_LABEL = "SYNTHETIC TEST AUDIO (tone bursts written by the demo) standing in for an xAI response; NOT a Grok voice"


def synthetic_speech_wav(folder, seconds, rate):
    """Synthetic tone bursts as a WAV with an extra LIST/INFO metadata chunk (which must be stripped)."""
    from .narration import parse_wav
    path = synthetic_narration(Path(folder) / f"synthetic-{seconds}s-{rate}.wav", seconds=seconds, rate=rate)
    channels, sample_rate, pcm = parse_wav(Path(path).read_bytes())
    info = b"INFO" + b"ISFT" + struct.pack("<I", 14) + b"mock-provider\x00"
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, sample_rate * channels * 2, channels * 2, 16)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"LIST" + struct.pack("<I", len(info)) + info
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
    return b"RIFF" + struct.pack("<I", len(body)) + body


class MockSpeech:
    """Stands in for POST https://api.x.ai/v1/tts. Never opens a connection; counts every call."""

    def __init__(self, folder, fail_first=1, long_voices=("ara",)):
        self.folder, self.fail_first, self.long_voices = folder, fail_first, set(long_voices)
        self.calls = 0

    def __call__(self, body, *, timeout_seconds, max_bytes):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise TimeoutError("simulated timeout: the provider may or may not have generated (and billed) the audio")
        seconds = 16.5 if body["voice_id"] in self.long_voices else 10.5
        return {"content_type": "audio/wav",
                "audio": synthetic_speech_wav(self.folder, seconds, body["output_format"]["sample_rate"])}


def run_speech_demo(output=None, clock=None):
    from . import speech_jobs as speech
    from .export import verify_package
    from .narration import parse_wav
    from .production import ProductionStore
    from .quality import probe_media
    from .review import ReviewRecorder, history, reviewable
    from .video_production import VideoProduction
    clock = clock or demo_clock()
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = ROOT / "runtime" / "video-production-demo" / f"speech-{stamp}-{secrets.token_hex(3)}"
    root = Path(output).resolve()
    if root.exists() and any(root.iterdir()):
        raise NetworkError("demo_folder_not_empty", "Choose an empty or new demo folder.")
    root.mkdir(parents=True, exist_ok=True)
    proofs = []

    def review(production_id, report_id):
        row = next(r for r in reviewable(production_id, root) if r["report_id"] == report_id)
        earlier = history(production_id, root)["reviews"]          # one append-only history per production
        return ReviewRecorder(root, clock=clock).record(
            production_id, report_id, decision="approved_for_preview", reviewer=DEMO_REVIEWER,
            binding=row["binding_digest"], acknowledgments=row["applicable_acknowledgments"], notes=DEMO_NOTE,
            supersedes=earlier[0]["review_id"] if earlier else None)[0]

    with network_blocked(), placeholder_credential(), review_time(clock), tempfile.TemporaryDirectory() as scratch:
        grok = MockGrok(scratch, fail_first_post_numbers=(), pending_once=())
        tts = MockSpeech(scratch)
        flow = VideoProduction(root=root, clock=clock, transport=grok)

        # 1. A silent workflow: the production, four paid (mock) clips, a silent preview and its approval.
        run_id, record_id = _story(root, clock)
        silent = flow.start(selection_run_id=run_id, record_id=record_id)
        silent_id = silent["workflow_id"]
        for scene in silent["scenes"]:
            flow.submit(silent_id, scene["scene_index"], consent=f"paid-generate:{scene['job_id']}", allow_network=True)
        silent = flow.resume(silent_id, allow_network=True)
        assert silent["status"] == "waiting_for_review", silent["status"]
        production_id = silent["production_id"]
        review(production_id, silent["outputs"]["quality_report_id"])
        assert flow.resume(silent_id)["status"] == "ready_to_export"
        video_posts = grok.posts

        # 2. Validated script -> reviewable speech request (offline) -> explicit paid (mock) submission.
        script = speech.production_script(production_id, root)
        job = speech.prepare(script, source="production", production_id=production_id, root=root, clock=clock)
        job_id = job["job_id"]
        assert job["text"] == speech.narration_text(script) and tts.calls == 0
        proofs.append({"check": "prepare_is_offline_and_reviewable", "spoken_text": job["text"],
                       "voice": job["settings"]["voice"], "consent_phrase": job["consent_phrase"]})
        _expect_refusal(lambda: speech.submit(job_id, consent="paid-speech:sp-" + "0" * 24, allow_network=True,
                                              transport=tts, root=root, clock=clock),
                        "speech_consent_required", proofs, "speech_consent_for_another_job")
        _expect_refusal(lambda: speech.submit(job_id, consent=job["consent_phrase"], allow_network=False,
                                              transport=tts, root=root, clock=clock),
                        "network_not_allowed", proofs, "speech_submit_without_allow_network")
        assert tts.calls == 0
        _expect_refusal(lambda: speech.submit(job_id, consent=job["consent_phrase"], allow_network=True,
                                              transport=tts, root=root, clock=clock),
                        "speech_submit_uncertain", proofs, "timeout_leaves_speech_job_uncertain")
        _expect_refusal(lambda: speech.submit(job_id, consent=job["consent_phrase"], allow_network=True,
                                              transport=tts, root=root, clock=clock),
                        "speech_submit_refused", proofs, "plain_resubmit_of_uncertain_speech_refused")
        _expect_refusal(lambda: speech.submit(job_id, consent=job["consent_phrase"], allow_network=True,
                                              retry_uncertain=True, transport=tts, root=root, clock=clock),
                        "duplicate_billing_ack_required", proofs, "speech_retry_without_duplicate_billing_ack")
        assert tts.calls == 1
        job = speech.submit(job_id, consent=job["consent_phrase"], allow_network=True, retry_uncertain=True,
                            acknowledge_duplicate_billing=True, transport=tts, root=root, clock=clock)
        assert job["status"] == "completed", job["status"]
        managed = (speech.folder(job_id, root) / speech.MANAGED).read_bytes()
        raw = (speech.folder(job_id, root) / job["raw_audio"]["file"]).read_bytes()
        assert b"LIST" in raw and b"LIST" not in managed and len(managed) == 44 + len(parse_wav(managed)[2])
        proofs.append({"check": "acknowledged_retry_then_local_conversion", "speech_calls": tts.calls,
                       "managed_wav_bytes": len(managed), "metadata_stripped": True,
                       "duration_seconds": job["audio"]["duration_seconds"]})

        # 3. Refusals: too long, wrong script, changed audio.
        long_job = speech.prepare(script, source="production", production_id=production_id, voice="ara",
                                  root=root, clock=clock)
        long_job = speech.submit(long_job["job_id"], consent=long_job["consent_phrase"], allow_network=True,
                                 transport=tts, root=root, clock=clock)
        assert long_job["status"] == "too_long" and (speech.folder(long_job["job_id"], root) / speech.MANAGED).is_file()
        _expect_refusal(lambda: flow.start(production_id=production_id, speech_job=long_job["job_id"]),
                        "speech_narration_too_long", proofs, "narration_too_long_kept_never_truncated")
        other_script = json.loads((ROOT / "examples/short-script-gta.json").read_text(encoding="utf-8"))
        other = speech.prepare(other_script, root=root, clock=clock)
        other = speech.submit(other["job_id"], consent=other["consent_phrase"], allow_network=True, transport=tts,
                              root=root, clock=clock)
        _expect_refusal(lambda: flow.start(production_id=production_id, speech_job=other["job_id"]),
                        "speech_script_mismatch", proofs, "narration_from_another_script_refused")
        managed_path = speech.folder(job_id, root) / speech.MANAGED
        managed_path.write_bytes(managed[:1000] + bytes([managed[1000] ^ 1]) + managed[1001:])   # one sample changed
        try:
            _expect_refusal(lambda: flow.start(production_id=production_id, speech_job=job_id),
                            "speech_audio_tampered", proofs, "changed_speech_audio_refused")
        finally:
            managed_path.write_bytes(managed)
        assert grok.posts == video_posts

        # 4. The narrated workflow: clips reused, narrated preview, fresh quality, old approval stale.
        narrated = flow.start(production_id=production_id, speech_job=job_id)
        workflow_id = narrated["workflow_id"]
        assert narrated["status"] == "waiting_for_review", narrated["status"]
        assert grok.posts == video_posts and grok.downloads == 4
        proofs.append({"check": "paid_clips_reused_no_new_video_request", "video_posts": grok.posts,
                       "video_downloads": grok.downloads})
        _expect_refusal(lambda: flow.export(silent_id, purpose="approved_preview"), "production_preview_changed",
                        proofs, "silent_approval_does_not_authorize_narrated_video")
        _expect_refusal(lambda: flow.export(workflow_id, purpose="approved_preview"), "no_current_preview_approval",
                        proofs, "narrated_video_needs_its_own_review")
        report_id = narrated["outputs"]["quality_report_id"]
        approval = review(production_id, report_id)
        proofs.append({"check": "simulated_review_recorded_by_demo_not_workflow", "reviewer": DEMO_REVIEWER})
        assert flow.resume(workflow_id)["status"] == "ready_to_export"
        exported = flow.export(workflow_id, purpose="approved_preview")
        package = Path(exported["export"]["path"])
        verification = verify_package(package)
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))

        state = ProductionStore(root).read(production_id)
        preview = next(s for s in state["stages"] if s["name"] == "preview")["artifacts"]
        video = ProductionStore(root).folder(production_id) / preview["preview_file"]
        assert (package / "media" / "preview.mp4").read_bytes() == video.read_bytes()
        info = probe_media(video)
        speech_db, tail_db = loudness(video, 1.0, 8.5), loudness(video, 12.2, 2.5)
        assert info["audio_present"] and abs(info["audio_duration"] - 15) <= 0.1, info
        assert speech_db > -30 and tail_db < -60, (speech_db, tail_db)
        proofs.append({"check": "narration_mixed_padded_to_15s_clip_audio_muted", "speech_peak_db": speech_db,
                       "after_narration_peak_db": tail_db})
        final = flow.inspect(workflow_id)
        return {
            "demo": True, "network": "blocked for the whole run", "paid_requests": 0,
            "speech_provider": "MOCK (no xAI request was made)", "speech_audio": SYNTHETIC_LABEL,
            "video_provider": "MOCK (no xAI request was made)",
            "media": "synthetic test clips generated locally with ffmpeg",
            "review": "SIMULATED by the demo, clearly labelled; the workflow itself never records reviews",
            "speech_job_id": job_id, "spoken_text": job["text"], "voice": job["settings"]["voice"],
            "narration_seconds": job["audio"]["duration_seconds"], "padded_to_seconds": 15,
            "workflow_id": workflow_id, "status": final["status"], "production_id": production_id,
            "quality_report_id": report_id, "quality_result": final["outputs"]["quality_result"],
            "review_id": approval["review_id"], "video": str(video), "export_package": str(package),
            "export_verified": verification["status"] == "consistent" and not verification["problems"],
            "export_problems": verification["problems"],
            "publishable": manifest["restrictions"]["publishable"],
            "audio": {"codec": info["audio_codec"], "seconds": info["audio_duration"], "speech_peak_db": speech_db,
                      "after_narration_peak_db": tail_db},
            "mock_counts": {"speech_calls": tts.calls, "video_posts": grok.posts, "video_downloads": grok.downloads},
            "proofs": proofs, "demo_folder": str(root),
        }
