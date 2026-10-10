"""Step 47 offline demo: narration-aligned, burned-in captions, with NO network and NO credits.

Runs in a fresh demo folder (default runtime/video-production-demo/captions-<run>/), which is the
root for every store, with every socket connection blocked:

1. mock story -> verification -> selection -> a silent Step 44 workflow (mock Grok clips, real
   local render): the production and four paid (mock) clips;
2. a speech job prepared WITH TIMESTAMPS from the production's saved script; the MOCK xAI reply is
   the documented JSON envelope carrying SYNTHETIC TEST AUDIO (tone bursts written by Python) and
   SYNTHETIC character timings spread over it. It is NOT a Grok voice and NOT real alignment;
3. two caption tracks from that job: provider timing and (explicitly) estimated timing; proofs that
   out-of-order provider timings, another script and a changed sidecar are refused;
4. a captioned workflow: clips reused (no new video request), narration mixed in, captions burned
   in, fresh quality report, SIMULATED review, verified export with SRT/WebVTT sidecars;
5. an estimated-timing workflow: a different video, so the earlier approval no longer authorizes
   export, and its quality report is needs_review (caption_timing_estimated);
6. representative frames are extracted into <demo>/frames/ for visual inspection.
"""

import base64
import json
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .errors import NetworkError
from .orchestrator import ROOT
from .speech_demo import synthetic_speech_wav
from .video_production_demo import (DEMO_NOTE, DEMO_REVIEWER, MockGrok, _expect_refusal, _story, demo_clock,
                                    loudness, network_blocked, placeholder_credential, review_time)

SYNTHETIC_LABEL = ("SYNTHETIC TEST AUDIO (tone bursts) with SYNTHETIC character timings, standing in for an xAI "
                   "timestamped response; NOT a Grok voice and NOT real speech alignment")


class MockTimedSpeech:
    """Stands in for POST /v1/tts with with_timestamps=true: the documented JSON envelope."""

    def __init__(self, folder, seconds=10.5):
        self.folder, self.seconds, self.calls = folder, seconds, 0

    def __call__(self, body, *, timeout_seconds, max_bytes):
        self.calls += 1
        rate = body["output_format"]["sample_rate"]
        audio = synthetic_speech_wav(self.folder, self.seconds, rate)
        text = body["text"]
        span = self.seconds - 0.5
        times = [[round(0.2 + span * i / len(text), 3), round(0.2 + span * (i + 1) / len(text), 3)]
                 for i in range(len(text))]
        envelope = {"audio": base64.b64encode(audio).decode("ascii"), "content_type": "audio/wav",
                    "duration": self.seconds,
                    "audio_timestamps": {"graph_chars": list(text), "graph_times": times}}
        return {"content_type": "application/json", "audio": json.dumps(envelope).encode("utf-8")}


def _frame(video, seconds, target):
    from .preview import dependencies, invoke
    exe = dependencies()[3]
    invoke(exe, ["-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1", str(target)], Path(target).parent)
    return target


def run_caption_demo(output=None, clock=None):
    from . import captions, speech_jobs as speech
    from .export import verify_package
    from .production import ProductionStore
    from .quality import load_report, probe_media
    from .review import ReviewRecorder, history, reviewable
    from .video_production import VideoProduction
    clock = clock or demo_clock()
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = ROOT / "runtime" / "video-production-demo" / f"captions-{stamp}-{secrets.token_hex(3)}"
    root = Path(output).resolve()
    if root.exists() and any(root.iterdir()):
        raise NetworkError("demo_folder_not_empty", "Choose an empty or new demo folder.")
    root.mkdir(parents=True, exist_ok=True)
    proofs = []

    def review(production_id, report_id):
        row = next(r for r in reviewable(production_id, root) if r["report_id"] == report_id)
        earlier = history(production_id, root)["reviews"]
        return ReviewRecorder(root, clock=clock).record(
            production_id, report_id, decision="approved_for_preview", reviewer=DEMO_REVIEWER,
            binding=row["binding_digest"], acknowledgments=row["applicable_acknowledgments"], notes=DEMO_NOTE,
            supersedes=earlier[0]["review_id"] if earlier else None)[0]

    def preview_video(production_id):
        state = ProductionStore(root).read(production_id)
        artifacts = next(s for s in state["stages"] if s["name"] == "preview")["artifacts"]
        return ProductionStore(root).folder(production_id) / artifacts["preview_file"]

    with network_blocked(), placeholder_credential(), review_time(clock), tempfile.TemporaryDirectory() as scratch:
        grok = MockGrok(scratch, fail_first_post_numbers=(), pending_once=())
        tts = MockTimedSpeech(scratch)
        flow = VideoProduction(root=root, clock=clock, transport=grok)

        # 1. production + paid (mock) clips
        run_id, record_id = _story(root, clock)
        silent = flow.start(selection_run_id=run_id, record_id=record_id)
        for scene in silent["scenes"]:
            flow.submit(silent["workflow_id"], scene["scene_index"], consent=f"paid-generate:{scene['job_id']}",
                        allow_network=True)
        silent = flow.resume(silent["workflow_id"], allow_network=True)
        production_id = silent["production_id"]
        video_posts = grok.posts

        # 2. timestamped speech job (explicit, mock-paid once)
        script = speech.production_script(production_id, root)
        job = speech.prepare(script, source="production", production_id=production_id, with_timestamps=True,
                             root=root, clock=clock)
        job = speech.submit(job["job_id"], consent=job["consent_phrase"], allow_network=True, transport=tts,
                            root=root, clock=clock)
        assert job["status"] == "completed" and job["timestamps"]["characters"] == len(job["text"])

        # 3. caption tracks and refusals
        provider = captions.prepare(job["job_id"], timing="provider", root=root)
        estimated = captions.prepare(job["job_id"], timing="estimated", root=root)
        assert provider["timing"]["requires_manual_timing_review"] is False
        assert estimated["timing"]["requires_manual_timing_review"] is True
        assert " ".join(c["text"] for c in provider["cues"]).split() == job["text"].split()
        proofs.append({"check": "captions_repeat_the_spoken_text_exactly", "cues": len(provider["cues"]),
                       "words": len(job["text"].split())})
        wav, record = speech.managed_audio(job["job_id"], root)
        stamps = speech.timestamps(job["job_id"], root)
        swapped = json.loads(json.dumps(stamps))
        times = swapped["data"]["graph_times"]
        times[10], times[11] = times[11], times[10]
        _expect_refusal(lambda: captions.build_track(script, record, wav, method="provider", timestamps=swapped),
                        "caption_timing_out_of_order", proofs, "out_of_order_provider_timing_refused")
        other = json.loads((ROOT / "examples/short-script-gta.json").read_text(encoding="utf-8"))
        _expect_refusal(lambda: captions.build_track(other, record, wav, method="estimated"),
                        "caption_script_mismatch", proofs, "captions_from_another_script_refused")
        sidecar = captions.folder(provider["caption_id"], root) / "captions.srt"
        original = sidecar.read_bytes()
        sidecar.write_bytes(original.replace(b"-->", b"--> ", 1))
        try:
            _expect_refusal(lambda: captions.load(provider["caption_id"], root), "captions_tampered", proofs,
                            "changed_caption_sidecar_refused")
        finally:
            sidecar.write_bytes(original)

        # 4. captioned workflow (provider timing): reuse clips, render, quality, simulated review, export
        captioned = flow.start(production_id=production_id, speech_job=job["job_id"], captions=provider["caption_id"])
        workflow_id = captioned["workflow_id"]
        assert captioned["status"] == "waiting_for_review", captioned["status"]
        assert grok.posts == video_posts
        proofs.append({"check": "paid_clips_and_speech_reused_no_new_request", "video_posts": grok.posts,
                       "speech_calls": tts.calls})
        report = load_report(captioned["outputs"]["quality_report_id"], root)
        assert report["result"] == "pass", report["reasons"]
        approval = review(production_id, report["report_id"])
        proofs.append({"check": "simulated_review_recorded_by_demo_not_workflow", "reviewer": DEMO_REVIEWER})
        assert flow.resume(workflow_id)["status"] == "ready_to_export"
        exported = flow.export(workflow_id, purpose="approved_preview")
        package = Path(exported["export"]["path"])
        verification = verify_package(package)
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
        video = preview_video(production_id)
        assert (package / "media/preview.mp4").read_bytes() == video.read_bytes()
        kept_video = root / "captioned-preview.mp4"
        kept_video.write_bytes(video.read_bytes())

        frames = root / "frames"
        frames.mkdir()
        shots = []
        for cue in (provider["cues"][0], provider["cues"][len(provider["cues"]) // 2], provider["cues"][-1]):
            middle = (cue["start_ms"] + cue["end_ms"]) / 2000
            shots.append(str(_frame(video, middle, frames / f"provider-cue-{cue['index']:02d}.png")))
        shots.append(str(_frame(video, 14.5, frames / "after-narration.png")))
        info = probe_media(video)
        speech_db, tail_db = loudness(video, 1.0, 8.5), loudness(video, 12.2, 2.5)
        assert info["audio_present"] and abs(info["audio_duration"] - 15) <= 0.1 and speech_db > -30 and tail_db < -60

        # 5. a caption change is a different video: the earlier approval stops applying
        changed = flow.start(production_id=production_id, speech_job=job["job_id"], captions=estimated["caption_id"])
        assert changed["status"] == "waiting_for_review" and grok.posts == video_posts
        _expect_refusal(lambda: flow.export(workflow_id, purpose="approved_preview"), "production_preview_changed",
                        proofs, "caption_change_invalidates_earlier_approval")
        estimated_report = load_report(changed["outputs"]["quality_report_id"], root)
        assert estimated_report["result"] == "needs_review" and "caption_timing_estimated" in estimated_report["reasons"]
        proofs.append({"check": "estimated_timing_is_needs_review", "reasons": ["caption_timing_estimated"]})
        _expect_refusal(lambda: flow.export(changed["workflow_id"], purpose="approved_preview"),
                        "no_current_preview_approval", proofs, "estimated_captions_need_their_own_review")
        estimated_video = preview_video(production_id)
        middle = (estimated["cues"][1]["start_ms"] + estimated["cues"][1]["end_ms"]) / 2000
        shots.append(str(_frame(estimated_video, middle, frames / "estimated-cue-02.png")))

        return {
            "demo": True, "network": "blocked for the whole run", "paid_requests": 0,
            "speech_provider": "MOCK (no xAI request was made)", "speech_audio": SYNTHETIC_LABEL,
            "video_provider": "MOCK (no xAI request was made)",
            "media": "synthetic test clips generated locally with ffmpeg",
            "review": "SIMULATED by the demo, clearly labelled; the workflow itself never records reviews",
            "speech_job_id": job["job_id"], "spoken_text": job["text"],
            "caption_track": provider["caption_id"], "caption_timing": provider["timing"]["method"],
            "cues": [{"start": c["start_ms"] / 1000, "end": c["end_ms"] / 1000, "lines": c["lines"]}
                     for c in provider["cues"]],
            "estimated_track": estimated["caption_id"],
            "workflow_id": workflow_id, "quality_result": report["result"], "review_id": approval["review_id"],
            "video": str(kept_video), "export_package": str(package),
            "sidecars": [str(package / "captions/captions.srt"), str(package / "captions/captions.vtt")],
            "export_verified": verification["status"] == "consistent" and not verification["problems"],
            "export_problems": verification["problems"], "publishable": manifest["restrictions"]["publishable"],
            "package_captions": manifest.get("captions"),
            "audio": {"codec": info["audio_codec"], "seconds": info["audio_duration"], "speech_peak_db": speech_db,
                      "after_narration_peak_db": tail_db},
            "frames": shots, "estimated_workflow_id": changed["workflow_id"],
            "mock_counts": {"speech_calls": tts.calls, "video_posts": grok.posts, "video_downloads": grok.downloads},
            "proofs": proofs, "demo_folder": str(root),
        }
