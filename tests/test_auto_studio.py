"""Step 54: Auto Studio (one approval -> finished, verified MP4).

Pure parts (planning, script rules, routes) always run. The end-to-end runs need FFmpeg and Pillow
(RUN_LOCAL_RENDER_TESTS=1) and build REAL MP4 files from synthetic clips and synthetic voices; they verify
the exported file itself (length, scene order, audio at the right times). No network: every socket is blocked.
"""

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack.autostudio import common, engine, planner, script as script_mod, voice
from vicekrack.errors import NetworkError
from vicekrack.hq import api
from vicekrack.hq.studio import Studio
from vicekrack.hq.studio_api import StudioGate
from vicekrack.research_demo import FixturePages, MockGrokResearch
from vicekrack.research_desk import ResearchDesk
from vicekrack.video_production_demo import MockGrok, demo_clock

CONFIG = common.load_config()
RENDER = os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1"
PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
BRIEF = {"topic": "GTA VI trailer", "angle": "x", "editorial": {"lane": "rumor", "rumor_label": "single_report"},
         "claims": [{"claim_id": "c1", "text": "Rockstar released a third GTA VI trailer.", "status": "unverified"},
                    {"claim_id": "c2", "text": "The trailer shows new characters in Leonida.", "status": "unverified"}],
         "sources": [{"publisher": "IGN", "title": "Trailer"}]}
SETTINGS = {"length": 30, "type": "news", "audio": "narrator", "music": True}


class PlanningTests(unittest.TestCase):
    def test_lengths_need_more_scenes_and_cost_more(self):
        previous = None
        for length in (15, 30, 60, 120):
            b = planner.budget(CONFIG, length)
            e = planner.estimate(CONFIG, length, "grok-imagine-video-1.5-lite", "720p")
            self.assertLessEqual(b["clip_seconds"], 15)                   # xAI's per-clip maximum
            self.assertGreaterEqual(e["cap_usd"], e["expected_usd"])
            if previous:
                self.assertGreater(b["scenes"], previous[0]["scenes"] - 1)
                self.assertGreater(e["expected_usd"], previous[1]["expected_usd"])
            previous = (b, e)
        self.assertEqual(planner.budget(CONFIG, 30)["scenes"], 3)
        full = planner.estimate(CONFIG, 30, "grok-imagine-video-1.5", "720p")
        self.assertGreater(full["expected_usd"], planner.estimate(CONFIG, 30, "grok-imagine-video-1.5-lite", "720p")["expected_usd"] * 3)

    def test_timeline_fits_clips_to_voices(self):
        tl = planner.timeline(CONFIG, 30, [[3.0, 2.5], [4.0], [3.5, 3.0]])
        self.assertAlmostEqual(tl["total_seconds"], 30, delta=1.0)        # short speech is padded to the length
        for seg in tl["segments"]:
            self.assertTrue(CONFIG["timing"]["min_clip_seconds"] <= seg["duration"] <= 15)
            for line in seg["lines"]:
                self.assertGreaterEqual(line["at"], seg["start"] + CONFIG["timing"]["lead_in_seconds"] - 1e-6)
                self.assertLessEqual(line["at"] + line["seconds"], seg["start"] + seg["duration"] + 1e-6)
        starts = [line["at"] for seg in tl["segments"] for line in seg["lines"]]
        self.assertEqual(starts, sorted(starts))
        long = planner.timeline(CONFIG, 30, [[7.0, 6.5, 5.0], [2.0]])  # one scene too long for a clip: split
        self.assertEqual([s["scene"] for s in long["segments"]], [0, 0, 1])
        with self.assertRaises(NetworkError):
            planner.timeline(CONFIG, 30, [[16.0]])
        self.assertFalse(planner.timeline(CONFIG, 15, [[12, 12], [12, 12]])["within_tolerance"])


class ScriptRuleTests(unittest.TestCase):
    def setUp(self):
        self.plan = planner.budget(CONFIG, 30)
        self.raw, _ = script_mod.mock_write(CONFIG, BRIEF, SETTINGS, self.plan)

    def test_mock_script_passes_and_voices_are_assigned(self):
        checked = script_mod.validate(self.raw, CONFIG, BRIEF, SETTINGS, self.plan)
        self.assertEqual(len(checked["scenes"]), 3)
        self.assertEqual(voice.assign_voices(CONFIG, checked["speakers"]), {"Narrator": "rex"})
        both = dict(SETTINGS, audio="dialogue")
        raw, _ = script_mod.mock_write(CONFIG, BRIEF, both, self.plan)
        voices = voice.assign_voices(CONFIG, script_mod.validate(raw, CONFIG, BRIEF, both, self.plan)["speakers"])
        self.assertEqual(len(set(voices.values())), 2)                   # two characters, two different voices

    def test_rules(self):
        cases = []
        bad = deepcopy(self.raw); bad["scenes"][0]["lines"][0]["claim_ids"] = ["c9"]; cases.append((bad, "unknown claim"))
        bad = deepcopy(self.raw); bad["scenes"][0]["lines"][0]["text"] = "Rockstar released a third trailer."
        cases.append((bad, "not labelled"))
        bad = deepcopy(self.raw); bad["scenes"].pop(); cases.append((bad, "scene count"))
        bad = deepcopy(self.raw); bad["speakers"].append({"name": "Maya", "kind": "woman"}); cases.append((bad, "speakers"))
        bad = deepcopy(self.raw); bad["scenes"][1]["lines"][0]["claim_ids"] = []; cases.append((bad, "at least one claim"))
        bad = deepcopy(self.raw); bad["scenes"][0]["lines"][0]["text"] = "Reportedly " + "word " * 200
        cases.append((bad, "usable"))
        for raw, reason in cases:
            with self.assertRaises(NetworkError) as raised:
                script_mod.validate(raw, CONFIG, BRIEF, SETTINGS, self.plan)
            self.assertEqual(raised.exception.code, "invalid_script")
            self.assertIn(reason, raised.exception.message)

    def test_writer_request_shape(self):
        sent = []

        def grok(body, *, timeout_seconds):
            sent.append(body)
            return {"status": "completed", "usage": {"input_tokens": 1000, "output_tokens": 800},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.raw)}]}]}
        raw, usage = script_mod.write(CONFIG, BRIEF, SETTINGS, self.plan, transport=grok)
        self.assertEqual(raw, self.raw)
        body = sent[0]
        self.assertEqual(body["model"], "grok-4.3")
        self.assertNotIn("tools", body)
        self.assertIn("NOT officially confirmed", body["input"][0]["content"])
        self.assertIn("Exactly 3 scenes", body["input"][0]["content"])


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = demo_clock()
        pages = FixturePages()
        self.desk = ResearchDesk(self.root, clock=self.clock, transport=MockGrokResearch(pages, self.clock),
                                 page_fetcher=pages, fetch_mode="fixture")
        job = self.desk.prepare(["GTA 6"])
        self.job = self.desk.submit(job["job_id"], job["consent_phrase"])
        self.card = next(c for c in self.job["cards"] if c["label"] == "MULTIPLE REPORTS")
        (self.root / "clips").mkdir()

    def providers(self, **kw):
        grok = MockGrok(self.root / "clips", fail_first_post_numbers=kw.pop("fail", ()),
                        pending_once=kw.pop("pending", ("demo-request-1",)))
        self.grok = grok
        return engine.Providers(writer="mock", tts=kw.pop("tts", voice.mock_transport()), video=grok, demo=True)

    def prepared(self, settings=SETTINGS):
        return engine.prepare(self.root, self.desk, self.job["job_id"], self.card["index"], settings, demo=True,
                              clock=self.clock)


class FlowTests(Base):
    def test_prepare_is_free_and_approval_is_exact(self):
        state = self.prepared()
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(state["consent_phrase"], "paid-auto:" + state["production_id"])
        self.assertTrue(state["rumor"])
        self.assertEqual(self.prepared()["production_id"], state["production_id"])     # same story + settings
        with self.assertRaises(NetworkError) as raised:
            engine.approve(self.root, state["production_id"], "paid-auto:asp-" + "0" * 24)
        self.assertEqual(raised.exception.code, "auto_consent_required")
        eng = engine.Engine(self.root, providers=self.providers(), clock=self.clock, sleep=lambda s: None)
        self.assertEqual(eng.active(), [])                                  # nothing runs without approval
        self.assertIsNone(eng.advance(state["production_id"]))
        self.assertEqual(self.grok.posts, 0)
        with self.assertRaises(NetworkError):
            engine.normalize_settings(CONFIG, dict(SETTINGS, length=45))

    def test_stop_and_cap_prevent_spending(self):
        state = self.prepared()
        engine.approve(self.root, state["production_id"], state["consent_phrase"], clock=self.clock)
        engine.stop(self.root, state["production_id"], clock=self.clock)
        eng = engine.Engine(self.root, providers=self.providers(), clock=self.clock, sleep=lambda s: None)
        self.assertIsNone(eng.advance(state["production_id"]))
        self.assertEqual(self.grok.posts, 0)
        # A cap too small for the clips: the script and voices run, no clip is sent, the reason is recorded.
        other = self.prepared(dict(SETTINGS, music=False))
        saved = common.read_state(other["production_id"], self.root)
        saved["estimate"]["cap_usd"] = 0.05
        common.save_state(saved, self.root)
        engine.approve(self.root, other["production_id"], other["consent_phrase"], clock=self.clock)
        result = eng.run_until_done(other["production_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"][-1]["code"], "spending_cap_reached")
        self.assertEqual(self.grok.posts, 0)
        self.assertLessEqual(result["cost"]["spent_usd"], 0.05)


@unittest.skipUnless(RENDER, "set RUN_LOCAL_RENDER_TESTS=1 (FFmpeg + Pillow)")
class EndToEndTests(Base):
    def test_thirty_second_video_is_made_checked_and_kept(self):
        state = self.prepared()
        pid = state["production_id"]
        engine.approve(self.root, pid, state["consent_phrase"], clock=self.clock)
        eng = engine.Engine(self.root, providers=self.providers(fail=(2,)), clock=self.clock, sleep=lambda s: None)
        done = eng.run_until_done(pid)
        self.assertEqual(done["status"], "completed", done["errors"])
        self.assertTrue(done["final"]["verified"])
        checks = {c["check"]: c for c in done["final"]["checks"]}
        self.assertTrue(all(c["passed"] for c in checks.values()))
        self.assertIn("matches its clip", checks["scene_order"]["detail"])
        final = common.folder(pid, self.root) / "final.mp4"
        info = common.probe(final)                               # the ACTUAL file, measured independently
        self.assertAlmostEqual(info["duration"], 30, delta=30 * CONFIG["timing"]["length_tolerance"])
        self.assertEqual((info["video"]["width"], info["video"]["height"]), (1080, 1920))
        self.assertEqual(info["audio"]["codec"], "aac")
        self.assertGreaterEqual(len(done["scenes"]), 3)
        self.assertEqual(self.grok.posts, len(done["scenes"]) + 1)          # one clip retried after a timeout
        self.assertLessEqual(done["cost"]["spent_usd"], done["estimate"]["cap_usd"])
        self.assertTrue(any("trying again automatically" in l["text"] for l in done["log"]))
        # A restart: a new Studio sees the same video, plays it in byte ranges and downloads it whole.
        gate = StudioGate(Studio(self.root, demo=True, clock=self.clock))
        status, _, body = api.respond("GET", "/api/studio/auto/videos", {"host": f"127.0.0.1:{PORT}"}, port=PORT,
                                      root=self.root, studio=gate)
        self.assertEqual(json.loads(body)["videos"][0]["production_id"], pid)
        url = f"/api/studio/auto/file?id={pid}&name=final"
        status, headers, part = api.respond("GET", url, {"host": f"127.0.0.1:{PORT}", "range": "bytes=0-99"}, port=PORT,
                                            root=self.root, studio=gate)
        self.assertEqual((status, len(part)), (206, 100))
        status, headers, whole = api.respond("GET", url + "&download=1", {"host": f"127.0.0.1:{PORT}"}, port=PORT,
                                             root=self.root, studio=gate)
        self.assertEqual(whole, final.read_bytes())
        self.assertIn("attachment", headers["Content-Disposition"])
        for bad in ("../state", "state", "final.mp4"):
            status, _, _ = api.respond("GET", f"/api/studio/auto/file?id={pid}&name={bad}", {"host": f"127.0.0.1:{PORT}"},
                                       port=PORT, root=self.root, studio=gate)
            self.assertEqual(status, 400)

    def test_dialogue_and_rewrite_when_the_voices_run_long(self):
        slow = voice.mock_transport()

        def tts(body, *, timeout_seconds, max_bytes):                   # a slow speaker: lines run twice as long
            body = dict(body, text=body["text"] + " " + body["text"])
            return slow(body, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
        settings = dict(SETTINGS, length=15, audio="dialogue", type="social")
        state = self.prepared(settings)
        engine.approve(self.root, state["production_id"], state["consent_phrase"], clock=self.clock)
        eng = engine.Engine(self.root, providers=self.providers(tts=tts), clock=self.clock, sleep=lambda s: None)
        done = eng.run_until_done(state["production_id"])
        self.assertGreaterEqual(done["rewrites"], 1)
        if done["status"] == "completed":
            self.assertTrue(done["final"]["verified"])
        else:                                                                 # never cut or sped up, no clips paid
            self.assertEqual(done["errors"][-1]["code"], "too_long")
            self.assertEqual(self.grok.posts, 0)


class RouteTests(Base):
    def post(self, gate, action, payload, cookie, csrf):
        headers = {"host": f"127.0.0.1:{PORT}", "origin": ORIGIN, "cookie": f"vk_studio={cookie}",
                   "x-vk-studio-csrf": csrf, "content-type": "application/json"}
        status, _, body = api.respond("POST", "/api/studio/" + action, headers, port=PORT, root=self.root, studio=gate,
                                      body=json.dumps(dict(payload, request_id="req-auto-" + os.urandom(6).hex())).encode())
        return status, json.loads(body)

    def test_prepare_approve_and_validation(self):
        studio = Studio(self.root, demo=True, clock=self.clock, auto_providers=self.providers())
        gate = StudioGate(studio)
        status, headers, body = api.respond("GET", "/api/studio/session", {"host": f"127.0.0.1:{PORT}"}, port=PORT,
                                            root=self.root, studio=gate)
        cookie, csrf = headers["Set-Cookie"].split(";")[0].split("=", 1)[1], json.loads(body)["csrf"]
        payload = {"job_id": self.job["job_id"], "story": self.card["index"], "length": 30, "type": "news",
                   "audio": "narrator", "music": True}
        for bad in (dict(payload, length=45), dict(payload, type="vlog"), dict(payload, audio=1), dict(payload, extra=1)):
            status, doc = self.post(gate, "auto/prepare", bad, cookie, csrf)
            self.assertEqual(status, 400, doc)
        status, doc = self.post(gate, "auto/prepare", payload, cookie, csrf)
        self.assertEqual((status, doc["status"]), (200, "prepared"))
        self.assertTrue(doc["consent_phrase"].startswith("paid-auto:asp-"))
        status, err = self.post(gate, "auto/approve", {"production_id": doc["production_id"],
                                                        "consent": "paid-auto:asp-" + "1" * 24}, cookie, csrf)
        self.assertEqual(err["error"]["code"], "auto_consent_required")
        status, ok = self.post(gate, "auto/approve", {"production_id": doc["production_id"],
                                                       "consent": doc["consent_phrase"]}, cookie, csrf)
        self.assertEqual(ok["status"], "approved")
        self.assertEqual(self.grok.posts, 0)                              # the worker (not the click) does the work
        status, _, body = api.respond("GET", "/api/studio/auto/options", {"host": f"127.0.0.1:{PORT}"}, port=PORT,
                                      root=self.root, studio=gate)
        options = json.loads(body)
        self.assertEqual(options["lengths"], [15, 30, 60, 120])
        self.assertEqual(options["model"], "grok-imagine-video-1.5-lite")

    def test_static_page_is_read_and_act_safe(self):
        source = (Path(api.STATIC) / "videos.js").read_text(encoding="utf-8")
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "XMLHttpRequest"):
            self.assertNotIn(forbidden, source)
        status, headers, body = api.respond("GET", "/static/videos.js", {"host": f"127.0.0.1:{PORT}"}, port=PORT)
        self.assertEqual(status, 200)
        self.assertIn('data-view="videos"', (Path(api.STATIC) / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
