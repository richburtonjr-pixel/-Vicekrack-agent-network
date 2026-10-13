"""Step 50: Research Desk (any topic -> Grok research -> own page checks -> labelled story cards -> Grok script).

Offline only: mock Grok (research and writer) and fixture pages; every socket connect fails the test.
"""

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.production import Pipeline, ProductionStore
from vicekrack.research_demo import FixturePages, MockGrokResearch, MockGrokWriter, _envelope
from vicekrack.research_desk import (
    RESEARCH_SCHEMA, ResearchDesk, _article_url, estimate, load_config, topic_keywords, writer_estimate,
)
from vicekrack.scout_cli import store_paths
from vicekrack.video_production_demo import demo_clock
from vicekrack.xai_responses import XAIScriptProvider, cost_usd, parse_structured, structured_body, usage_of

KEY = "synthetic-test-value-not-a-key"


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = demo_clock()
        self.pages = FixturePages()
        self.grok = MockGrokResearch(self.pages, self.clock)
        self.desk = ResearchDesk(self.root, clock=self.clock, transport=self.grok, page_fetcher=self.pages,
                                 fetch_mode="fixture")

    def researched(self, topics=("Jordan 4 release",)):
        job = self.desk.prepare(list(topics))
        return self.desk.submit(job["job_id"], job["consent_phrase"])

    def card(self, job, label):
        return next(c for c in job["cards"] if c["label"] == label)


class ConfigAndHelpers(unittest.TestCase):
    def test_config_and_estimates(self):
        config, _ = load_config()
        low, high = estimate(config, 1)
        self.assertLess(0, low)
        self.assertLess(low, high)
        self.assertLess(high, 0.5)                                 # one topic stays well under 50 cents
        self.assertLess(writer_estimate(config), 0.01)
        self.assertEqual(config["pipeline"]["creator"], "config/creator.xai.json")

    def test_topic_keywords(self):
        self.assertEqual(topic_keywords("GTA 6"), ["GTA 6"])
        self.assertEqual(topic_keywords("Jordan 4 release date"), ["Jordan 4 release date", "Jordan 4", "Jordan"])
        self.assertNotIn("new", [k.lower() for k in topic_keywords("Drake new album")])

    def test_article_urls(self):
        self.assertEqual(_article_url("https://www.ign.com/articles/x?utm_source=a#top"), "https://www.ign.com/articles/x")
        for bad in ("http://ign.com/a", "https://ign.com/", "https://ign.com/a?id=4", "https://127.0.0.1/a",
                    "https://user:pw@ign.com/a", "javascript:alert(1)", None, 5):
            self.assertIsNone(_article_url(bad), bad)


class PrepareTests(Base):
    def test_prepare_is_free_and_exact(self):
        job = self.desk.prepare(["  Jordan 4 release ", "jordan 4 release", "Philadelphia Eagles"])
        self.assertEqual(job["topics"], ["Jordan 4 release", "Philadelphia Eagles"])
        self.assertEqual((job["status"], self.grok.calls), ("prepared", []))
        self.assertEqual(job["request"]["tools"], [{"type": "web_search"}])
        self.assertEqual(job["request"]["text"]["format"]["schema"], RESEARCH_SCHEMA)
        self.assertEqual(json.loads(job["request"]["input"][1]["content"]), {"topics": job["topics"]})
        self.assertEqual(job["consent_phrase"], "paid-research:" + job["job_id"])
        self.assertLess(job["estimate_usd"]["low"], job["estimate_usd"]["high"])

    def test_bad_topics(self):
        for topics in ([], ["a"], ["x" * 81], ["ok", "two", "three", "four"], ["<script>"], "GTA 6", [5]):
            with self.assertRaises(NetworkError) as raised:
                self.desk.prepare(topics)
            self.assertEqual(raised.exception.code, "invalid_research_topics")

    def test_consent_must_match(self):
        job = self.desk.prepare(["GTA 6"])
        with self.assertRaises(NetworkError) as raised:
            self.desk.submit(job["job_id"], "paid-research:rsj-" + "0" * 24)
        self.assertEqual(raised.exception.code, "research_consent_required")
        self.assertEqual(self.grok.calls, [])


class ResearchTests(Base):
    def test_cards_are_labelled_by_viceKracks_own_checks(self):
        job = self.researched()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(self.grok.calls), 1)
        labels = [c["label"] for c in job["cards"]]
        self.assertEqual(labels, ["MULTIPLE REPORTS", "RUMOR", "NOT USABLE"])
        multi, rumor, unusable = job["cards"]
        self.assertTrue(multi["make_video"] and rumor["make_video"])
        self.assertIsNone(unusable["make_video"])
        self.assertIn("trusted outlet list", unusable["reason"])
        self.assertTrue(multi["make_video"]["rumor"])
        self.assertEqual(multi["make_video"]["consent"], "paid-script:" + multi["make_video"]["production_id"])
        self.assertEqual({s["check"] for c in job["cards"] for s in c["sources"]}, {"opened"})
        self.assertGreater(job["cost_usd"], 0)
        self.assertEqual(sorted(self.pages.calls), sorted(s["url"] for c in job["cards"] for s in c["sources"]))

    def test_evidence_comes_from_the_pages_not_from_grok(self):
        job = self.researched()
        folder, _ = store_paths(self.root)
        candidates = [read_json(p) for p in folder.glob("cand-*.json")]
        self.assertEqual(len(candidates), 4)
        for candidate in candidates:
            self.assertEqual(candidate["content_profile"], "open")
            self.assertNotIn("summary by Grok", json.dumps(candidate))
            self.assertNotIn("Two outlets report", json.dumps(candidate))     # Grok's summary text
        grok_summaries = {c["summary"] for c in job["cards"]}
        self.assertTrue(all("Demo data" in s for s in grok_summaries))

    def test_invented_url_and_off_topic_page(self):
        def lying(body, *, timeout_seconds):
            self.pages.add("https://apnews.com/article/real-page", "Weather today", ["Rain is expected all day long here."],
                           "2026-01-05T10:00:00Z")
            story = {"topic": "GTA 6", "headline": "GTA 6 news", "summary": "x",
                     "sources": [{"url": "https://www.ign.com/articles/made-up", "publisher": "IGN", "published": ""},
                                 {"url": "https://apnews.com/article/real-page", "publisher": "AP", "published": ""},
                                 {"url": "https://www.ign.com/articles/q?id=1", "publisher": "IGN", "published": ""}]}
            return _envelope({"stories": [story]}, 100, 100)
        self.desk.transport = lying
        job = self.researched(["GTA 6"])
        card = job["cards"][0]
        self.assertEqual([s["check"] for s in card["sources"]], ["article_unavailable", "page_does_not_mention_topic"])
        self.assertEqual((card["label"], card["make_video"]), ("NOT USABLE", None))
        self.assertEqual(list(store_paths(self.root)[0].glob("cand-*.json")), [])

    def test_failures_and_retry_rules(self):
        job = self.desk.prepare(["GTA 6"])

        def unknown(body, *, timeout_seconds):
            raise NetworkError("xai_transport_failed", "unknown")
        self.desk.transport = unknown
        with self.assertRaises(NetworkError):
            self.desk.submit(job["job_id"], job["consent_phrase"])
        self.assertEqual(self.desk.read(job["job_id"])["status"], "uncertain")
        with self.assertRaises(NetworkError) as raised:
            self.desk.submit(job["job_id"], job["consent_phrase"])            # never resent silently
        self.assertEqual(raised.exception.code, "research_uncertain")
        self.desk.transport = self.grok
        done = self.desk.submit(job["job_id"], job["consent_phrase"], retry_uncertain=True,
                                acknowledge_duplicate_billing=True)
        self.assertEqual(done["status"], "completed")
        with self.assertRaises(NetworkError) as raised:
            self.desk.submit(job["job_id"], job["consent_phrase"])
        self.assertEqual(raised.exception.code, "research_already_done")

        refused = self.desk.prepare(["GTA 6"])

        def reject(body, *, timeout_seconds):
            raise NetworkError("xai_rejected", "refused")
        self.desk.transport = reject
        with self.assertRaises(NetworkError):
            self.desk.submit(refused["job_id"], refused["consent_phrase"])
        self.assertEqual(self.desk.read(refused["job_id"])["status"], "rejected")
        self.desk.transport = self.grok                                        # refused = not billed: plain retry
        self.assertEqual(self.desk.submit(refused["job_id"], refused["consent_phrase"])["status"], "completed")

    def test_key_is_never_saved(self):
        with patch.dict(os.environ, {"XAI_API_KEY": KEY}):
            job = self.researched()
        for path in self.root.rglob("*.json"):
            self.assertNotIn(KEY, path.read_text(encoding="utf-8"))
        self.assertNotIn(KEY, json.dumps(job))

    def test_recover_rechecks_for_free(self):
        job = self.researched()
        again = self.desk.recover(job["job_id"])
        self.assertEqual(len(self.grok.calls), 1)
        self.assertEqual([c["label"] for c in again["cards"]], [c["label"] for c in job["cards"]])

    def test_jobs_list_and_bad_ids(self):
        job = self.researched()
        self.assertEqual(self.desk.list()[0]["job_id"], job["job_id"])
        for bad in ("rsj-1", "../x", None):
            with self.assertRaises(NetworkError):
                self.desk.read(bad)


class MakeVideoTests(Base):
    def setUp(self):
        super().setUp()
        self.writer = MockGrokWriter()
        self.pipeline = Pipeline(root=self.root, clock=self.clock,
                                 providers={"xai": XAIScriptProvider(transport_fn=self.writer)}, renderer=self.fake_render)
        self.job = self.researched()

    @staticmethod
    def fake_render(plan, *, allow_draft, directory, narration):
        folder = Path(directory) / "pkg"
        folder.mkdir(parents=True)
        (folder / "preview.mp4").write_bytes(b"mp4")
        (folder / "manifest.json").write_text(json.dumps({"plan_id": plan["plan_id"], "publishable": False,
                                                          "preview_only": True}))
        return {"preview_file": str(folder / "preview.mp4"), "manifest_file": str(folder / "manifest.json"),
                "preview_only": True, "publishable": False,
                "source_blocked_for_production": plan["blocked_for_production"], "audio_present": False}

    def test_make_video_with_the_grok_writer(self):
        card = self.card(self.job, "MULTIPLE REPORTS")
        result = self.desk.make_video(self.job["job_id"], card["index"], card["make_video"]["consent"],
                                      pipeline=self.pipeline)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.writer.calls), 1)
        body = self.writer.calls[0]
        self.assertEqual((body["model"], body["store"]), ("grok-4.3", False))
        self.assertNotIn("tools", body)
        state = ProductionStore(self.root).read(card["make_video"]["production_id"])
        self.assertEqual((state["config"]["creator"]["adapter"], state["config"]["creator"]["paid"]), ("xai", True))
        self.assertTrue(state["config"]["allow_draft_preview"])
        folder = ProductionStore(self.root).folder(state["production_id"])
        brief = read_json(folder / state["stages"][0]["artifacts"]["brief_path"])
        script = read_json(folder / state["stages"][1]["artifacts"]["script_path"])
        self.assertEqual((brief["editorial"]["lane"], brief["editorial"]["rumor_label"]), ("rumor", "multiple_reports"))
        self.assertEqual(script["provenance"]["provider"], "xai")
        self.assertTrue(script["disclosures"][0].startswith("MULTIPLE REPORTS - UNCONFIRMED"))
        again = self.desk.make_video(self.job["job_id"], card["index"], card["make_video"]["consent"],
                                     pipeline=self.pipeline)
        self.assertEqual(again["status"], "completed")
        self.assertEqual(len(self.writer.calls), 1)                          # never written (or paid) twice

    def test_refusals(self):
        unusable, rumor = self.card(self.job, "NOT USABLE"), self.card(self.job, "RUMOR")
        cases = ((unusable["index"], "paid-script:prod-" + "0" * 24, "story_not_makeable"),
                 (rumor["index"], "paid-script:prod-" + "0" * 24, "script_consent_required"),
                 (99, "x", "research_story_not_found"))
        for index, consent, code in cases:
            with self.assertRaises(NetworkError) as raised:
                self.desk.make_video(self.job["job_id"], index, consent, pipeline=self.pipeline)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.writer.calls, [])

    def test_refused_writer_is_not_uncertain(self):
        def reject(body, *, timeout_seconds):
            raise NetworkError("xai_rejected", "refused")
        pipeline = Pipeline(root=self.root, clock=self.clock, providers={"xai": XAIScriptProvider(transport_fn=reject)},
                            renderer=self.fake_render)
        card = self.card(self.job, "RUMOR")
        result = self.desk.make_video(self.job["job_id"], card["index"], card["make_video"]["consent"], pipeline=pipeline)
        self.assertEqual((result["status"], result["error"]["code"]), ("failed", "xai_rejected"))


class ResponsesParsingTests(unittest.TestCase):
    SCHEMA = {"type": "object", "additionalProperties": False, "required": ["a"], "properties": {"a": {"type": "string"}}}

    def test_parse_and_usage(self):
        result, usage = parse_structured(_envelope({"a": "x"}, 1000, 200, 3), self.SCHEMA)
        self.assertEqual((result, usage), ({"a": "x"}, {"input_tokens": 1000, "output_tokens": 200, "search_calls": 3}))
        prices = {"input_per_million": 1.0, "output_per_million": 2.0, "web_search_per_thousand": 5.0}
        self.assertAlmostEqual(cost_usd(usage, prices), 0.0164)
        self.assertEqual(usage_of({}), {"input_tokens": 0, "output_tokens": 0, "search_calls": 0})

    def test_bad_answers(self):
        refusal = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]}
        for response, code in ((dict(_envelope({"a": "x"}, 1, 1), status="incomplete"), "xai_incomplete"),
                               (refusal, "xai_refusal"), (_envelope({"b": 1}, 1, 1), "xai_invalid_response"),
                               ({"status": "failed"}, "xai_invalid_response")):
            with self.assertRaises(NetworkError) as raised:
                parse_structured(response, self.SCHEMA)
            self.assertEqual(raised.exception.code, code)

    def test_body_has_no_key(self):
        with patch.dict(os.environ, {"XAI_API_KEY": KEY}):
            body = structured_body(model="m", instructions="i", payload={"x": 1}, schema=self.SCHEMA, schema_name="s",
                                   max_output_tokens=10, tools=[{"type": "web_search"}])
        self.assertNotIn(KEY, json.dumps(body))

    def test_missing_key_sends_nothing(self):
        from vicekrack import xai_responses
        with patch.dict(os.environ, {"XAI_API_KEY": ""}), patch("vicekrack.xai_responses.build_opener") as opener:
            with self.assertRaises(NetworkError) as raised:
                xai_responses.transport({}, timeout_seconds=5)
        self.assertEqual(raised.exception.code, "missing_xai_credential")
        opener.assert_not_called()


class StudioResearchTests(Base):
    """The Research routes on the Studio gate (same session, CSRF, one-action and request-ID rules)."""

    def setUp(self):
        super().setUp()
        from vicekrack.hq import api
        from vicekrack.hq.studio import Studio
        from vicekrack.hq.studio_api import StudioGate
        self.api = api
        self.studio = Studio(self.root, demo=True, clock=self.clock, research_transport=self.grok,
                             page_fetcher=self.pages, writer_transport=MockGrokWriter(),
                             renderer=MakeVideoTests.fake_render)
        self.gate = StudioGate(self.studio)
        status, headers, body = self.call("GET", "/api/studio/session")
        self.cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.csrf = json.loads(body)["csrf"]
        self.assertIn("research", json.loads(body))
        self.n = 0

    def call(self, method, target, headers=None, body=b""):
        base = {"host": "127.0.0.1:8765"}
        base.update(headers or {})
        return self.api.respond(method, target, base, port=8765, root=self.root, studio=self.gate, body=body)

    def post(self, action, payload):
        self.n += 1
        headers = {"origin": "http://127.0.0.1:8765", "cookie": f"vk_studio={self.cookie}", "x-vk-studio-csrf": self.csrf,
                   "content-type": "application/json", "sec-fetch-site": "same-origin"}
        status, _, body = self.call("POST", "/api/studio/" + action, headers,
                                    json.dumps(dict(payload, request_id=f"req-research-{self.n:04d}")).encode())
        return status, json.loads(body)

    def test_full_flow_through_the_routes(self):
        status, job = self.post("research/prepare", {"topics": ["Jordan 4 release"]})
        self.assertEqual(status, 200, job)
        self.assertEqual(job["status"], "prepared")
        self.assertNotIn("request", job)                                   # the full body stays server-side
        status, job = self.post("research/submit", {"job_id": job["job_id"], "consent": job["consent_phrase"]})
        self.assertEqual((status, job["status"]), (200, "completed"))
        status, _, body = self.call("GET", "/api/studio/research/jobs")
        self.assertEqual(json.loads(body)["jobs"][0]["job_id"], job["job_id"])
        card = next(c for c in job["cards"] if c["make_video"])
        status, done = self.post("research/make-video", {"job_id": job["job_id"], "story": card["index"],
                                                         "consent": card["make_video"]["consent"]})
        self.assertEqual((status, done["made"]["status"]), (200, "completed"), done)
        status, _, body = self.call("GET", "/api/studio/productions")
        self.assertIn(card["make_video"]["production_id"], [p["production_id"] for p in json.loads(body)["productions"]])

    def test_route_validation(self):
        for payload in ({"topics": "GTA 6"}, {"topics": []}, {"topics": ["<b>"]}, {"topics": ["ok"], "extra": 1}):
            status, doc = self.post("research/prepare", payload)
            self.assertEqual(status, 400, payload)
        status, doc = self.post("research/submit", {"job_id": "rsj-" + "0" * 24, "consent": "paid-speech:sp-" + "0" * 24})
        self.assertEqual(status, 400)
        status, doc = self.post("research/make-video", {"job_id": "rsj-" + "0" * 24, "story": -1,
                                                        "consent": "paid-script:prod-" + "0" * 24})
        self.assertEqual(status, 400)

    def test_static_research_page_is_safe(self):
        text = (ROOT / "vicekrack/hq/static/research.js").read_text(encoding="utf-8")
        for forbidden in ("innerHTML", "localStorage", "sessionStorage", "setInterval", "setTimeout", "eval(",
                          "XAI_API_KEY"):
            self.assertNotIn(forbidden, text)
        status, _, body = self.call("GET", "/static/research.js")
        self.assertEqual((status, body.decode("utf-8")), (200, text))


if __name__ == "__main__":
    unittest.main()
