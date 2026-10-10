"""Step 44 offline demo: the whole video-production workflow with NO network and NO credits.

It runs inside a fresh demo folder (default runtime/video-production-demo/<run>/), which is the
`root` for every store, so it never touches your real productions, jobs, reviews or exports:

1. mock story intake -> verification -> selection (the shipped *.mock.json configs);
2. the Step 21 production pipeline with the mock Creator and the REAL local renderer;
3. `video-production-start`: scene plan + four prepared Grok jobs (nothing is sent);
4. four explicit, job-specific submissions to a MOCK provider. The first request for scene 2
   "times out", so the workflow pauses as uncertain; a plain resubmit and a retry without the
   duplicate-billing acknowledgment are both refused, then an explicit acknowledged retry runs;
5. bounded resumes: scene 3 is still "pending" on the first status check, so the workflow
   waits; the next resume downloads four SYNTHETIC clips (generated locally with ffmpeg),
   writes the media manifest, re-renders the production preview for real and runs a real
   quality report;
6. a SIMULATED review, labelled "SIMULATED DEMO REVIEWER (not a person)" and confined to the
   demo folder, then an approved_preview export through the unchanged Step 38 gates;
7. a final resume proves completed stages and paid submissions are never repeated, and a held
   workflow lock proves a concurrent run is refused.

Every socket connection is blocked for the whole run. The mock provider only ever receives
the request body ViceKrack built; a placeholder (not a key) satisfies the XAI_API_KEY check.

Step 45 (`narrated=True`, `python -m vicekrack video-production-demo --narrated` or
`scripts/step45_demo.py`): the same run with a SYNTHETIC local narration (tone bursts written
by Python, not a voice). The synthetic clips then carry their own loud tone, so the demo can
measure that the generated source audio is muted. It also proves that overlong and all-silent
narration are refused before anything is created, that the managed copy is hash-checked on
resume, and that the exported MP4 has 15 seconds of narration audio (speech where the narration
is, silence after it).
"""

import json
import math
import os
import re
import secrets
import struct
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import NetworkError
from .orchestrator import ROOT

DEMO_REVIEWER = "SIMULATED DEMO REVIEWER (not a person)"
DEMO_EPOCH = datetime(2026, 10, 4, 13, 0, 0, tzinfo=timezone.utc)   # matches the dated mock story fixtures
DEMO_NOTE = "Simulated by the offline demo. Not a human decision; demo data only."
PLACEHOLDER = "offline-demo-placeholder-not-a-key"


@contextmanager
def network_blocked():
    def refuse(*args, **kwargs):
        raise OSError("network disabled by the offline video-production demo")
    saved = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
    socket.socket.connect, socket.socket.connect_ex, socket.create_connection = refuse, refuse, refuse
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = saved


@contextmanager
def placeholder_credential():
    previous = os.environ.get("XAI_API_KEY")
    os.environ["XAI_API_KEY"] = PLACEHOLDER
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("XAI_API_KEY", None)
        else:
            os.environ["XAI_API_KEY"] = previous


def synthetic_clip(duration, index, folder, with_audio=False):
    """A real, decodable 720x1280 (9:16) H.264 clip made locally; clearly synthetic test media.
    `with_audio` adds a loud 440 Hz tone (like a provider's generated sound) that must be muted."""
    from .preview import dependencies
    exe = dependencies()[3]
    colors = ("0x1d4ed8", "0x047857", "0xb45309", "0x7c3aed")
    target = Path(folder) / f"synthetic-{index}.mp4"
    audio = (["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}"] if with_audio else [])
    encode = (["-map", "[v]", "-map", "2:a", "-c:a", "aac", "-shortest"] if with_audio else ["-map", "[v]", "-an"])
    subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=720x1280:rate=24:duration={duration}", "-f", "lavfi", "-i",
                    f"color=c={colors[(index - 1) % 4]}:size=720x1280:rate=24:duration={duration}", *audio,
                    "-filter_complex", "[0:v][1:v]blend=all_mode=overlay:all_opacity=0.5,format=yuv420p[v]",
                    *encode, "-c:v", "libx264", "-preset", "ultrafast", str(target)],
                   check=True, capture_output=True, timeout=120)
    return target.read_bytes()


def synthetic_narration(path, seconds=11.5, rate=22050):
    """A 16-bit mono PCM WAV of 'syllable' tone bursts. Synthetic test audio, NOT a voice or a recording."""
    frames = []
    for n in range(int(seconds * rate)):
        t = n / rate
        syllable = t % 0.42                                   # a burst roughly every 0.42 s, with short gaps
        envelope = math.sin(math.pi * syllable / 0.32) if syllable < 0.32 else 0.0
        pitch = 180 + 60 * math.sin(2 * math.pi * 0.7 * t)    # a gently moving "voice" pitch
        value = envelope * (0.55 * math.sin(2 * math.pi * pitch * t) + 0.25 * math.sin(4 * math.pi * pitch * t))
        frames.append(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 20000)))
    pcm = b"".join(frames)
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2,
                         16, b"data", len(pcm))
    Path(path).write_bytes(header + pcm)
    return Path(path)


def silent_wav(path, seconds=3, rate=16000):
    pcm = bytes(seconds * rate * 2)
    Path(path).write_bytes(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1, rate,
                                       rate * 2, 2, 16, b"data", len(pcm)) + pcm)
    return Path(path)


def loudness(video, start, length):
    """Peak level (dBFS) of the MP4's audio between start and start+length, decoded locally."""
    from .preview import dependencies
    exe = dependencies()[3]
    done = subprocess.run([exe, "-hide_banner", "-nostdin", "-ss", str(start), "-t", str(length), "-i", str(video),
                           "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, timeout=120, check=True)
    found = re.search(r"max_volume: (-?[0-9.]+|-inf) dB", done.stderr.decode("utf-8", "replace"))
    if found is None:
        raise AssertionError("demo could not measure the narration audio")
    return -999.0 if found.group(1) == "-inf" else float(found.group(1))


class MockGrok:
    """Stands in for xAI's video API. Never opens a connection; keeps counts for the demo report."""

    def __init__(self, clip_folder, fail_first_post_numbers=(2,), pending_once=("demo-request-3",), clip_audio=False):
        from .video_transport import GENERATE_URL, STATUS_URL, check_download_url
        self.generate_url, self.status_url, self.check_download_url = GENERATE_URL, STATUS_URL, check_download_url
        self.clip_folder, self.clip_audio = clip_folder, clip_audio
        self.fail_first = set(fail_first_post_numbers)      # 1-based POST numbers that "time out"
        self.pending_once = set(pending_once)               # request IDs whose first status check is pending
        self.posts, self.status_checks, self.downloads = 0, 0, 0
        self.durations, self.seen_pending = {}, set()

    def __call__(self, method, url, body, *, credential):
        if method == "POST":
            if url != self.generate_url or not credential:
                raise NetworkError("video_transport_refused", "Unsupported provider request.")
            self.posts += 1
            if self.posts in self.fail_first:
                raise TimeoutError("simulated timeout: the provider may or may not have accepted the job")
            request_id = f"demo-request-{self.posts}"
            self.durations[request_id] = body["duration"]
            return {"request_id": request_id}
        if method == "GET":
            request_id = url[len(self.status_url):]
            self.status_checks += 1
            if request_id not in self.durations:
                return {"status": "failed"}
            if request_id in self.pending_once and request_id not in self.seen_pending:
                self.seen_pending.add(request_id)
                return {"status": "pending"}
            return {"status": "done", "video": {"url": f"https://vidgen.x.ai/demo/{request_id}.mp4"}}
        if method == "DOWNLOAD":
            self.check_download_url(url)
            if credential:
                raise NetworkError("video_transport_refused", "Media downloads cannot carry credentials.")
            request_id = url.rsplit("/", 1)[1][:-4]
            self.downloads += 1
            return synthetic_clip(self.durations[request_id], self.downloads, self.clip_folder, self.clip_audio)
        raise NetworkError("video_transport_refused", "Unsupported provider request.")


def demo_clock(start=DEMO_EPOCH):
    """A fixed, ticking demo clock (one second per reading). The mock stories carry fixed publish dates,
    so a wall clock would make them too old to select after a week; a fixed clock keeps the demo
    reproducible on any date. Folder names still use the real time."""
    state = {"now": start}

    def clock():
        state["now"] += timedelta(seconds=1)
        return state["now"].strftime("%Y-%m-%dT%H:%M:%SZ")
    clock.seconds = lambda: state["now"].timestamp()
    return clock


@contextmanager
def review_time(clock):
    """Step 37 judges evidence age against `review._clock_now` (its documented seam). In the demo it
    follows the demo clock, so the simulated review sees the same time as every other stage."""
    from . import review
    seconds = getattr(clock, "seconds", None)
    if seconds is None:
        yield
        return
    saved = review._clock_now
    review._clock_now = seconds
    try:
        yield
    finally:
        review._clock_now = saved


def _story(root, clock):
    from .scout_cli import scout_once
    from .selection_cli import select_stories
    from .verification_cli import verify_stored
    scout_once("config/scout-sources.mock.json", root=root, clock=clock)
    verify_stored([], "config/verification.mock.json", verify_all=True, root=root, clock=clock)
    report, _ = select_stories([], "config/verification.mock.json", "config/editorial.mock.json", select_all=True,
                               root=root, clock=clock)
    record_id = next(e["record_id"] for e in report["entries"] if e["disposition"] == "select")
    return report["selection_run_id"], record_id


def _expect_refusal(function, code, log, label):
    try:
        function()
    except NetworkError as error:
        if error.code != code:
            raise
        log.append({"check": label, "refused_with": code})
        return
    raise AssertionError(f"demo expected {label} to be refused with {code}")


def run_demo(output=None, clock=None, narrated=False):
    from .export import verify_package
    from .review import ReviewRecorder, reviewable
    from .video_production import VideoProduction, WorkflowStore
    clock = clock or demo_clock()
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = ROOT / "runtime" / "video-production-demo" / f"run-{stamp}-{secrets.token_hex(3)}"
    root = Path(output).resolve()
    if root.exists() and any(root.iterdir()):
        raise NetworkError("demo_folder_not_empty", "Choose an empty or new demo folder.")
    root.mkdir(parents=True, exist_ok=True)
    proofs = []
    with network_blocked(), placeholder_credential(), review_time(clock), tempfile.TemporaryDirectory() as clips:
        provider = MockGrok(clips, clip_audio=narrated)
        flow = VideoProduction(root=root, clock=clock, transport=provider)
        run_id, record_id = _story(root, clock)
        narration = _narration_inputs(flow, root, run_id, record_id, proofs) if narrated else None
        started = flow.start(selection_run_id=run_id, record_id=record_id, narration=narration)
        workflow_id = started["workflow_id"]
        assert started["status"] == "waiting_for_consent", started["status"]
        again = flow.resume(workflow_id, allow_network=True)
        assert again["status"] == "waiting_for_consent" and provider.posts == 0
        proofs.append({"check": "start_and_resume_never_submit", "provider_posts": provider.posts})
        scenes = {s["scene_index"]: s["job_id"] for s in started["scenes"]}
        _expect_refusal(lambda: flow.submit(workflow_id, 1, consent=f"paid-generate:{scenes[2]}", allow_network=True),
                        "video_consent_required", proofs, "consent_for_another_job")
        for index in (1, 2, 3, 4):
            result = flow.submit(workflow_id, index, consent=f"paid-generate:{scenes[index]}", allow_network=True)
            if index == 2:
                assert result["submission"]["outcome"] == "uncertain"
        assert flow.inspect(workflow_id)["status"] == "uncertain_submission"
        proofs.append({"check": "uncertain_submission_pauses_workflow", "scene": 2})
        consent = f"paid-generate:{scenes[2]}"
        _expect_refusal(lambda: flow.submit(workflow_id, 2, consent=consent, allow_network=True),
                        "video_submit_refused", proofs, "plain_resubmit_of_uncertain_job")
        _expect_refusal(lambda: flow.submit(workflow_id, 2, consent=consent, allow_network=True, retry_uncertain=True),
                        "duplicate_billing_ack_required", proofs, "retry_without_duplicate_billing_acknowledgment")
        flow.submit(workflow_id, 2, consent=consent, allow_network=True, retry_uncertain=True,
                    acknowledge_duplicate_billing=True)
        proofs.append({"check": "explicit_acknowledged_retry", "scene": 2, "provider_posts": provider.posts})
        waiting = flow.resume(workflow_id, allow_network=True)
        assert waiting["status"] == "waiting_for_provider", waiting["status"]
        proofs.append({"check": "pending_scene_waits_without_polling", "status_checks": provider.status_checks})
        with WorkflowStore(root).lock(workflow_id):
            _expect_refusal(lambda: flow.resume(workflow_id, allow_network=True), "workflow_busy", proofs,
                            "concurrent_resume_while_locked")
        review_wait = flow.resume(workflow_id, allow_network=True)
        assert review_wait["status"] == "waiting_for_review", review_wait
        production_id = review_wait["production_id"]
        report_id = review_wait["outputs"]["quality_report_id"]
        _expect_refusal(lambda: flow.export(workflow_id, purpose="approved_preview"), "no_current_preview_approval",
                        proofs, "export_before_human_review")
        row = next(r for r in reviewable(production_id, root) if r["report_id"] == report_id)
        review = ReviewRecorder(root, clock=clock).record(
            production_id, report_id, decision="approved_for_preview", reviewer=DEMO_REVIEWER,
            binding=row["binding_digest"], acknowledgments=row["applicable_acknowledgments"], notes=DEMO_NOTE)
        proofs.append({"check": "simulated_review_recorded_by_demo_not_workflow", "reviewer": DEMO_REVIEWER})
        ready = flow.resume(workflow_id)
        assert ready["status"] == "ready_to_export", ready["status"]
        exported = flow.export(workflow_id, purpose="approved_preview")
        package = Path(exported["export"]["path"])
        verification = verify_package(package)
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
        page = (package / "index.html").read_text(encoding="utf-8")
        posts_before = provider.posts
        if narrated:
            _narration_integrity(flow, root, workflow_id, proofs)
        final = flow.resume(workflow_id, allow_network=True)
        assert provider.posts == posts_before and final["status"] == "exported"
        proofs.append({"check": "resume_after_export_repeats_nothing", "provider_posts": provider.posts})
        from .production import ProductionStore
        state = ProductionStore(root).read(production_id)
        preview = next(s for s in state["stages"] if s["name"] == "preview")["artifacts"]
        video = ProductionStore(root).folder(production_id) / preview["preview_file"]
        audio = _narration_audio(video, package, narration, proofs) if narrated else None
        return {
            "demo": True, "network": "blocked for the whole run", "paid_requests": 0,
            "provider": "MOCK (no xAI request was made)", "media": "synthetic test clips generated locally with ffmpeg",
            "review": "SIMULATED by the demo, clearly labelled; the workflow itself never records reviews",
            "workflow_id": workflow_id, "status": final["status"], "production_id": production_id,
            "quality_report_id": report_id, "quality_result": final["outputs"]["quality_result"],
            "review_id": review[0]["review_id"],
            "video": str(video), "export_package": str(package),
            "export_verified": verification["status"] == "consistent" and not verification["problems"],
            "export_problems": verification["problems"],
            "publishable": manifest["restrictions"]["publishable"],
            "generated_footage_disclosed": "ILLUSTRATIVE MEDIA" in page,
            "mock_provider_counts": {"posts": provider.posts, "status_checks": provider.status_checks,
                                     "downloads": provider.downloads},
            "proofs": proofs, "demo_folder": str(root),
            **({"narration": audio} if narrated else {}),
        }


def _narration_inputs(flow, root, run_id, record_id, proofs):
    """Synthetic narration (and two invalid ones) in the demo folder; invalid input is refused before anything exists."""
    inputs = root / "inputs"
    inputs.mkdir()
    overlong = synthetic_narration(inputs / "synthetic-narration-16s.wav", seconds=16)
    before = sorted(p.name for p in (root / "runtime" / "video-production").glob("*")) \
        if (root / "runtime" / "video-production").exists() else []
    _expect_refusal(lambda: flow.start(selection_run_id=run_id, record_id=record_id, narration=overlong),
                    "narration_too_long", proofs, "overlong_narration_refused_never_truncated")
    _expect_refusal(lambda: flow.start(selection_run_id=run_id, record_id=record_id,
                                       narration=silent_wav(inputs / "all-silence.wav")),
                    "narration_silent", proofs, "all_silent_narration_refused")
    after = sorted(p.name for p in (root / "runtime" / "video-production").glob("*")) \
        if (root / "runtime" / "video-production").exists() else []
    assert before == after, "a refused narration must not create a workflow"
    proofs.append({"check": "refused_narration_created_nothing", "workflows": len(after)})
    return synthetic_narration(inputs / "synthetic-narration.wav", seconds=11.5)


def _narration_integrity(flow, root, workflow_id, proofs):
    """A changed managed copy is refused on resume; restoring the exact bytes makes it usable again."""
    from .video_production import NARRATION_FILE, WorkflowStore
    managed = WorkflowStore(root).folder(workflow_id) / NARRATION_FILE
    original = managed.read_bytes()
    managed.write_bytes(original[:-2] + bytes(2))                     # one sample changed
    try:
        _expect_refusal(lambda: flow.resume(workflow_id), "narration_tampered", proofs,
                        "changed_narration_refused_on_resume")
    finally:
        managed.write_bytes(original)


def _narration_audio(video, package, source, proofs):
    """Measure the real MP4: narration audio for 15 s, speech where the narration is, silence after it."""
    from .quality import probe_media
    from .video_production import sha256_bytes
    info = probe_media(video)
    exported = package / "media" / "preview.mp4"
    assert exported.read_bytes() == video.read_bytes(), "the export must carry the exact reviewed video"
    speech, tail = loudness(video, 1.0, 9.0), loudness(video, 12.2, 2.5)
    assert info["audio_present"] and abs(info["audio_duration"] - 15) <= 0.1, info
    assert speech > -30 and tail < -60, (speech, tail)
    proofs.append({"check": "narration_mixed_source_audio_muted", "speech_peak_db": speech, "after_narration_peak_db": tail})
    return {"source": "SYNTHETIC tone bursts written by the demo (not a voice)",
            "source_seconds": 11.5, "source_sha256": sha256_bytes(Path(source).read_bytes()),
            "padded_to_seconds": 15, "audio_codec": info["audio_codec"], "audio_seconds": info["audio_duration"],
            "speech_peak_db": speech, "after_narration_peak_db": tail,
            "generated_clip_audio": "synthetic clips carried a loud 440 Hz tone; it is muted in the preview"}
