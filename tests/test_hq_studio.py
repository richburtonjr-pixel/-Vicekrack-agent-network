"""Step 48: Video Studio in the Living HQ.

Backend tests drive the real HTTP handler (`api.respond`) with the studio gate, using the Step 44-47
local fixtures: fake xAI video/speech transports (no sockets), fake renderer, fake probes. No network,
no credits. The demo and the browser test (real render, Playwright) run only when enabled.
"""

import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_captions import CaptionBase, TimedSpeech
from vicekrack import speech_jobs as sj
from vicekrack import video_production as vp
from vicekrack.hq import api
from vicekrack.hq.studio import Studio
from vicekrack.hq.studio_api import StudioGate
from vicekrack.orchestrator import ROOT

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
KEY = "synthetic-test-value-not-a-key"


class StudioBase(CaptionBase):
    def setUp(self):
        super().setUp()
        self.make(renderer=self.renderer)               # one completed production (pid)
        self.studio = Studio(self.root, clock=self.tick, video_transport=self.grok, speech_transport=self.timed,
                             renderer=self.renderer, prober=self.probe, poster_reader=lambda path: ((1080, 1920), [(89, 214, 193)] * 2))
        self.gate = StudioGate(self.studio)
        self.cookie, self.csrf = self.session()
        self.count = 0

    def respond(self, method, target, headers=None, body=b"", studio="gate"):
        base = {"host": f"127.0.0.1:{PORT}"}
        base.update(headers or {})
        return api.respond(method, target, base, port=PORT, root=self.root,
                           studio=self.gate if studio == "gate" else studio, body=body)

    def session(self):
        status, headers, body = self.respond("GET", "/api/studio/session")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return cookie, json.loads(body)["csrf"]

    def get(self, target):
        status, _, body = self.respond("GET", target)
        return status, json.loads(body)

    def post(self, action, payload, headers=None, request_id=None, raw=None):
        self.count += 1
        document = dict(payload, request_id=request_id or f"req-test-{self.count:06d}")
        base = {"origin": ORIGIN, "cookie": f"vk_studio={self.cookie}", "x-vk-studio-csrf": self.csrf,
                "content-type": "application/json", "sec-fetch-site": "same-origin"}
        base.update(headers or {})
        base = {k: v for k, v in base.items() if v is not None}
        status, _, body = self.respond("POST", "/api/studio/" + action, base,
                                       raw if raw is not None else json.dumps(document).encode())
        return status, json.loads(body)

    def ok(self, action, payload, **kwargs):
        status, doc = self.post(action, payload, **kwargs)
        self.assertEqual(status, 200, doc)
        return doc

    def speech_ready(self, timestamps=True):
        job = self.ok("speech/prepare", {"production_id": self.pid, "with_timestamps": timestamps})
        return self.ok("speech/submit", {"job_id": job["job_id"], "consent": job["consent_phrase"]})

    def workflow(self, captions="provider"):
        job = self.speech_ready()
        caption = self.ok("captions/prepare", {"speech_job_id": job["job_id"], "timing": captions}) if captions else None
        return job, self.ok("workflow/start", {"production_id": self.pid, "narration": "speech",
                                               "speech_job_id": job["job_id"],
                                               "caption_id": caption["caption_id"] if caption else None})

    def submit_all(self, doc):
        for action in [a for a in doc["actions"] if a["action"] == "workflow_submit"]:
            doc = self.ok("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": action["scene"],
                                              "consent": action["consent"]})
        return doc

    def to_review_doc(self, captions="provider"):
        _, doc = self.workflow(captions)
        doc = self.submit_all(doc)
        doc = self.ok("workflow/resume", {"workflow_id": doc["workflow_id"], "allow_network": True})
        self.assertEqual(doc["status"], "waiting_for_review", doc)
        return doc

    def approve(self, doc):
        review = doc["review"]
        return self.ok("review/record", {"workflow_id": doc["workflow_id"], "decision": "approved_for_preview",
                                         "reviewer": "Test Reviewer", "binding": review["binding_digest"],
                                         "acknowledgments": [a["id"] for a in review["applicable_acknowledgments"]],
                                         "supersedes": review["latest_review_id"]})


class FlowTests(StudioBase):
    def test_creation_to_verified_export(self):
        status, listing = self.get("/api/studio/productions")
        row = next(p for p in listing["productions"] if p["production_id"] == self.pid)
        self.assertTrue(row["eligible"])
        status, production = self.get(f"/api/studio/production?id={self.pid}")
        self.assertEqual(len(production["script"]["beats"]), 4)
        self.assertEqual([s["index"] for s in production["scene_plan"]["scenes"]], [1, 2, 3, 4])
        self.assertEqual(production["script"]["beats"][0]["narration"], self.script_beat(0))

        job = self.ok("speech/prepare", {"production_id": self.pid, "with_timestamps": True})
        self.assertEqual(job["status"], "prepared")
        self.assertEqual(self.timed.bodies, [])                         # preparing is free
        self.assertEqual(job["actions"][0]["consent"], job["consent_phrase"])
        job = self.ok("speech/submit", {"job_id": job["job_id"], "consent": job["consent_phrase"]})
        self.assertEqual((job["status"], len(self.timed.bodies)), ("completed", 1))
        caption = self.ok("captions/prepare", {"speech_job_id": job["job_id"], "timing": "provider"})
        doc = self.ok("workflow/start", {"production_id": self.pid, "narration": "speech", "speech_job_id": job["job_id"],
                                         "caption_id": caption["caption_id"], "resolution": "720p"})
        self.assertEqual(doc["status"], "waiting_for_consent")
        self.assertEqual(self.grok.posts, [])                           # starting never pays
        paid = [a for a in doc["actions"] if a["action"] == "workflow_submit"]
        self.assertEqual([a["scene"] for a in paid], [1, 2, 3, 4])
        self.assertTrue(all(a["paid"] for a in paid))
        for scene in doc["scenes"]:
            self.assertEqual(set(scene["request"]), {"model", "prompt", "duration", "aspect_ratio", "resolution", "generate_audio"})
        doc = self.submit_all(doc)
        self.assertEqual(len(self.grok.posts), 4)
        doc = self.ok("workflow/resume", {"workflow_id": doc["workflow_id"], "allow_network": True})
        self.assertEqual(doc["status"], "waiting_for_review")
        self.assertEqual(doc["quality"]["result"], "pass")
        self.assertTrue(doc["video"]["media_id"].startswith("med-"))
        self.assertTrue(doc["video"]["captions"])
        status, _, _ = self.respond("GET", f"/api/content/media?production={self.pid}&id={doc['video']['media_id']}",
                                    {"range": "bytes=0-10"})
        self.assertEqual(status, 206)                                    # playable through the read-only route
        actions = {a["action"] for a in doc["actions"]}
        self.assertIn("review_record", actions)
        self.assertNotIn("approved_preview", [a.get("purpose") for a in doc["actions"]])
        status, error = self.post("workflow/export", {"workflow_id": doc["workflow_id"], "purpose": "approved_preview"})
        self.assertEqual((status, error["error"]["code"]), (409, "no_current_preview_approval"))
        doc = self.approve(doc)
        self.assertEqual(doc["status"], "ready_to_export")
        self.assertTrue(doc["review"]["decisions"][0]["current_preview_approval"])
        doc = self.ok("workflow/export", {"workflow_id": doc["workflow_id"], "purpose": "approved_preview"})
        self.assertEqual(doc["status"], "exported")
        self.assertTrue(doc["export"]["verified"])
        self.assertIs(doc["export"]["publishable"], False)
        package = doc["export"]["package_id"]
        status, headers, data = self.respond("GET", f"/api/studio/export?workflow={doc['workflow_id']}&package={package}")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/zip"))
        self.assertIn("attachment", headers["Content-Disposition"])
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        self.assertIn(f"{package}/package.json", names)
        self.assertIn(f"{package}/captions/captions.srt", names)
        video = archive.read(f"{package}/media/preview.mp4")
        self.assertEqual(video, (self.root / "runtime/productions" / self.pid / self.preview()["preview_file"]).read_bytes())
        self.assertEqual(len(self.grok.posts), 4)                       # nothing was ever resubmitted

    def script_beat(self, index):
        return sj.production_script(self.pid, self.root)["beats"][index]["narration"]

    def test_silent_workflow_and_review_copy(self):
        doc = self.ok("workflow/start", {"production_id": self.pid, "narration": "silent"})
        doc = self.submit_all(doc)
        doc = self.ok("workflow/resume", {"workflow_id": doc["workflow_id"], "allow_network": True})
        self.assertEqual(doc["narration"]["present"], False)
        doc = self.ok("workflow/export", {"workflow_id": doc["workflow_id"], "purpose": "review_copy"})
        self.assertEqual(doc["export"]["purpose"], "review_copy")
        self.assertEqual(doc["status"], "waiting_for_review")           # a review copy is not an approval
        status, error = self.post("workflow/start", {"production_id": self.pid, "narration": "silent",
                                                     "caption_id": "cap-" + "0" * 24})
        self.assertEqual((status, error["error"]["code"]), (400, "invalid_narration_choice"))

    def test_ineligible_productions_are_explained(self):
        from vicekrack.production import ProductionStore
        state = ProductionStore(self.root).read(self.pid)
        script = self.root / "runtime/productions" / self.pid / next(
            s for s in state["stages"] if s["name"] == "creator")["artifacts"]["script_path"]
        script.write_text(script.read_text() + " ")
        status, listing = self.get("/api/studio/productions")
        row = listing["productions"][0]
        self.assertFalse(row["eligible"])
        self.assertEqual(row["reasons"][0]["code"], "artifact_changed")
        self.assertIn("changed after it was written", row["reasons"][0]["text"])
        status, error = self.post("workflow/start", {"production_id": self.pid, "narration": "silent"})
        self.assertEqual((status, error["error"]["code"]), (409, "production_not_eligible"))
        status, error = self.post("speech/prepare", {"production_id": self.pid})
        self.assertEqual(error["error"]["code"], "speech_unavailable")
        self.assertEqual(self.get(f"/api/studio/production?id=prod-{'0' * 24}")[1]["reasons"][0]["code"],
                         "production_not_found")


class ConsentAndDuplicateTests(StudioBase):
    def test_missing_or_wrong_consent_never_pays(self):
        job = self.ok("speech/prepare", {"production_id": self.pid})
        status, error = self.post("speech/submit", {"job_id": job["job_id"]})
        self.assertEqual((status, error["error"]["code"]), (400, "invalid_studio_request"))
        status, error = self.post("speech/submit", {"job_id": job["job_id"], "consent": "paid-speech:sp-" + "0" * 24})
        self.assertEqual(error["error"]["code"], "speech_consent_required")
        status, error = self.post("speech/submit", {"job_id": job["job_id"], "consent": "yes please"})
        self.assertEqual(error["error"]["code"], "invalid_studio_request")
        self.assertEqual(self.timed.bodies, [])
        doc = self.ok("workflow/start", {"production_id": self.pid, "narration": "silent"})
        scene1 = doc["scenes"][0]
        for consent in (None, doc["scenes"][1]["consent"], "paid-generate:vid-" + "0" * 24):
            status, error = self.post("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 1, "consent": consent})
            self.assertIn(status, (400, 409), error)
        self.assertEqual(self.grok.posts, [])
        self.ok("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 1, "consent": scene1["consent"]})
        self.assertEqual(len(self.grok.posts), 1)

    def test_duplicate_clicks_and_concurrent_actions(self):
        doc = self.ok("workflow/start", {"production_id": self.pid, "narration": "silent"})
        consent = doc["scenes"][0]["consent"]
        first = self.ok("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 1, "consent": consent},
                        request_id="req-same-click-0001")
        again = self.ok("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 1, "consent": consent},
                        request_id="req-same-click-0001")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["status"], first["status"])
        self.assertEqual(len(self.grok.posts), 1)                       # the same click is never performed twice
        status, error = self.post("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 1, "consent": consent})
        self.assertEqual((status, error["error"]["code"]), (409, "video_submit_refused"))
        self.assertEqual(len(self.grok.posts), 1)                       # a second click cannot pay again
        self.assertTrue(self.gate.lock.acquire())
        try:
            status, error = self.post("workflow/resume", {"workflow_id": doc["workflow_id"]})
            self.assertEqual((status, error["error"]["code"]), (409, "studio_busy"))
        finally:
            self.gate.lock.release()
        with vp.WorkflowStore(self.root).lock(doc["workflow_id"]):          # e.g. a terminal command meanwhile
            status, error = self.post("workflow/resume", {"workflow_id": doc["workflow_id"]})
            self.assertEqual(error["error"]["code"], "workflow_busy")


class RecoveryTests(StudioBase):
    def test_provider_failure_and_scene_replacement(self):
        doc = self.ok("workflow/start", {"production_id": self.pid, "narration": "silent"})
        doc = self.submit_all(doc)
        self.grok.status_script["req-2"] = ["failed"]
        doc = self.ok("workflow/resume", {"workflow_id": doc["workflow_id"], "allow_network": True})
        self.assertEqual(doc["status"], "failed")
        self.assertEqual([a for a in doc["actions"] if a["action"] == "workflow_retry_scene"],
                         [{"action": "workflow_retry_scene", "scene": 2, "paid": False}])
        posts = len(self.grok.posts)
        doc = self.ok("workflow/retry-scene", {"workflow_id": doc["workflow_id"], "scene": 2,
                                               "model": "grok-imagine-video-1.5-lite"})
        self.assertEqual(doc["status"], "waiting_for_consent")
        self.assertEqual(len(self.grok.posts), posts)                   # replacing is free; it needs new consent
        action = next(a for a in doc["actions"] if a["action"] == "workflow_submit")
        self.assertEqual(action["scene"], 2)
        self.ok("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 2, "consent": action["consent"]})
        doc = self.ok("workflow/resume", {"workflow_id": doc["workflow_id"], "allow_network": True})
        self.assertEqual(doc["status"], "waiting_for_review")

    def test_uncertain_submissions_need_acknowledged_retry(self):
        doc = self.ok("workflow/start", {"production_id": self.pid, "narration": "silent"})
        self.grok.post_effects = [TimeoutError("private provider detail")]
        consent = doc["scenes"][0]["consent"]
        doc = self.ok("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 1, "consent": consent})
        self.assertEqual(doc["status"], "uncertain_submission")
        risky = next(a for a in doc["actions"] if a.get("retry_uncertain"))
        self.assertTrue(risky["duplicate_billing_risk"])
        self.assertNotIn("private provider detail", json.dumps(doc))
        status, error = self.post("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 1, "consent": consent})
        self.assertEqual(error["error"]["code"], "video_submit_refused")
        status, error = self.post("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 1, "consent": consent,
                                                      "retry_uncertain": True})
        self.assertEqual(error["error"]["code"], "duplicate_billing_ack_required")
        self.assertEqual(len(self.grok.posts), 1)
        self.ok("workflow/submit", {"workflow_id": doc["workflow_id"], "scene": 1, "consent": consent,
                                    "retry_uncertain": True, "acknowledge_duplicate_billing": True})
        self.assertEqual(len(self.grok.posts), 2)

    def test_speech_uncertain_and_recovery(self):
        self.studio.speech_transport = TimedSpeech()
        job = self.ok("speech/prepare", {"production_id": self.pid, "with_timestamps": True})
        with patch.object(self.studio, "speech_transport", side_effect=TimeoutError("private")):
            status, error = self.post("speech/submit", {"job_id": job["job_id"], "consent": job["consent_phrase"]})
        self.assertEqual((status, error["error"]["code"]), (409, "speech_submit_uncertain"))
        self.assertIn("NOT retried", error["error"]["message"])
        production = self.get(f"/api/studio/production?id={self.pid}")[1]
        row = next(j for j in production["speech_jobs"] if j["job_id"] == job["job_id"])
        self.assertTrue(row["actions"][0]["duplicate_billing_risk"])
        status, error = self.post("speech/submit", {"job_id": job["job_id"], "consent": job["consent_phrase"]})
        self.assertEqual(error["error"]["code"], "speech_submit_refused")
        done = self.ok("speech/submit", {"job_id": job["job_id"], "consent": job["consent_phrase"], "retry_uncertain": True,
                                         "acknowledge_duplicate_billing": True})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.ok("speech/recover", {"job_id": job["job_id"]})["status"], "completed")

    def test_unexpected_errors_are_sanitized(self):
        doc = self.ok("workflow/start", {"production_id": self.pid, "narration": "silent"})
        with patch.object(Studio, "workflow_resume", side_effect=RuntimeError("secret /home/user " + KEY)):
            status, error = self.post("workflow/resume", {"workflow_id": doc["workflow_id"]})
        self.assertEqual((status, error["error"]["code"]), (500, "studio_error"))
        self.assertNotIn("/home", json.dumps(error))
        self.assertNotIn(KEY, json.dumps(error))


class StaleApprovalTests(StudioBase):
    def test_a_newer_video_makes_the_old_approval_stale(self):
        first = self.to_review_doc("provider")
        first = self.approve(first)
        self.assertEqual(first["status"], "ready_to_export")
        job = next(j for j in self.get(f"/api/studio/production?id={self.pid}")[1]["speech_jobs"] if j["status"] == "completed")
        estimated = self.ok("captions/prepare", {"speech_job_id": job["job_id"], "timing": "estimated"})
        second = self.ok("workflow/start", {"production_id": self.pid, "narration": "speech", "speech_job_id": job["job_id"],
                                            "caption_id": estimated["caption_id"]})
        self.assertEqual(second["status"], "waiting_for_review")
        self.assertEqual(second["quality"]["result"], "needs_review")
        self.assertIn("needs_review_result", [a["id"] for a in second["review"]["applicable_acknowledgments"]])
        old = self.get(f"/api/studio/workflow?id={first['workflow_id']}")[1]
        self.assertTrue(old["video"]["superseded"])
        self.assertTrue(old["integrity_problems"])                       # production_preview_changed
        status, error = self.post("workflow/export", {"workflow_id": first["workflow_id"], "purpose": "approved_preview"})
        self.assertEqual((status, error["error"]["code"]), (409, "production_preview_changed"))
        status, error = self.post("workflow/export", {"workflow_id": second["workflow_id"], "purpose": "approved_preview"})
        self.assertEqual(error["error"]["code"], "no_current_preview_approval")
        review = second["review"]
        status, error = self.post("review/record", {"workflow_id": second["workflow_id"], "decision": "approved_for_preview",
                                                    "reviewer": "Test", "binding": review["binding_digest"],
                                                    "supersedes": review["latest_review_id"]})
        self.assertEqual(error["error"]["code"], "acknowledgment_required")   # estimated captions: acknowledge first
        status, error = self.post("review/record", {"workflow_id": second["workflow_id"], "decision": "approved_for_preview",
                                                    "reviewer": "Test", "binding": "0" * 64,
                                                    "acknowledgments": ["needs_review_result"],
                                                    "supersedes": review["latest_review_id"]})
        self.assertIn(status, (400, 409))
        rejected = self.ok("review/record", {"workflow_id": second["workflow_id"], "decision": "rejected", "reviewer": "Test",
                                             "binding": review["binding_digest"], "supersedes": review["latest_review_id"]})
        self.assertEqual(rejected["status"], "review_rejected")


class SecurityTests(StudioBase):
    def test_requests_must_come_from_this_page_with_the_session(self):
        payload = {"production_id": self.pid, "narration": "silent"}
        cases = [({"origin": None}, 403), ({"origin": "http://evil.example"}, 403), ({"origin": "null"}, 403),
                 ({"sec-fetch-site": "cross-site"}, 403), ({"sec-fetch-site": "same-site"}, 403),
                 ({"cookie": None}, 403), ({"cookie": "vk_studio=forged"}, 403), ({"x-vk-studio-csrf": None}, 403),
                 ({"x-vk-studio-csrf": "forged"}, 403), ({"content-type": "text/plain"}, 400),
                 ({"host": "attacker.example"}, 403), ({"host": "127.0.0.1:9999"}, 403)]
        for headers, expected in cases:
            status, error = self.post("workflow/start", payload, headers=headers)
            self.assertEqual(status, expected, (headers, error))
        self.assertEqual(vp.WorkflowStore(self.root).ids(), [])          # nothing was started
        other = StudioGate(self.studio)                                  # a restarted server: old tokens are dead
        self.gate = other
        self.assertEqual(self.post("workflow/start", payload)[0], 403)

    def test_request_bodies_are_bounded_and_strict(self):
        bad = [{"production_id": "../../etc", "narration": "silent"},
               {"production_id": self.pid, "narration": "silent", "path": "/tmp/x"},
               {"production_id": self.pid, "narration": "local"},
               {"production_id": self.pid, "narration": "silent", "model": "other-model"},
               {"production_id": self.pid}]
        for payload in bad:
            status, error = self.post("workflow/start", payload)
            self.assertEqual((status, error["error"]["code"]), (400, "invalid_studio_request"), payload)
        self.assertEqual(self.post("workflow/start", {}, raw=b"{not json")[1]["error"]["code"], "invalid_studio_request")
        self.assertEqual(self.post("workflow/start", {}, raw=b"[]")[1]["error"]["code"], "invalid_studio_request")
        status, error = self.post("workflow/start", {}, raw=b'{"a": "' + b"x" * 20000 + b'"}')
        self.assertEqual((status, error["error"]["code"]), (413, "studio_request_too_large"))
        status, error = self.post("run/command", {"cmd": "rm -rf /"})
        self.assertEqual((status, error["error"]["code"]), (404, "studio_action_not_found"))
        self.assertEqual(self.post("workflow/start", {"production_id": self.pid, "narration": "silent"},
                                   request_id="../x")[1]["error"]["code"], "invalid_studio_request")
        for target in ("/api/studio/workflow?id=../x", "/api/studio/production?id=prod-1",
                       "/api/studio/export?workflow=vpw-" + "0" * 24 + "&package=../../x", "/api/studio/files?path=/etc"):
            self.assertIn(self.respond("GET", target)[0], (400, 404), target)
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.respond(method, "/api/studio/workflow/start")[0], 405)

    def test_read_only_server_and_existing_routes_are_unchanged(self):
        status, _, body = self.respond("GET", "/api/studio/session", studio=None)
        self.assertEqual((status, json.loads(body)["actions_enabled"]), (200, False))
        self.assertEqual(self.respond("GET", "/api/studio/productions", studio=None)[0], 403)
        status, error = self.post("workflow/start", {"production_id": self.pid, "narration": "silent"})
        self.gate, gate = None, self.gate
        try:
            status, _, _ = self.respond("POST", "/api/studio/workflow/start", {"origin": ORIGIN}, b"{}", studio=None)
            self.assertEqual(status, 405)
        finally:
            self.gate = gate
        self.assertEqual(json.loads(self.respond("GET", "/api/status", studio=None)[2])["read_only"], True)
        self.assertEqual(json.loads(self.respond("GET", "/api/status")[2])["studio_actions"], True)
        for target in ("/api/scene?timeline=demo", "/api/timelines", "/api/broker-paper"):
            self.assertEqual(self.respond("POST", target, {"origin": ORIGIN})[0], 405)   # still read-only
            self.assertEqual(self.respond("GET", target)[0], 200)
        app = (ROOT / "vicekrack/hq/static/app.js").read_text(encoding="utf-8")
        self.assertNotIn("POST", app)
        studio_js = (ROOT / "vicekrack/hq/static/studio.js").read_text(encoding="utf-8")
        for forbidden in ("innerHTML", "localStorage", "sessionStorage", "setInterval", "eval(", "XAI_API_KEY ="):
            self.assertNotIn(forbidden, studio_js)

    def test_credentials_never_reach_the_browser(self):
        with patch.dict(os.environ, {"XAI_API_KEY": KEY}):
            doc = self.to_review_doc("provider")
            texts = [json.dumps(doc)]
            for target in ("/api/studio/session", "/api/studio/productions", f"/api/studio/production?id={self.pid}",
                           f"/api/studio/workflow?id={doc['workflow_id']}"):
                status, _, body = self.respond("GET", target)
                self.assertEqual(status, 200)
                texts.append(body.decode())
            session = json.loads(texts[1])
            self.assertEqual(session["credentials"], {"xai_configured": True, "source": "XAI_API_KEY environment variable"})
            for text in texts:
                self.assertNotIn(KEY, text)
                self.assertNotIn(str(self.root), text)
                self.assertNotIn("download_url", text)
                self.assertNotIn("vidgen", text)
            with patch.object(Studio, "productions", return_value={"leak": KEY}):
                status, _, body = self.respond("GET", "/api/studio/productions")
            self.assertEqual(status, 500)
            self.assertNotIn(KEY.encode(), body)
        with patch.dict(os.environ, {"XAI_API_KEY": ""}):
            self.assertFalse(json.loads(self.respond("GET", "/api/studio/session")[2])["credentials"]["xai_configured"])

    def test_server_reads_bodies_only_for_studio_posts(self):
        from vicekrack.hq.server import HQServer
        server = HQServer(0, self.root, self.studio)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertIsInstance(server.studio, StudioGate)
        server.server_close()
        plain = HQServer(0, self.root)
        self.assertIsNone(plain.studio)
        plain.server_close()


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "set RUN_LOCAL_RENDER_TESTS=1 (real render)")
class DemoTests(unittest.TestCase):
    def test_demo_is_isolated_offline_and_labelled(self):
        from vicekrack.hq.studio_demo import demo_studio
        from vicekrack.production import ProductionStore
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack, \
                patch("socket.socket.connect", side_effect=AssertionError("network disabled in tests")):
            os.environ.pop("XAI_API_KEY", None)
            studio, folder = demo_studio(stack, Path(temp) / "demo")
            self.assertTrue(folder.is_relative_to(Path(temp).resolve()))
            self.assertTrue(studio.demo)
            self.assertIn("STUDIO DEMO", studio.session_info()["notice"])
            self.assertEqual(len(ProductionStore(folder).list_ids()), 1)
            listing = studio.productions()
            self.assertTrue(listing["demo"] and listing["productions"][0]["eligible"])
        self.assertNotIn("XAI_API_KEY", os.environ)


@unittest.skipUnless(os.environ.get("RUN_LOCAL_BROWSER_TESTS") == "1" and os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1",
                     "set RUN_LOCAL_BROWSER_TESTS=1 and RUN_LOCAL_RENDER_TESTS=1 (Playwright + Chromium + FFmpeg)")
class StudioBrowserTests(unittest.TestCase):
    def test_studio_screen_from_production_to_download(self):
        from playwright.sync_api import sync_playwright
        from vicekrack.hq.server import HQServer
        from vicekrack.hq.studio_demo import demo_studio
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            os.environ.pop("XAI_API_KEY", None)
            studio, folder = demo_studio(stack, Path(temp) / "demo")
            server = HQServer(0, folder, studio)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                    problems, posts = [], []
                    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
                    page.on("pageerror", lambda e: problems.append(str(e)))
                    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
                    idle = "#studio-view[data-busy=false]"
                    page.goto(url)
                    page.wait_for_selector(".bot[data-bot='creator']")
                    self.assertEqual(posts, [])                              # nothing acts on load
                    page.click(".view-btn[data-view='studio']")
                    page.wait_for_selector("text=Script and scene plan")
                    self.assertIn("DEMO", page.inner_text("#studio-badge"))
                    self.assertEqual(posts, [])                              # opening the Studio acts on nothing
                    page.check("#narration-speech")
                    page.click("#studio-speech-prepare")
                    page.wait_for_selector(".paid-box")
                    send = page.locator(".paid-box button").first
                    self.assertTrue(send.is_disabled())                      # no approval ticked: cannot send
                    page.locator(".paid-box input[type=checkbox]").first.check()
                    send.dblclick()                                          # a double click is one request
                    page.wait_for_selector("text=Use this narration")
                    page.wait_for_selector(idle)
                    self.assertEqual(sum(1 for u in posts if u.endswith("/speech/submit")), 1)
                    self.assertEqual(studio.speech_transport.calls, 1)
                    page.check("#captions-provider")
                    page.click("#studio-captions-prepare")
                    page.wait_for_selector("text=Caption track")
                    page.click("#studio-start")
                    page.wait_for_selector("text=Generation progress")
                    self.assertEqual(studio.video_transport.posts, 0)
                    for _ in range(4):
                        page.locator(".paid-box input[type=checkbox]").first.check()
                        page.locator(".paid-box button").first.click()
                        page.wait_for_selector(idle)
                    self.assertEqual(studio.video_transport.posts, 4)
                    page.click("button[data-action=resume]")
                    page.wait_for_selector(idle)
                    self.assertIn("generated or not downloaded yet", page.inner_text("#studio-body"))   # mock: scene 3 pending
                    page.click("button[data-action=resume]")
                    page.wait_for_selector("#studio-video", timeout=180000)
                    page.wait_for_selector(idle)
                    self.assertIn("pass", page.inner_text("#studio-body"))
                    page.click("#studio-approve")                            # no name: refused in the page
                    self.assertIn("Enter your name", page.inner_text("#studio-message"))
                    page.fill("#studio-reviewer", "Browser Test Reviewer")
                    for box in page.locator("input[data-ack]").all():
                        box.check()
                    page.click("#studio-approve")
                    page.wait_for_selector("button[data-purpose=approved_preview]")
                    page.click("button[data-purpose=approved_preview]")
                    page.wait_for_selector("#studio-download")
                    with page.expect_download() as download:
                        page.click("#studio-download")
                    data = Path(download.value.path()).read_bytes()
                    self.assertTrue(data.startswith(b"PK"))
                    self.assertEqual(studio.video_transport.posts, 4)
                    # An unauthorized request (no CSRF token) is refused, even from this origin.
                    response = page.request.post(url + "api/studio/workflow/resume", data="{}", headers={
                        "Content-Type": "application/json", "Origin": url.rstrip("/")})
                    self.assertEqual(response.status, 403)
                    self.assertEqual(problems, [])
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 1440)
                    phone = browser.new_page(viewport={"width": 390, "height": 844})
                    phone.goto(url)
                    phone.wait_for_selector(".bot[data-bot='creator']")
                    phone.click(".view-btn[data-view='studio']")
                    phone.wait_for_selector("text=Script and scene plan")
                    self.assertLessEqual(phone.evaluate("document.documentElement.scrollWidth"), 390)
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
