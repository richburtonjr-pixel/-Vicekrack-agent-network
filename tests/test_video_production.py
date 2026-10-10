"""Step 44: the controlled video-production workflow.

Local fixtures only: a fake Grok transport (no sockets), fake renderer packages, a fake media
probe and poster reader. No network and no credits. The real offline demo (real ffmpeg render)
runs only with RUN_LOCAL_RENDER_TESTS=1, with sockets blocked.
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_artifact_binding import BoundRenderer, Step36Base
from test_quality import GOOD_MEDIA
from vicekrack import video_jobs as jobs
from vicekrack import video_production as vp
from vicekrack.errors import NetworkError
from vicekrack.export import verify_package
from vicekrack.media_production import render_production
from vicekrack.orchestrator import ROOT
from vicekrack.review import ReviewRecorder, history, reviewable
from vicekrack.video_transport import STATUS_URL

EPOCH = datetime(2026, 10, 4, 13, 10, tzinfo=timezone.utc)
TEAL = ((1080, 1920), [(89, 214, 193)] * 2)
KEY = "synthetic-test-value-not-a-key"


class FakeGrok:
    """Stands in for xAI. Records every call; never opens a connection."""

    def __init__(self):
        self.posts, self.status_checks, self.downloads = [], [], []
        self.post_effects = []                    # queued exceptions for the next POSTs (None = accept)
        self.status_script = {}                   # request_id -> list of statuses to return first
        self.durations = {}

    def __call__(self, method, url, body, *, credential):
        if method == "POST":
            assert credential, "paid submissions must carry the credential"
            self.posts.append(body)
            effect = self.post_effects.pop(0) if self.post_effects else None
            if effect is not None:
                raise effect
            request_id = f"req-{len(self.posts)}"
            self.durations[request_id] = body["duration"]
            return {"request_id": request_id}
        if method == "GET":
            request_id = url[len(STATUS_URL):]
            self.status_checks.append(request_id)
            script = self.status_script.get(request_id)
            status = script.pop(0) if script else "done"
            if status == "done":
                return {"status": "done", "video": {"url": f"https://vidgen.x.ai/test/{request_id}.mp4"}}
            return {"status": status}
        if method == "DOWNLOAD":
            assert not credential, "media downloads must never carry credentials"
            request_id = url.rsplit("/", 1)[1][:-4]
            self.downloads.append(request_id)
            return b"0000ftyp" + f"duration={self.durations[request_id]};".encode() + request_id.encode() * 8
        raise AssertionError(method)


def fake_inspect(path, *args, **kwargs):
    data = Path(path).read_bytes()
    duration = float(re.search(rb"duration=([0-9.]+);", data).group(1))
    return {"width": 720, "height": 1280, "duration": duration}


class WorkflowRenderer(BoundRenderer):
    """The fake Step 36 renderer; a media revision is written into its own folder (like the real one)."""

    def __init__(self):
        self.revisions = 0

    def __call__(self, plan, **kwargs):
        media = kwargs.pop("media", None)
        kwargs.pop("media_root", None)
        if media is not None:
            self.revisions += 1
            kwargs["directory"] = Path(kwargs["directory"]) / f"revision-{self.revisions}"
        return super().__call__(plan, **kwargs)


class Base(Step36Base):
    def setUp(self):
        super().setUp()
        self.ticks = 0
        self.grok = FakeGrok()
        self.renderer = WorkflowRenderer()
        self.media_info = dict(GOOD_MEDIA)
        patch.dict(os.environ, {"XAI_API_KEY": KEY}).start()
        patch("vicekrack.media_render.inspect_video", side_effect=fake_inspect).start()
        patch("vicekrack.media_render.prepare_media").start()
        # Evidence age is judged at the fixture time, not the wall clock (Step 37's documented seam).
        patch("vicekrack.review._clock_now",
              side_effect=lambda: (EPOCH + timedelta(seconds=self.ticks)).timestamp()).start()

    def tick(self):
        self.ticks += 1
        return self.tick_now()

    def tick_now(self):
        return (EPOCH + timedelta(seconds=self.ticks)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def flow(self, config_root=None, prober=None):
        return vp.VideoProduction(root=self.root, clock=self.tick, transport=self.grok, renderer=self.renderer,
                                  prober=prober or (lambda path: dict(self.media_info)),
                                  poster_reader=lambda path: TEAL, config_root=config_root)

    def start(self, **kwargs):
        self.vp = self.flow()
        view = self.vp.start(selection_run_id=self.run_id, record_id=self.record_id, **kwargs)
        self.wid = view["workflow_id"]
        self.jobs = {s["scene_index"]: s["job_id"] for s in view["scenes"]}
        return view

    def submit(self, scene, **kwargs):
        kwargs.setdefault("consent", f"paid-generate:{self.jobs.get(scene)}")
        kwargs.setdefault("allow_network", True)
        return self.vp.submit(self.wid, scene, **kwargs)

    def submit_all(self):
        for scene in (1, 2, 3, 4):
            view = self.submit(scene)
        return view

    def to_review(self):
        self.start()
        self.submit_all()
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_review", view)
        self.pid = view["production_id"]
        self.report_id = view["outputs"]["quality_report_id"]
        return view

    def review(self, decision="approved_for_preview", supersedes=None):
        row = next(r for r in reviewable(self.pid, self.root) if r["report_id"] == self.report_id)
        record, _ = ReviewRecorder(self.root, clock=self.tick).record(
            self.pid, self.report_id, decision=decision, reviewer="Test Reviewer", binding=row["binding_digest"],
            acknowledgments=row["applicable_acknowledgments"], supersedes=supersedes)
        return record

    def state(self, wid=None):
        return json.loads((self.root / "runtime/video-production" / (wid or self.wid) / "workflow.json").read_text())

    def stages(self, view):
        return {s["name"]: s["status"] for s in view["stages"]}

    def code(self, function, *args, **kwargs):
        with self.assertRaises(NetworkError) as caught:
            function(*args, **kwargs)
        return caught.exception.code


class SuccessfulWorkflowTests(Base):
    def test_full_workflow_render_quality_explicit_review_and_permitted_export(self):
        view = self.start()
        self.assertEqual(view["status"], "waiting_for_consent")
        self.assertEqual(self.grok.posts, [])                     # starting never spends money
        self.assertEqual(self.stages(view)["jobs"], "completed")
        self.assertEqual({s["consent"] for s in view["scenes"]}, {f"paid-generate:{j}" for j in self.jobs.values()})
        self.assertTrue(any("video-production-submit" in line for line in view["next"]))
        self.assertTrue((self.root / "runtime/video-jobs").is_dir())             # jobs stay inside this root

        view = self.submit_all()
        self.assertEqual(len(self.grok.posts), 4)
        self.assertEqual(view["status"], "waiting_for_provider")
        offline = self.vp.resume(self.wid)                               # no --allow-network: no request at all
        self.assertEqual(offline["status"], "waiting_for_provider")
        self.assertEqual(self.grok.status_checks, [])

        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual(len(self.grok.status_checks), 4)
        self.assertEqual(len(self.grok.downloads), 4)
        self.assertEqual(self.renderer.revisions, 1)
        self.assertEqual(view["outputs"]["quality_result"], "pass")
        pid, report_id = view["production_id"], view["outputs"]["quality_report_id"]
        self.assertEqual(history(pid, self.root)["reviews"], [])           # the workflow never reviews itself
        rows = {r["report_id"]: r for r in reviewable(pid, self.root)}
        self.assertEqual(rows[report_id]["binding"], "matching")
        self.assertEqual(len(rows), 1)                                    # the revision is what was checked

        self.pid, self.report_id = pid, report_id
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "no_current_preview_approval")
        self.review()
        ready = self.vp.resume(self.wid)
        self.assertEqual(ready["status"], "ready_to_export")
        self.assertEqual(ready["next"][0], f"python -m vicekrack video-production-export {self.wid} --purpose approved_preview")
        exported = self.vp.export(self.wid, purpose="approved_preview")
        self.assertEqual(exported["status"], "exported")
        self.assertIs(exported["publishable"], False)
        self.assertIs(exported["export"]["publishable"], False)
        package = Path(exported["export"]["path"])
        self.assertEqual(verify_package(package)["status"], "consistent")
        manifest = json.loads((package / "package.json").read_text())
        self.assertIs(manifest["restrictions"]["publishable"], False)
        page = (package / "index.html").read_text()
        self.assertIn("ILLUSTRATIVE MEDIA", page)
        self.assertIn("not approved for publishing", page)
        self.assertEqual(set(self.stages(exported).values()), {"completed"})

        calls = (len(self.grok.posts), len(self.grok.status_checks), len(self.grok.downloads), self.renderer.revisions)
        again = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(again["status"], "exported")
        self.assertEqual(calls, (len(self.grok.posts), len(self.grok.status_checks), len(self.grok.downloads),
                                 self.renderer.revisions))

        text = (self.root / "runtime/video-production" / self.wid / "workflow.json").read_text()
        self.assertNotIn("vidgen", text)
        self.assertNotIn("download_url", text)
        self.assertNotIn(KEY, text)
        vp.validate_state(json.loads(text))
        self.assertTrue((self.root / "runtime/video-production" / self.wid / "scene-plan.json").is_file())
        self.assertTrue((self.root / "runtime/video-production" / self.wid / "media-manifest.json").is_file())

    def test_start_from_completed_production_and_review_copy_export(self):
        self.make(renderer=self.renderer)
        self.vp = self.flow()
        view = self.vp.start(production_id=self.pid)
        self.wid = view["workflow_id"]
        self.jobs = {s["scene_index"]: s["job_id"] for s in view["scenes"]}
        self.assertEqual(view["production_id"], self.pid)
        self.submit_all()
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_review")
        copy = self.vp.export(self.wid, purpose="review_copy")             # allowed without approval
        self.assertIs(copy["export"]["publishable"], False)
        self.assertEqual(copy["status"], "waiting_for_review")
        self.assertEqual(self.stages(copy)["export"], "pending")

    def test_start_requires_one_origin_and_allowed_settings(self):
        flow = self.flow()
        self.assertEqual(self.code(flow.start), "invalid_workflow_origin")
        self.assertEqual(self.code(flow.start, production_id="prod-x", selection_run_id="a", record_id="b"),
                         "invalid_workflow_origin")
        self.assertEqual(self.code(flow.start, selection_run_id=self.run_id, record_id=self.record_id, model="other"),
                         "invalid_generation_settings")
        self.start()
        self.assertEqual(self.code(self.flow().start, selection_run_id=self.run_id, record_id=self.record_id),
                         "workflow_exists")
        self.assertEqual([row["workflow_id"] for row in self.vp.list()], [self.wid])


class ImportAndCliTests(Base):
    def test_documented_imports_exist(self):
        from vicekrack.video_production import VideoProduction, WorkflowStore
        from vicekrack.video_production_demo import run_demo
        self.assertTrue(callable(VideoProduction) and callable(WorkflowStore))
        spec = importlib.util.spec_from_file_location("step44_demo", ROOT / "scripts/step44_demo.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))
        self.assertIs(module.run_demo, run_demo)

    def test_every_documented_command_is_a_valid_choice(self):
        for command in ("video-production-start", "video-production-list", "video-production-inspect",
                        "video-production-resume", "video-production-submit", "video-production-retry-scene",
                        "video-production-export", "video-production-demo"):
            result = subprocess.run([sys.executable, "-m", "vicekrack", command, "--help"], cwd=ROOT,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, (command, result.stderr))
            self.assertNotIn("invalid choice", result.stderr)

    def cli(self, *argv):
        from vicekrack.video_cli import main
        out = io.StringIO()
        flow = self.flow()
        with patch("vicekrack.video_production.VideoProduction", lambda: flow), redirect_stdout(out):
            code = main(list(argv))
        return code, json.loads(out.getvalue())

    def test_cli_runs_the_workflow(self):
        code, view = self.cli("video-production-start", "--selection", self.run_id, "--record", self.record_id)
        self.assertEqual((code, view["status"]), (0, "waiting_for_consent"))
        wid = view["workflow_id"]
        jobs_ = {s["scene_index"]: s["job_id"] for s in view["scenes"]}
        self.assertEqual(self.cli("video-production-list")[1]["workflows"][0]["workflow_id"], wid)
        self.assertEqual(self.cli("video-production-inspect", wid)[1]["status"], "waiting_for_consent")
        code, error = self.cli("video-production-submit", wid, "--scene", "1", "--consent", "paid-generate:wrong",
                               "--allow-network")
        self.assertEqual((code, error["error"]["code"]), (1, "video_consent_required"))
        for scene in (1, 2, 3, 4):
            code, view = self.cli("video-production-submit", wid, "--scene", str(scene), "--consent",
                                  f"paid-generate:{jobs_[scene]}", "--allow-network")
            self.assertEqual(code, 0)
        code, view = self.cli("video-production-resume", wid, "--allow-network")
        self.assertEqual(view["status"], "waiting_for_review")
        code, error = self.cli("video-production-export", wid, "--purpose", "approved_preview")
        self.assertEqual(error["error"]["code"], "no_current_preview_approval")
        code, error = self.cli("video-production-retry-scene", wid, "--scene", "1")
        self.assertEqual(error["error"]["code"], "scene_not_failed")
        out = json.dumps(view)
        self.assertNotIn("vidgen", out)
        self.assertNotIn(KEY, out)

    def test_cli_demo_command_dispatches_to_run_demo(self):
        from vicekrack.video_cli import main
        out = io.StringIO()
        with patch("vicekrack.video_production_demo.run_demo", return_value={"demo": True}) as demo, redirect_stdout(out):
            self.assertEqual(main(["video-production-demo", "--output", str(self.root / "demo")]), 0)
        demo.assert_called_once_with(self.root / "demo")


class ProviderRecoveryTests(Base):
    def test_failed_provider_job_is_replaced_only_explicitly(self):
        self.start()
        self.submit_all()
        self.grok.status_script["req-1"] = ["failed"]
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "failed")
        self.assertEqual(self.stages(view)["generation"], "failed")
        self.assertTrue(any("video-production-retry-scene" in line for line in view["next"]))
        self.assertEqual(self.code(self.vp.retry_scene, self.wid, 1), "replacement_must_differ")
        self.assertEqual(self.code(self.vp.retry_scene, self.wid, 2), "scene_not_failed")
        posts = len(self.grok.posts)
        view = self.vp.retry_scene(self.wid, 1, model="grok-imagine-video-1.5-lite")
        self.assertEqual(view["status"], "waiting_for_consent")           # the new job needs its own consent
        self.assertEqual(len(self.grok.posts), posts)
        new_job = view["scenes"][0]["job_id"]
        self.assertNotEqual(new_job, self.jobs[1])
        self.assertEqual(view["scenes"][0]["replaced_job_ids"], [self.jobs[1]])
        self.assertEqual(self.code(self.submit, 1), "video_consent_required")   # the old consent is not reused
        self.submit(1, consent=f"paid-generate:{new_job}")
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_review")

    def test_transient_status_error_keeps_provider_ids_and_waits(self):
        self.start()
        self.submit_all()
        original = self.grok.__call__

        def flaky(method, url, body, *, credential):
            if method == "GET":
                raise TimeoutError("private provider detail")
            return original(method, url, body, credential=credential)
        self.vp.transport = flaky
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_provider")
        self.assertEqual([s["request_id"] for s in view["scenes"]], ["req-1", "req-2", "req-3", "req-4"])
        self.assertNotIn("private provider detail", json.dumps(self.state()))
        self.vp.transport = self.grok
        self.assertEqual(self.vp.resume(self.wid, allow_network=True)["status"], "waiting_for_review")
        self.assertEqual(len(self.grok.posts), 4)

    def test_pending_scene_waits_and_status_checks_are_bounded(self):
        self.start()
        self.submit_all()
        for request in ("req-1", "req-2", "req-3", "req-4"):
            self.grok.status_script[request] = ["pending"] * 3
        for _ in range(3):
            view = self.vp.resume(self.wid, allow_network=True)
            self.assertEqual(view["status"], "waiting_for_provider")
        limit = self.vp.limits["max_status_checks_per_resume"]
        self.assertEqual(len(self.grok.status_checks), 3 * limit)               # never more than the budget
        self.assertEqual(self.vp.resume(self.wid, allow_network=True)["status"], "waiting_for_review")

    def test_interrupted_preview_revision_is_adopted_not_rendered_twice(self):
        self.start()
        self.submit_all()
        real = render_production

        def crash_after_render(*args, **kwargs):
            real(*args, **kwargs)
            raise KeyboardInterrupt("simulated crash after the revision was saved")
        with patch("vicekrack.media_production.render_production", crash_after_render), \
                self.assertRaises(KeyboardInterrupt):
            self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(self.renderer.revisions, 1)
        state = self.state()
        self.assertEqual({s["name"]: s["status"] for s in state["stages"]}["media_manifest"], "completed")
        view = self.vp.resume(self.wid, allow_network=True)                    # the lock was released by the crash
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual(self.renderer.revisions, 1)
        self.assertEqual((len(self.grok.posts), len(self.grok.downloads)), (4, 4))
        self.assertIn("adopted_existing_revision", [t["event"] for t in self.state()["trace"]])

    def test_interrupted_quality_stage_resumes_without_repeating_work(self):
        self.start()
        self.submit_all()
        with patch.object(vp.VideoProduction, "_stage_quality", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(self.stages(self.vp.inspect(self.wid))["preview_revision"], "completed")
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "waiting_for_review")
        self.assertEqual((len(self.grok.posts), len(self.grok.downloads), self.renderer.revisions), (4, 4, 1))


class PaidSubmissionTests(Base):
    def test_uncertain_submission_pauses_and_needs_acknowledged_retry(self):
        self.start()
        self.grok.post_effects = [TimeoutError("private provider detail")]
        view = self.submit(1)
        self.assertEqual(view["submission"]["outcome"], "uncertain")
        self.assertEqual(view["status"], "uncertain_submission")
        self.assertNotIn("private provider detail", json.dumps(self.state()))
        self.assertTrue(any("--acknowledge-duplicate-billing" in line for line in view["next"]))
        for scene in (2, 3, 4):
            self.submit(scene)
        self.assertEqual(self.vp.resume(self.wid, allow_network=True)["status"], "uncertain_submission")
        posts = len(self.grok.posts)
        self.assertEqual(self.code(self.submit, 1), "video_submit_refused")
        self.assertEqual(self.code(self.submit, 1, retry_uncertain=True), "duplicate_billing_ack_required")
        self.assertEqual(self.code(self.submit, 1, retry_uncertain=True, acknowledge_duplicate_billing=True,
                                   allow_network=False), "network_not_allowed")
        self.assertEqual(len(self.grok.posts), posts)                      # none of the refusals sent anything
        view = self.submit(1, retry_uncertain=True, acknowledge_duplicate_billing=True)
        self.assertEqual(len(self.grok.posts), posts + 1)
        self.assertEqual(view["status"], "waiting_for_provider")

    def test_duplicate_paid_requests_are_refused(self):
        self.start()
        self.assertEqual(self.code(self.submit, 1, consent=f"paid-generate:{self.jobs[2]}"), "video_consent_required")
        self.assertEqual(self.code(self.submit, 1, consent="yes"), "video_consent_required")
        self.assertEqual(self.code(self.submit, 1, allow_network=False), "network_not_allowed")
        self.assertEqual(self.code(self.submit, 9, consent=f"paid-generate:{self.jobs[1]}"), "invalid_scene")
        self.assertEqual(self.grok.posts, [])
        self.submit(1)
        self.assertEqual(self.code(self.submit, 1), "video_submit_refused")
        self.assertEqual(self.code(self.submit, 1, retry_uncertain=True, acknowledge_duplicate_billing=True),
                         "video_submit_refused")                           # an accepted job is never resent
        self.assertEqual(len(self.grok.posts), 1)
        for _ in range(3):
            self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(len(self.grok.posts), 1)                          # resume never submits
        # A second workflow for the same story and settings maps to the same jobs: no second charge.
        self.assertEqual(self.code(self.flow().start, selection_run_id=self.run_id, record_id=self.record_id),
                         "workflow_exists")
        with jobs.use_root(self.root):
            record = jobs.inspect(self.jobs[1])
        self.assertEqual((record["request_id"], len(record["attempts"])), ("req-1", 1))

    def test_missing_key_refuses_before_any_request(self):
        self.start()
        with patch.dict(os.environ, {"XAI_API_KEY": ""}):
            self.code(self.submit, 1)
        self.assertEqual(self.grok.posts, [])
        self.assertEqual(self.state()["scenes"][0]["job_status"], "prepared")

    def test_submit_after_generation_complete_is_refused(self):
        self.to_review()
        self.assertEqual(self.code(self.submit, 1), "generation_complete")
        self.assertEqual(len(self.grok.posts), 4)


class LimitsAndConcurrencyTests(Base):
    def config(self, **limits):
        folder = Path(tempfile.mkdtemp(dir=self.root))
        config = json.loads((ROOT / vp.CONFIG_PATH).read_text())
        config["limits"].update(limits)
        (folder / "config").mkdir()
        (folder / vp.CONFIG_PATH).write_text(json.dumps(config))
        return folder

    def test_concurrent_runs_are_refused(self):
        self.start()
        with vp.WorkflowStore(self.root).lock(self.wid):
            self.assertEqual(self.code(self.vp.resume, self.wid), "workflow_busy")
            self.assertEqual(self.code(self.submit, 1), "workflow_busy")
            self.assertEqual(self.code(self.vp.export, self.wid, purpose="review_copy"), "workflow_busy")
            self.assertEqual(self.code(self.vp.retry_scene, self.wid, 1), "workflow_busy")
        self.assertEqual(self.grok.posts, [])
        self.assertEqual(self.vp.resume(self.wid)["status"], "waiting_for_consent")

    def test_maximum_steps(self):
        flow = vp.VideoProduction(root=self.root, clock=self.tick, transport=self.grok, renderer=self.renderer,
                                  config_root=self.config(max_steps=2))
        with self.assertRaises(NetworkError) as caught:
            flow.start(selection_run_id=self.run_id, record_id=self.record_id)
        self.assertEqual(caught.exception.code, "workflow_step_limit")
        wid = vp.WorkflowStore(self.root).ids()[0]
        self.assertEqual(self.state(wid)["status"], "blocked")
        self.assertEqual(self.code(flow.resume, wid), "workflow_step_limit")

    def test_maximum_stage_attempts(self):
        self.start()
        self.submit_all()
        with patch("vicekrack.media_render.inspect_video", return_value={"width": 720, "height": 1280, "duration": 99}):
            for attempt in (1, 2, 3):
                view = self.vp.resume(self.wid, allow_network=True)
                self.assertEqual(view["status"], "failed")
                downloads = next(s for s in view["stages"] if s["name"] == "downloads")
                self.assertEqual((downloads["attempts"], downloads["error_code"]), (attempt, "video_download_invalid"))
            self.assertEqual(self.code(self.vp.resume, self.wid, allow_network=True), "workflow_attempt_limit")
        self.assertEqual(self.state()["status"], "blocked")
        self.assertEqual(len(self.grok.posts), 4)


class IntegrityTests(Base):
    def path(self, name):
        return self.root / "runtime/video-production" / self.wid / name

    def test_corrupted_state_is_refused(self):
        self.start()
        good = self.path("workflow.json").read_text()
        for broken in ("{not json", json.dumps({"contract": "video_production_workflow"})):
            self.path("workflow.json").write_text(broken)
            self.assertEqual(self.code(self.vp.resume, self.wid), "workflow_corrupt")
        edited = json.loads(good)
        edited["publishable"] = True
        self.path("workflow.json").write_text(json.dumps(edited))
        self.assertEqual(self.code(self.vp.inspect, self.wid), "workflow_corrupt")
        edited = json.loads(good)
        edited["status"] = "ready_to_export"                                # valid shape, wrong self-hash
        self.path("workflow.json").write_text(json.dumps(edited))
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "workflow_corrupt")
        edited = json.loads(good)
        edited["notice"] = "https://vidgen.x.ai/signed?download_url=1"
        body = {k: v for k, v in edited.items() if k != "state_sha256"}
        edited["state_sha256"] = vp.sha256_bytes(vp.canonical(body))
        self.path("workflow.json").write_text(json.dumps(edited))
        self.assertEqual(self.code(self.vp.resume, self.wid), "workflow_corrupt")
        self.assertEqual(self.vp.list()[0]["error"], "workflow_corrupt")
        for bad in ("../x", "vpw-" + "g" * 24, "vpw-1"):
            self.assertEqual(self.code(self.vp.inspect, bad), "invalid_workflow_id")
        self.assertEqual(self.grok.posts, [])

    def test_configuration_change_blocks_the_workflow(self):
        self.start()
        folder = Path(tempfile.mkdtemp(dir=self.root))
        config = json.loads((ROOT / vp.CONFIG_PATH).read_text())
        config["limits"]["max_steps"] = 79
        (folder / "config").mkdir()
        (folder / vp.CONFIG_PATH).write_text(json.dumps(config))
        changed = vp.VideoProduction(root=self.root, clock=self.tick, transport=self.grok, config_root=folder)
        self.assertEqual(self.code(changed.resume, self.wid), "workflow_config_changed")
        self.assertEqual(self.code(changed.submit, self.wid, 1, consent=f"paid-generate:{self.jobs[1]}",
                                   allow_network=True), "workflow_config_changed")
        self.assertEqual(changed.inspect(self.wid)["integrity_problems"], ["workflow_config_changed"])
        self.assertEqual(self.grok.posts, [])
        (folder / vp.CONFIG_PATH).write_text("{")
        self.assertEqual(self.code(vp.VideoProduction, root=self.root, config_root=folder), "invalid_workflow_config")

    def test_media_tampering_blocks_everything(self):
        self.to_review()
        clip = self.path(f"media/{self.jobs[2]}.mp4")
        clip.write_bytes(clip.read_bytes() + b"x")
        self.assertEqual(self.code(self.vp.resume, self.wid), "media_tampered")
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="review_copy"), "media_tampered")
        self.assertEqual(self.vp.inspect(self.wid)["integrity_problems"], ["media_tampered"])
        clip.unlink()
        self.assertEqual(self.code(self.vp.resume, self.wid), "media_tampered")

    def test_manifest_and_scene_plan_tampering(self):
        self.to_review()
        manifest = self.path("media-manifest.json")
        original = manifest.read_bytes()
        manifest.write_bytes(original.replace(b"cover", b"contain"))
        self.assertEqual(self.code(self.vp.resume, self.wid), "media_manifest_tampered")
        manifest.write_bytes(original)
        plan = self.path("scene-plan.json")
        plan.write_bytes(plan.read_bytes() + b" ")
        self.assertEqual(self.code(self.vp.resume, self.wid), "scene_plan_tampered")

    def test_production_preview_replaced_outside_the_workflow(self):
        self.to_review()
        manifest = json.loads(self.path("media-manifest.json").read_text())
        manifest["assignments"][0]["anchor"] = "top"
        render_production(self.pid, manifest, self.path("media"), root=self.root, clock=self.tick,
                          renderer=self.renderer)
        self.assertEqual(self.code(self.vp.resume, self.wid), "production_preview_changed")
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "production_preview_changed")


class QualityAndReviewTests(Base):
    def test_quality_failure_stops_the_workflow_and_blocks_export(self):
        self.media_info["width"] = 720
        self.start()
        self.submit_all()
        view = self.vp.resume(self.wid, allow_network=True)
        self.assertEqual(view["status"], "quality_failed")
        self.assertEqual(view["outputs"]["quality_result"], "fail")
        self.assertEqual(self.stages(view)["quality"], "failed")
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "quality_required")
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="review_copy"), "quality_required")

    def test_review_rejection_waits_and_never_exports(self):
        self.to_review()
        self.review("rejected")
        view = self.vp.resume(self.wid)
        self.assertEqual(view["status"], "review_rejected")
        self.assertEqual(self.stages(view)["review"], "waiting")
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "no_current_preview_approval")

    def test_stale_or_superseded_approval_blocks_export(self):
        self.to_review()
        approval = self.review()
        self.assertEqual(self.vp.resume(self.wid)["status"], "ready_to_export")
        self.review("changes_requested", supersedes=approval["review_id"])
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "no_current_preview_approval")
        self.assertEqual(self.vp.inspect(self.wid)["status"], "review_rejected")      # changes requested: not approved
        self.assertEqual(self.vp.resume(self.wid)["status"], "review_rejected")

    def test_new_media_invalidates_the_previous_approval(self):
        self.to_review()
        self.review()
        self.assertEqual(self.vp.resume(self.wid)["status"], "ready_to_export")
        # A later media revision of the same production (another media manifest): the approval no longer binds.
        manifest = json.loads(self.path_media_manifest().read_text())
        manifest["assignments"][0]["anchor"] = "top"
        render_production(self.pid, manifest, self.root / "runtime/video-production" / self.wid / "media",
                          root=self.root, clock=self.tick, renderer=self.renderer)
        rows = {r["report_id"]: r for r in reviewable(self.pid, self.root)}
        self.assertEqual(rows[self.report_id]["binding"], "changed")
        self.assertFalse(history(self.pid, self.root)["reviews"][0]["current_preview_approval"])
        self.assertEqual(self.code(self.vp.export, self.wid, purpose="approved_preview"), "production_preview_changed")
        from vicekrack.export import PreviewExporter
        with self.assertRaises(NetworkError):                               # the Step 38 gate refuses on its own too
            PreviewExporter(self.root, clock=self.tick).export(self.pid, self.report_id, purpose="approved_preview")

    def path_media_manifest(self):
        return self.root / "runtime/video-production" / self.wid / "media-manifest.json"


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "set RUN_LOCAL_RENDER_TESTS=1 for the real demo")
class OfflineDemoTests(unittest.TestCase):
    def test_demo_renders_a_real_video_with_the_network_blocked(self):
        from vicekrack.video_production_demo import DEMO_REVIEWER, run_demo
        from vicekrack.media_render import inspect_video
        with tempfile.TemporaryDirectory() as temp, \
                patch("socket.socket.connect", side_effect=AssertionError("network disabled in tests")), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XAI_API_KEY", None)
            with patch("vicekrack.review._clock_now", return_value=datetime(2027, 3, 1, tzinfo=timezone.utc).timestamp()):
                summary = run_demo(Path(temp) / "demo")             # any wall-clock date: the demo uses its own clock
            self.assertNotIn("XAI_API_KEY", os.environ)                     # the placeholder is removed again
            self.assertEqual(summary["status"], "exported")
            self.assertEqual(summary["quality_result"], "pass")
            self.assertTrue(summary["export_verified"], summary["export_problems"])
            self.assertIs(summary["publishable"], False)
            self.assertTrue(summary["generated_footage_disclosed"])
            self.assertEqual(summary["paid_requests"], 0)
            self.assertEqual(summary["mock_provider_counts"], {"posts": 5, "status_checks": 5, "downloads": 4})
            checks = {p["check"] for p in summary["proofs"]}
            for check in ("start_and_resume_never_submit", "uncertain_submission_pauses_workflow",
                          "plain_resubmit_of_uncertain_job", "retry_without_duplicate_billing_acknowledgment",
                          "concurrent_resume_while_locked", "export_before_human_review",
                          "simulated_review_recorded_by_demo_not_workflow", "resume_after_export_repeats_nothing"):
                self.assertIn(check, checks)
            reviews = history(summary["production_id"], Path(temp) / "demo")["reviews"]
            self.assertEqual([r["reviewer_label"] for r in reviews], [DEMO_REVIEWER])
            video = Path(summary["video"])
            self.assertTrue(video.is_file() and video.is_relative_to((Path(temp) / "demo").resolve()))   # resolved: aliased temp folders
            info = inspect_video(video)
            self.assertEqual((info["width"], info["height"]), (1080, 1920))
            self.assertAlmostEqual(info["duration"], 15, delta=0.2)


if __name__ == "__main__":
    unittest.main()
