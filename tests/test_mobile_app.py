"""Mobile Command Center (PR 2): app files, Home and System summaries. No network."""

import json
import tempfile
import unittest
from datetime import datetime, timezone

from vicekrack.errors import NetworkError
from vicekrack.mobile import security, summary
from vicekrack.mobile.gateway import Gateway
from vicekrack.mobile.store import MobileStore

NOW = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)
WF_WAIT, WF_RUN, WF_REVIEW, WF_FAIL = ("vpw-" + c * 24 for c in "abcd")
PROD = "prod-" + "e" * 24


class RecordsHQ:
    """Answers like the HQ's Studio reads for a small set of saved records."""

    def __init__(self):
        self.down = False
        self.reads = []

    def get(self, path):
        if self.down:
            raise NetworkError("hq_offline", "x")
        self.reads.append(path)
        if path == "/api/studio/research/jobs":
            return 200, {"jobs": [
                {"job_id": "rsj-" + "1" * 24, "topics": ["GTA 6"], "status": "prepared", "created_at": "2026-10-15T15:00:00Z"},
                {"job_id": "rsj-" + "2" * 24, "topics": ["NBA"], "status": "submitting", "created_at": "2026-10-15T15:10:00Z"},
                {"job_id": "rsj-" + "3" * 24, "topics": ["Old"], "status": "completed", "created_at": "2026-10-14T15:00:00Z"},
                {"job_id": "rsj-" + "4" * 24, "topics": ["Bad"], "status": "rejected", "created_at": "2026-10-14T15:00:00Z"}]}
        if path.startswith("/api/studio/research/job?"):
            return 200, {"estimate_usd": {"low": 0.05, "high": 0.12}}
        if path == "/api/studio/productions":
            return 200, {"productions": [{"production_id": PROD, "title": "Trailer date", "workflows": 4,
                                          "updated_at": "2026-10-15T15:30:00Z"},
                                         {"production_id": "prod-" + "f" * 24, "title": "No videos", "workflows": 0}]}
        if path.startswith("/api/studio/production?"):
            return 200, {"workflows": [{"workflow_id": WF_WAIT, "status": "waiting_for_consent", "updated_at": "2026-10-15T15:40:00Z"},
                                       {"workflow_id": WF_RUN, "status": "waiting_for_provider", "updated_at": "2026-10-15T15:41:00Z"},
                                       {"workflow_id": WF_REVIEW, "status": "waiting_for_review", "updated_at": "2026-10-15T15:42:00Z"},
                                       {"workflow_id": WF_FAIL, "status": "failed", "updated_at": "2026-10-15T15:43:00Z"}]}
        if path == f"/api/studio/workflow?id={WF_WAIT}":
            return 200, {"actions": [{"action": "workflow_submit_all", "estimate_usd": 0.8, "scenes": [1, 2, 3, 4]}]}
        if path == f"/api/studio/workflow?id={WF_RUN}":
            return 200, {"stages": [{"name": "scene_plan", "status": "completed"}, {"name": "generate", "status": "running"},
                                    {"name": "render", "status": "pending"}, {"name": "quality", "status": "pending"}],
                         "actions": []}
        if path.startswith("/api/studio/workflow?"):
            return 200, {"actions": []}
        if path == "/api/studio/overview":
            return 200, {"activity": [{"at": "2026-10-15T15:43:00Z", "agent": "nia", "text": "Video workflow vpw-dddd: failed"}]}
        return 404, {"error": {"code": "not_found"}}

    def refresh_session(self):
        return {"actions_enabled": True, "demo": False, "credentials": {"xai_configured": True}}


class Summaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MobileStore(self.tmp.name)
        self.store.save_settings(pin=security.hash_pin("2468"))
        self.hq = RecordsHQ()

    def tearDown(self):
        self.tmp.cleanup()

    def test_home_counts_come_from_records(self):
        doc = summary.home(self.hq, self.store, lambda: NOW)
        self.assertTrue(doc["hq_ok"])
        self.assertEqual(doc["counts"], {"running": 2, "waiting": 3, "finished": 1, "problems": 2})
        kinds = {i["kind"]: i for i in doc["needs_you"]}
        self.assertEqual(set(kinds), {"research_approval", "clips_approval", "video_review"})
        self.assertEqual(kinds["clips_approval"]["estimate_usd"], 0.8)
        self.assertEqual(kinds["clips_approval"]["clips"], 4)
        self.assertEqual(kinds["research_approval"]["estimate_usd"], 0.12)
        video = next(i for i in doc["live"] if i["kind"] == "video")
        self.assertEqual(video["progress"], {"done": 1, "total": 4, "stage": "generate"})
        self.assertEqual(len(doc["recent"]), 1)
        self.assertNotIn("/api/studio/production?id=prod-" + "f" * 24, self.hq.reads)   # no workflows: not read

    def test_home_when_hq_down_shows_nothing_invented(self):
        self.hq.down = True
        doc = summary.home(self.hq, self.store, lambda: NOW)
        self.assertFalse(doc["hq_ok"])
        self.assertEqual(doc["counts"], {"running": 0, "waiting": 0, "finished": 0, "problems": 0})
        self.assertEqual((doc["needs_you"], doc["live"], doc["recent"]), ([], [], []))

    def test_system_has_no_secrets(self):
        code = security.new_pairing_code(self.store, NOW)
        token, _ = security.redeem_pairing_code(self.store, code, "Rich iPhone", NOW)
        text = json.dumps(summary.system(self.store, lambda: NOW))
        self.assertIn("Rich iPhone", text)
        for secret in (token, "token_sha256", "salt", '"hash"'):
            self.assertNotIn(secret, text)


class AppFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MobileStore(self.tmp.name)
        self.gw = Gateway(self.store, RecordsHQ(), port=8770, clock=lambda: NOW)
        self.host = {"host": "127.0.0.1:8770"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_app_files_served(self):
        expected = {"/": "text/html", "/static/app.js": "text/javascript", "/static/app.css": "text/css",
                    "/sw.js": "text/javascript", "/manifest.webmanifest": "application/manifest+json",
                    "/static/icon-180.png": "image/png", "/static/icon-512.png": "image/png"}
        for path, kind in expected.items():
            status, headers, body = self.gw.respond("GET", path, self.host)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(kind), path)
            self.assertTrue(body, path)
        manifest = json.loads(self.gw.respond("GET", "/manifest.webmanifest", self.host)[2])
        self.assertEqual(manifest["display"], "standalone")

    def test_app_never_uses_inner_html(self):
        _, _, body = self.gw.respond("GET", "/static/app.js", self.host)
        self.assertNotIn(b"innerHTML", body)
        self.assertNotIn(b"insertAdjacentHTML", body)

    def test_service_worker_never_stores_live_data(self):
        _, _, body = self.gw.respond("GET", "/sw.js", self.host)
        self.assertIn(b"!SHELL.includes(url.pathname)", body)

    def test_home_and_system_need_pairing(self):
        for path in ("/m/api/home", "/m/api/system"):
            self.assertEqual(self.gw.respond("GET", path, self.host)[0], 401)

    def test_home_after_pairing(self):
        code = security.new_pairing_code(self.store, NOW)
        token, _ = security.redeem_pairing_code(self.store, code, "iPhone", NOW)
        status, _, body = self.gw.respond("GET", "/m/api/home", {**self.host, "cookie": f"vk_mobile={token}"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["contract"], "mobile_home")


if __name__ == "__main__":
    unittest.main()
