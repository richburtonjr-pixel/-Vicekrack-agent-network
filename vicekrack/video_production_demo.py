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
"""

import json
import os
import secrets
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


def synthetic_clip(duration, index, folder):
    """A real, decodable 720x1280 (9:16) H.264 clip made locally; clearly synthetic test media."""
    from .preview import dependencies
    exe = dependencies()[3]
    colors = ("0x1d4ed8", "0x047857", "0xb45309", "0x7c3aed")
    target = Path(folder) / f"synthetic-{index}.mp4"
    subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=720x1280:rate=24:duration={duration}", "-f", "lavfi", "-i",
                    f"color=c={colors[(index - 1) % 4]}:size=720x1280:rate=24:duration={duration}",
                    "-filter_complex", "[0:v][1:v]blend=all_mode=overlay:all_opacity=0.5,format=yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", "-an", str(target)],
                   check=True, capture_output=True, timeout=120)
    return target.read_bytes()


class MockGrok:
    """Stands in for xAI's video API. Never opens a connection; keeps counts for the demo report."""

    def __init__(self, clip_folder, fail_first_post_numbers=(2,), pending_once=("demo-request-3",)):
        from .video_transport import GENERATE_URL, STATUS_URL, check_download_url
        self.generate_url, self.status_url, self.check_download_url = GENERATE_URL, STATUS_URL, check_download_url
        self.clip_folder = clip_folder
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
            return synthetic_clip(self.durations[request_id], self.downloads, self.clip_folder)
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


def run_demo(output=None, clock=None):
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
        provider = MockGrok(clips)
        flow = VideoProduction(root=root, clock=clock, transport=provider)
        run_id, record_id = _story(root, clock)
        started = flow.start(selection_run_id=run_id, record_id=record_id)
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
        final = flow.resume(workflow_id, allow_network=True)
        assert provider.posts == posts_before and final["status"] == "exported"
        proofs.append({"check": "resume_after_export_repeats_nothing", "provider_posts": provider.posts})
        from .production import ProductionStore
        state = ProductionStore(root).read(production_id)
        preview = next(s for s in state["stages"] if s["name"] == "preview")["artifacts"]
        video = ProductionStore(root).folder(production_id) / preview["preview_file"]
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
        }
