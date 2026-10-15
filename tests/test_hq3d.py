"""HQ 2.0, Phase 1: the 3D headquarters (vicekrack/hq/static/hq3d/) and its real-data overview.

Offline: no network; the browser test is opt-in like the other browser tests.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from vicekrack.hq import api
from vicekrack.hq.studio import Studio
from vicekrack.hq.studio_api import StudioGate
from vicekrack.research_demo import FixturePages, MockGrokResearch
from vicekrack.research_desk import ResearchDesk
from vicekrack.video_production_demo import demo_clock

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "vicekrack/hq/static"
PORT = 8765
THREE_SHA256 = "f7cee3c7533449a1505cc12cb5128b89e3d4fd3d7ea62b05f9f5464a217472ee"   # three@0.169.0 build/three.module.min.js
HQ3D_FILES = ("/static/hq3d/main.js", "/static/hq3d/world.js", "/static/hq3d/layout.js", "/static/hq3d/agents.js",
              "/static/vendor/three.module.min.js")


def get(target, studio=None, root=None):
    status, headers, body = api.respond("GET", target, {"host": f"127.0.0.1:{PORT}"}, port=PORT, root=root, studio=studio)
    return status, headers, body


class StaticFilesTests(unittest.TestCase):
    def test_modules_are_served_from_the_allowlist_as_javascript(self):
        for path in HQ3D_FILES:
            status, headers, body = get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith("text/javascript"), path)
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])      # no CDN, no inline code
            self.assertEqual(body, (STATIC / path[len("/static/"):]).read_bytes())
        for path in ("/static/hq3d/", "/static/hq3d/../app.js", "/static/vendor/THREE-LICENSE.txt",
                     "/static/hq3d/main.js?x=1"):
            self.assertEqual(get(path)[0], 404, path)

    def test_three_js_is_the_pinned_mit_build(self):
        data = (STATIC / "vendor/three.module.min.js").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), THREE_SHA256)
        self.assertIn("MIT", (STATIC / "vendor/THREE-LICENSE.txt").read_text(encoding="utf-8"))

    def test_page_offers_the_headquarters_and_keeps_every_screen(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="hq3d"', html)
        self.assertIn('<script type="module" src="/static/hq3d/main.js"></script>', html)
        for view in ("house", "trading", "results", "content", "timeline", "sessions", "broker", "research", "studio"):
            self.assertIn(f'data-view="{view}"', html)

    def test_the_headquarters_only_reads(self):
        """No paid or state-changing request can start from the 3D HQ: it only GETs the session and overview."""
        for name in ("main.js", "world.js", "layout.js", "agents.js"):
            source = (STATIC / "hq3d" / name).read_text(encoding="utf-8")
            self.assertNotIn("POST", source, name)
            self.assertNotIn("method:", source, name)
            self.assertNotIn("Math.random", source, name)                 # no invented activity or numbers
            self.assertNotIn("XAI_API_KEY", source, name)
            for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                              "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "postMessage"):
                self.assertNotIn(forbidden, source, f"{name}: {forbidden}")
            fetches = re.findall(r"fetch\(\"([^\"]+)\"", source)
            self.assertLessEqual(set(fetches), {"/api/studio/session", "/api/studio/overview"}, name)
            for imported in re.findall(r'from "([^"]+)"', source):
                self.assertTrue(imported.startswith("/static/"), imported)  # only local, allowlisted modules

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_walking_rules_in_node(self):
        result = subprocess.run(["node", str(ROOT / "tests/hq3d_layout_test.mjs"), str(ROOT)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("hq3d layout ok", result.stdout)


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = demo_clock()

    def overview(self, studio):
        status, _, body = get("/api/studio/overview", studio=StudioGate(studio), root=self.root)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_empty_records_are_shown_as_empty_never_invented(self):
        doc = self.overview(Studio(self.root, clock=self.clock))
        self.assertEqual((doc["contract"], doc["demo"]), ("hq_overview", False))
        self.assertEqual(doc["totals"], {"research_runs": 0, "productions": 0, "workflows": 0, "waiting_for_review": 0})
        self.assertEqual(doc["activity"], [])
        states = {name: d["state"] for name, d in doc["departments"].items()}
        self.assertEqual(states, {"executive": "empty", "research": "empty", "trends": "coming_soon",
                                  "strategy": "empty", "risk": "empty", "studio": "empty",
                                  "analytics": "coming_soon", "operations": "empty"})
        for d in doc["departments"].values():
            self.assertTrue(all(f["value"] == 0 for f in d["facts"]))
        self.assertEqual({d["agent"] for d in doc["departments"].values()},
                         {"ricky", "kiara", "jax", "layla", "ace", "sienna", "kai", "nia"})

    def test_saved_research_is_counted_from_the_records(self):
        pages = FixturePages()
        desk = ResearchDesk(self.root, clock=self.clock, transport=MockGrokResearch(pages, self.clock),
                            page_fetcher=pages, fetch_mode="fixture")
        job = desk.prepare(["Jordan 4 release"])
        desk.submit(job["job_id"], job["consent_phrase"])
        doc = self.overview(Studio(self.root, clock=self.clock))
        research = doc["departments"]["research"]
        self.assertEqual(research["state"], "idle")
        self.assertEqual({f["label"]: f["value"] for f in research["facts"]},
                         {"Research runs": 1, "Running now": 0, "Stories found": 3})
        self.assertEqual(research["latest"]["text"], "Jordan 4 release")
        strategy = {f["label"]: f["value"] for f in doc["departments"]["strategy"]["facts"]}
        self.assertEqual(strategy["Ready to make (confirmed / reports / rumor)"], 2)
        self.assertEqual(strategy["Your call"], 1)
        self.assertEqual(doc["activity"][0]["agent"], "kiara")
        self.assertEqual(doc["totals"]["research_runs"], 1)

    def test_demo_is_labelled_and_the_route_needs_the_studio(self):
        self.assertTrue(self.overview(Studio(self.root, demo=True, clock=self.clock))["demo"])
        status, _, body = get("/api/studio/overview", studio=None, root=self.root)
        self.assertEqual((status, json.loads(body)["error"]["code"]), (403, "studio_disabled"))
        status, _, _ = get("/api/studio/overview?x=1", studio=StudioGate(Studio(self.root)), root=self.root)
        self.assertEqual(status, 404)


@unittest.skipUnless(os.environ.get("RUN_LOCAL_BROWSER_TESTS") == "1" and os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1",
                     "set RUN_LOCAL_BROWSER_TESTS=1 and RUN_LOCAL_RENDER_TESTS=1 (Playwright + Chromium)")
class HeadquartersBrowserTests(unittest.TestCase):
    def test_walk_the_headquarters_with_real_empty_data(self):
        from playwright.sync_api import sync_playwright
        from vicekrack.hq.server import HQServer
        with tempfile.TemporaryDirectory() as temp, ExitStack():
            server = HQServer(0, Path(temp), Studio(Path(temp)))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
                    page = browser.new_page(viewport={"width": 1366, "height": 800})
                    problems, posts = [], []
                    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
                    page.on("pageerror", lambda e: problems.append(str(e)))
                    page.on("request", lambda r: posts.append(r.url) if r.method != "GET" else None)
                    page.goto(url)                                           # the Studio is on: 3D HQ first
                    page.wait_for_selector("#hq3d-view:not([hidden]) canvas", timeout=30000)
                    page.wait_for_selector("#hq3d-badge.real", timeout=30000)
                    self.assertEqual(page.inner_text("#hq3d-badge"), "Real data")
                    self.assertEqual(page.locator(".hq3d-dept").count(), 9)
                    start = page.evaluate("({...window.HQ3D.state.player})")
                    page.locator("#hq3d-canvas canvas").focus()
                    page.keyboard.down("w"); page.wait_for_timeout(4000); page.keyboard.up("w")
                    moved = page.evaluate("({...window.HQ3D.state.player})")
                    self.assertGreater(moved["z"], start["z"] + 0.5)          # walked forward (software GPU: slow)
                    page.click(".hq3d-dept[data-department=research]")
                    panel = page.inner_text("#hq3d-panel")
                    self.assertIn("Kiara", panel)
                    self.assertIn("Character pending", panel)
                    self.assertIn("Nothing yet", panel)                      # honest empty state
                    self.assertIn("From your saved ViceKrack records", panel)
                    page.click(".hq3d-dept[data-department=trends]")
                    self.assertIn("Coming soon", page.inner_text("#hq3d-panel"))
                    page.click(".hq3d-dept[data-department=studio]")
                    page.click(".hq3d-go")                                   # the existing screen still opens
                    page.wait_for_selector("#studio-view:not([hidden])")
                    self.assertTrue(page.evaluate("document.getElementById('hq3d-view').hidden"))
                    self.assertFalse(page.evaluate("window.HQ3D.state.active"))  # rendering paused off-screen
                    page.click(".view-btn[data-view='hq3d']")
                    page.wait_for_selector("#hq3d-view:not([hidden])")
                    page.click("#hq3d-quality")
                    self.assertEqual(page.inner_text("#hq3d-quality"), "Graphics: low")
                    self.assertEqual(posts, [])                              # nothing acted, nothing paid
                    self.assertEqual(problems, [])
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
