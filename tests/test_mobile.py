"""Mobile Command Center (PR 1): connection rules, pairing, PIN, daily cap, HQ client. No network."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vicekrack.errors import NetworkError
from vicekrack.mobile import security
from vicekrack.mobile.gateway import Gateway
from vicekrack.mobile.hq_client import HQClient
from vicekrack.mobile.store import MobileStore

PORT = 8770
REMOTE = "richpc.tail1234.ts.net"
OWNER = "richburtonjr@gmail.com"
NOW = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)          # 12:00 in New York


class FakeHQ:
    def __init__(self):
        self.posts, self.gets = [], []
        self.research_job = {"job_id": "rsj-" + "a" * 24, "estimate_usd": {"low": 0.05, "high": 0.12},
                             "cards": [{"make_video": {"estimate_usd": 0.03}}]}
        self.workflow = {"workflow_id": "vpw-" + "b" * 24,
                         "actions": [{"action": "workflow_submit_all", "estimate_usd": 0.8}],
                         "scenes": [{"scene_index": 1, "request": {"duration": 6, "model": "grok-imagine-video-1.5-lite"}}]}
        self.answer = (200, {"ok": True})
        self.offline = False

    def refresh_session(self):
        if self.offline:
            raise NetworkError("hq_offline", "x")
        return {"actions_enabled": True, "demo": False, "credentials": {"xai_configured": True}}

    def get(self, path):
        self.gets.append(path)
        if path.startswith("/api/studio/research/job"):
            return 200, self.research_job
        if path.startswith("/api/studio/workflow"):
            return 200, self.workflow
        return 200, {"path": path}

    def post(self, action, document):
        if self.offline:
            raise NetworkError("hq_offline", "x")
        self.posts.append((action, document))
        return self.answer


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MobileStore(self.tmp.name)
        self.store.save_settings(remote_host=REMOTE, owner_login=OWNER, pin=security.hash_pin("2468"))
        self.now = NOW
        self.hq = FakeHQ()
        self.gw = Gateway(self.store, self.hq, port=PORT, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def remote(self, extra=None):
        return {"host": REMOTE, "tailscale-user-login": OWNER, **(extra or {})}

    def call(self, method, path, body=None, headers=None):
        headers = dict(headers if headers is not None else self.remote())
        data = b""
        if body is not None:
            data = json.dumps(body).encode()
            headers.setdefault("content-type", "application/json")
        if method == "POST":
            headers.setdefault("origin", f"https://{REMOTE}")
        status, response_headers, raw = self.gw.respond(method, path, headers, data)
        try:
            document = json.loads(raw)
        except ValueError:
            document = None
        return status, response_headers, document

    def paired(self):
        code = security.new_pairing_code(self.store, self.now)
        status, headers, doc = self.call("POST", "/m/api/pair", {"code": code, "name": "Rich iPhone"})
        self.assertEqual(status, 200, doc)
        token = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        return self.remote({"cookie": f"vk_mobile={token}", "x-vk-mobile-csrf": doc["csrf"]})


class ConnectionRules(Base):
    def test_unknown_host_refused(self):
        status, _, doc = self.call("GET", "/m/api/status", headers={"host": "evil.example"})
        self.assertEqual((status, doc["error"]["code"]), (403, "forbidden_host"))

    def test_remote_needs_owner_login(self):
        status, _, doc = self.call("GET", "/m/api/status", headers={"host": REMOTE})
        self.assertEqual(doc["error"]["code"], "not_owner")
        status, _, doc = self.call("GET", "/m/api/status", headers={"host": REMOTE, "tailscale-user-login": "x@y.z"})
        self.assertEqual(doc["error"]["code"], "not_owner")

    def test_remote_off_until_setup(self):
        self.store.save_settings(remote_host=None, owner_login=None)
        status, _, doc = self.call("GET", "/m/api/status",
                                   headers={"host": f"127.0.0.1:{PORT}", "tailscale-user-login": OWNER})
        self.assertEqual(doc["error"]["code"], "remote_not_set_up")

    def test_loopback_on_pc_works(self):
        status, _, doc = self.call("GET", "/m/api/status", headers={"host": f"127.0.0.1:{PORT}"})
        self.assertEqual((status, doc["paired"]), (200, False))

    def test_post_needs_matching_origin(self):
        headers = self.paired()
        status, _, doc = self.call("POST", "/m/api/unpair", {}, headers={**headers, "origin": "https://evil.example"})
        self.assertEqual(doc["error"]["code"], "forbidden_origin")
        status, _, doc = self.call("POST", "/m/api/unpair", {}, headers={**headers, "sec-fetch-site": "cross-site"})
        self.assertEqual(doc["error"]["code"], "forbidden_origin")

    def test_page_served_with_strict_headers(self):
        status, headers, _ = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_unpaired_status_reveals_nothing(self):
        status, _, doc = self.call("GET", "/m/api/status")
        self.assertEqual(doc, {"contract": "mobile_status", "paired": False})


class Pairing(Base):
    def test_pair_sets_secure_cookie_and_status_works(self):
        headers = self.paired()
        status, _, doc = self.call("GET", "/m/api/status", headers=headers)
        self.assertTrue(doc["paired"])
        self.assertEqual(doc["hq"]["mode"], "real")
        self.assertEqual(doc["connection"]["via"], "tailscale")
        self.assertEqual(doc["spending"]["cap_usd"], 5.0)

    def test_cookie_flags(self):
        code = security.new_pairing_code(self.store, self.now)
        _, headers, _ = self.call("POST", "/m/api/pair", {"code": code})
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=Strict"):
            self.assertIn(flag, cookie)

    def test_only_one_phone_can_be_paired(self):
        first = security.new_pairing_code(self.store, self.now)
        self.call("POST", "/m/api/pair", {"code": first})
        second = security.new_pairing_code(self.store, self.now)
        self.assertEqual(self.call("POST", "/m/api/pair", {"code": second})[0], 200)
        self.assertEqual(len(security.list_devices(self.store)), 1)

    def test_code_used_once(self):
        code = security.new_pairing_code(self.store, self.now)
        self.assertEqual(self.call("POST", "/m/api/pair", {"code": code})[0], 200)
        _, _, doc = self.call("POST", "/m/api/pair", {"code": code})
        self.assertEqual(doc["error"]["code"], "pairing_code_expired")

    def test_code_expires(self):
        code = security.new_pairing_code(self.store, self.now)
        self.now += timedelta(minutes=11)
        _, _, doc = self.call("POST", "/m/api/pair", {"code": code})
        self.assertEqual(doc["error"]["code"], "pairing_code_expired")

    def test_five_wrong_codes_burn_the_code(self):
        code = security.new_pairing_code(self.store, self.now)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.call("POST", "/m/api/pair", {"code": wrong})
        _, _, doc = self.call("POST", "/m/api/pair", {"code": code})
        self.assertEqual(doc["error"]["code"], "pairing_code_expired")

    def test_unpair_revokes(self):
        headers = self.paired()
        self.assertEqual(self.call("POST", "/m/api/unpair", {}, headers=headers)[0], 200)
        _, _, doc = self.call("GET", "/m/api/status", headers=headers)
        self.assertFalse(doc["paired"])

    def test_device_expires_after_90_days(self):
        headers = self.paired()
        self.now += timedelta(days=91)
        _, _, doc = self.call("GET", "/m/api/status", headers=headers)
        self.assertFalse(doc["paired"])

    def test_csrf_required(self):
        headers = self.paired()
        headers["x-vk-mobile-csrf"] = "nope"
        _, _, doc = self.call("POST", "/m/api/action/research/prepare", {"request_id": "req-00001", "topics": ["GTA 6"]},
                              headers=headers)
        self.assertEqual(doc["error"]["code"], "session_invalid")

    def test_secrets_not_stored_in_clear(self):
        headers = self.paired()
        token = headers["cookie"].split("=", 1)[1]
        folder = Path(self.tmp.name) / "runtime" / "mobile"
        for path in folder.iterdir():
            self.assertNotIn(token, path.read_text())
        pin = self.store.settings()["pin"]
        self.assertEqual(set(pin), {"salt", "hash", "n", "r", "p"})
        self.assertNotIn("2468", json.dumps({k: v for k, v in pin.items() if k not in ("salt", "hash")}))


class Actions(Base):
    def act(self, headers, action, body):
        return self.call("POST", f"/m/api/action/{action}", body, headers=headers)

    def test_free_action_forwarded_without_pin(self):
        headers = self.paired()
        status, _, doc = self.act(headers, "research/prepare", {"request_id": "req-00001", "topics": ["GTA 6"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.hq.posts, [("research/prepare", {"request_id": "req-00001", "topics": ["GTA 6"]})])

    def test_unknown_or_speech_actions_refused(self):
        headers = self.paired()
        for action in ("speech/submit", "captions/prepare", "nope"):
            self.assertEqual(self.act(headers, action, {"request_id": "req-00001"})[0], 404)
        self.assertEqual(self.hq.posts, [])

    def test_paid_needs_right_pin(self):
        headers = self.paired()
        body = {"request_id": "req-00002", "job_id": "rsj-" + "a" * 24, "consent": "paid-research:rsj-" + "a" * 24}
        _, _, doc = self.act(headers, "research/submit", dict(body))
        self.assertEqual(doc["error"]["code"], "pin_wrong")
        _, _, doc = self.act(headers, "research/submit", {**body, "pin": "1111"})
        self.assertEqual(doc["error"]["code"], "pin_wrong")
        status, _, doc = self.act(headers, "research/submit", {**body, "pin": "2468"})
        self.assertEqual(status, 200)
        self.assertNotIn("pin", self.hq.posts[0][1])
        self.assertAlmostEqual(security.spent_today(self.store, self.now), 0.12)

    def test_pin_lockout(self):
        headers = self.paired()
        body = {"request_id": "req-00003", "job_id": "rsj-" + "a" * 24, "consent": "c"}
        for _ in range(5):
            _, _, doc = self.act(headers, "research/submit", {**body, "pin": "0000"})
        self.assertEqual(doc["error"]["code"], "pin_locked")
        _, _, doc = self.act(headers, "research/submit", {**body, "pin": "2468"})
        self.assertEqual(doc["error"]["code"], "pin_locked")
        self.now += timedelta(minutes=16)
        self.assertEqual(self.act(headers, "research/submit", {**body, "pin": "2468"})[0], 200)

    def test_daily_cap(self):
        headers = self.paired()
        wf = "vpw-" + "b" * 24
        for n in range(6):                                   # 6 x $0.80 = $4.80
            status, _, doc = self.act(headers, "workflow/submit-all",
                                      {"request_id": f"req-cap-{n:04d}", "workflow_id": wf, "consent": "c", "pin": "2468"})
            self.assertEqual(status, 200, doc)
        _, _, doc = self.act(headers, "workflow/submit-all",
                             {"request_id": "req-cap-0099", "workflow_id": wf, "consent": "c", "pin": "2468"})
        self.assertEqual(doc["error"]["code"], "daily_cap_reached")
        self.assertEqual(len(self.hq.posts), 6)
        self.now += timedelta(days=1)                         # a new day in New York
        self.assertEqual(self.act(headers, "workflow/submit-all",
                                  {"request_id": "req-cap-0100", "workflow_id": wf, "consent": "c", "pin": "2468"})[0], 200)

    def test_repeat_request_not_counted_twice(self):
        headers = self.paired()
        body = {"request_id": "req-same-01", "workflow_id": "vpw-" + "b" * 24, "consent": "c", "pin": "2468"}
        self.act(headers, "workflow/submit-all", dict(body))
        self.act(headers, "workflow/submit-all", dict(body))
        self.assertAlmostEqual(security.spent_today(self.store, self.now), 0.8)

    def test_single_scene_estimate(self):
        headers = self.paired()
        self.act(headers, "workflow/submit", {"request_id": "req-scene-1", "workflow_id": "vpw-" + "b" * 24,
                                              "scene": 1, "consent": "c", "pin": "2468"})
        self.assertAlmostEqual(security.spent_today(self.store, self.now), 0.12)   # 6 s x $0.02

    def test_no_estimate_means_pc_only(self):
        headers = self.paired()
        self.hq.research_job = {"cards": []}
        _, _, doc = self.act(headers, "research/make-video", {"request_id": "req-00004", "job_id": "rsj-" + "a" * 24,
                                                              "story": 0, "consent": "c", "pin": "2468"})
        self.assertEqual(doc["error"]["code"], "cost_unknown")
        self.assertEqual(self.hq.posts, [])

    def test_uncertain_retry_is_pc_only(self):
        headers = self.paired()
        _, _, doc = self.act(headers, "workflow/submit", {"request_id": "req-00005", "workflow_id": "vpw-" + "b" * 24,
                                                          "scene": 1, "consent": "c", "pin": "2468",
                                                          "retry_uncertain": True})
        self.assertEqual(doc["error"]["code"], "pc_only")
        self.assertEqual(self.hq.posts, [])

    def test_refused_by_hq_gives_cap_back(self):
        headers = self.paired()
        self.hq.answer = (400, {"error": {"code": "research_consent_required", "message": "x"}})
        self.act(headers, "research/submit", {"request_id": "req-00006", "job_id": "rsj-" + "a" * 24,
                                              "consent": "bad", "pin": "2468"})
        self.assertEqual(security.spent_today(self.store, self.now), 0)

    def test_uncertain_answer_keeps_cap(self):
        headers = self.paired()
        self.hq.answer = (409, {"error": {"code": "research_submit_uncertain", "message": "x"}})
        self.act(headers, "research/submit", {"request_id": "req-00007", "job_id": "rsj-" + "a" * 24,
                                              "consent": "c", "pin": "2468"})
        self.assertAlmostEqual(security.spent_today(self.store, self.now), 0.12)

    def test_hq_offline_gives_cap_back(self):
        headers = self.paired()
        self.hq.offline = True
        status, _, doc = self.act(headers, "research/submit", {"request_id": "req-00008", "job_id": "rsj-" + "a" * 24,
                                                               "consent": "c", "pin": "2468"})
        self.assertEqual((status, doc["error"]["code"]), (503, "hq_offline"))
        self.assertEqual(security.spent_today(self.store, self.now), 0)

    def test_audit_has_no_secrets(self):
        headers = self.paired()
        self.act(headers, "research/submit", {"request_id": "req-00009", "job_id": "rsj-" + "a" * 24,
                                              "consent": "paid-research:x", "pin": "2468"})
        text = (Path(self.tmp.name) / "runtime" / "mobile" / "audit.jsonl").read_text()
        self.assertNotIn("2468", text)
        self.assertNotIn("paid-research", text)
        self.assertIn("research/submit", text)

    def test_hq_reads_allowlisted(self):
        headers = self.paired()
        self.assertEqual(self.call("GET", "/m/api/hq/overview", headers=headers)[0], 200)
        self.assertEqual(self.call("GET", "/m/api/hq/export?workflow=x", headers=headers)[0], 404)
        self.assertEqual(self.call("GET", "/m/api/hq/overview")[0], 401)


class Client(unittest.TestCase):
    def test_client_speaks_like_the_hq_page_and_renews_session(self):
        sent = []
        sessions = iter(["s1", "s2"])

        def transport(method, path, headers, body, timeout):
            sent.append((method, path, headers))
            if path == "/api/studio/session":
                token = next(sessions)
                return 200, {"set-cookie": f"vk_studio={token}; Path=/api/studio/"}, json.dumps({"csrf": "c-" + token}).encode()
            if headers["Cookie"] == "vk_studio=s1":
                return 403, {}, json.dumps({"error": {"code": "studio_session_invalid"}}).encode()
            return 200, {}, b'{"ok": true}'

        client = HQClient(8765, transport=transport)
        status, doc = client.post("research/prepare", {"request_id": "req-00001", "topics": ["x"]})
        self.assertEqual((status, doc), (200, {"ok": True}))
        last = sent[-1][2]
        self.assertEqual(last["Origin"], "http://127.0.0.1:8765")
        self.assertEqual(last["Host"], "127.0.0.1:8765")
        self.assertEqual(last["X-VK-Studio-CSRF"], "c-s2")

    def test_refused_connection_is_offline(self):
        def transport(*_):
            raise ConnectionRefusedError()
        with self.assertRaises(NetworkError) as caught:
            HQClient(8765, transport=transport).get("/api/status")
        self.assertEqual(caught.exception.code, "hq_offline")

    def test_timeout_is_uncertain(self):
        def transport(*_):
            raise TimeoutError()
        with self.assertRaises(NetworkError) as caught:
            HQClient(8765, transport=transport).get("/api/status")
        self.assertEqual(caught.exception.code, "hq_no_answer")


class HQUntouched(unittest.TestCase):
    def test_mobile_package_does_not_change_hq_rules(self):
        from vicekrack.hq.api import _allowed_origin
        self.assertFalse(_allowed_origin({"host": REMOTE}, 8765))       # the HQ itself still refuses remote hosts
        self.assertTrue(_allowed_origin({"host": "127.0.0.1:8765"}, 8765))


if __name__ == "__main__":
    unittest.main()
