"""Step 32: ViceKrack Living HQ. Local synthetic data only; no network beyond 127.0.0.1, no credits.

Expected frame states are folded here independently with the Step 31 transition function;
expected handoffs and room mappings are written out by hand.
"""

import _socket
import io
import json
import os
import re
import shutil
import subprocess
import threading
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import patch

from test_events import EventBase, tree
from test_production import NOW as CONTENT_NOW, Base as ProductionBase, FakeRenderer
from vicekrack.events.cli import load_timeline
from vicekrack.events.contract import apply
from vicekrack.events.store import EventStore
from vicekrack.hq import api
from vicekrack.hq.demo import demo_events, demo_scene
from vicekrack.hq.layout import COMPONENT_ROOM, ROOMS
from vicekrack.hq.scene import assemble, scene_from_view
from vicekrack.hq.server import HQServer, main as hq_main
from vicekrack.production import Pipeline
from vicekrack.trading.agents.cli import main as agent_main
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.simulation.cli import main as sim_main
from vicekrack.trading.timeline import AGENT_COMPONENTS, from_agent_run

STATIC = ROOT / "vicekrack/hq/static"
PORT = 8765
HOST = {"host": f"127.0.0.1:{PORT}"}
EXPECTED_MAP = {                                     # Step 33: rooms show only their actual roles
    "trading.research.market_scout": "market_scout", "trading.research.trend_agent": "trend_agent",
    "trading.research.strategy_agent": "strategy_agent", "trading.research.risk_review": "risk_review",
    "content.workflow.researcher": "researcher", "content.workflow.analyst": "analyst",
    "content.workflow.reviewer": "reviewer", "content.production.creator": "creator",
    "trading.research.controller": "operations", "trading.simulation.engine": "operations",
    "content.workflow.orchestrator": "operations", "content.production.pipeline": "operations",
    "content.production.brief": "operations", "content.production.validate": "operations",
    "content.production.plan": "operations", "content.production.preview": "operations",
    "content.production.quality": "operations"}
UPSTAIRS = ["market_scout", "trend_agent", "strategy_agent", "risk_review"]
DOWNSTAIRS = ["researcher", "analyst", "reviewer", "creator"]


def get(target, headers=None, root=None, method="GET"):
    status, response_headers, body = api.respond(method, target, dict(HOST, **(headers or {})), port=PORT, root=root)
    return status, response_headers, body


def fold_rooms(events, upto):
    """Independent fold: component states after events[:upto + 1], then the room each maps to."""
    states = {component: "idle" for component in EXPECTED_MAP}
    for event in events[:upto + 1]:
        apply(states, event)
    return states


class MappingTests(unittest.TestCase):
    def test_room_mapping_and_floors(self):
        self.assertEqual(COMPONENT_ROOM, EXPECTED_MAP)
        floors = {room["room"]: (room["floor"], room["department"]) for room in ROOMS}
        self.assertEqual({r: floors[r] for r in UPSTAIRS}, {r: ("upper", "trading") for r in UPSTAIRS})
        self.assertEqual({r: floors[r] for r in DOWNSTAIRS}, {r: ("ground", "content") for r in DOWNSTAIRS})
        self.assertEqual({floors[r] for r in ("operations", "lounge", "kitchen")}, {("shared", "shared")})
        self.assertEqual([r["components"] for r in ROOMS if r["room"] in ("lounge", "kitchen")], [[], []])
        for component in AGENT_COMPONENTS:
            self.assertIn(component, COMPONENT_ROOM)          # every recorded trading component has a place


class DemoTests(unittest.TestCase):
    def test_demo_is_deterministic_labelled_and_complete(self):
        first, second = demo_scene(), demo_scene()
        self.assertEqual(first, second)
        self.assertEqual((first["mode"], first["timeline"]["origin"], first["timeline"]["time_basis"], first["current"]),
                         ("demo", "demo", "synthetic", None))
        shown = {state for frame in first["frames"] for state in frame["room_states"].values()}
        self.assertEqual(shown, {"idle", "working", "waiting", "blocked", "completed", "failed"})
        self.assertTrue(all(room["has_activity"] for room in first["rooms"] if room["components"]))
        self.assertTrue(all(e["run_id"] is None for e in demo_events()))     # demo never points at real runs

    def test_demo_follows_real_workflow_order(self):
        orders = (["trading.research.market_scout", "trading.research.trend_agent", "trading.research.strategy_agent",
                   "trading.research.risk_review"],
                  ["content.workflow.researcher", "content.workflow.analyst", "content.workflow.reviewer"],
                  ["content.production.brief", "content.production.creator", "content.production.validate",
                   "content.production.plan", "content.production.preview"])
        events = demo_events()
        for order in orders:
            first_seen = [next(i for i, e in enumerate(events) if e["component"] == c) for c in order]
            self.assertEqual(first_seen, sorted(first_seen))

    def test_demo_writes_nothing(self):
        before = tree(ROOT / "runtime") if (ROOT / "runtime").exists() else {}
        demo_scene()
        get("/api/scene?timeline=demo")
        self.assertEqual(tree(ROOT / "runtime") if (ROOT / "runtime").exists() else {}, before)


class SceneTests(EventBase):
    def recorded_agent_view(self, handlers=None):
        recorder = self.recorder(kind="research_agent_workflow", components=AGENT_COMPONENTS)
        run = self.agents(recorder, handlers)
        recorder.close(run["status"])
        return run, EventStore(self.root).load(recorder.timeline_id)

    def test_frames_follow_recorded_order_and_fold(self):
        _, view = self.recorded_agent_view()
        scene = scene_from_view(view)
        self.assertEqual(scene["mode"], "recorded_replay")
        self.assertEqual([f["event"]["sequence"] for f in scene["frames"]], [e["sequence"] for e in view["events"]])
        for index, frame in enumerate(scene["frames"]):
            states = fold_rooms(view["events"], index)
            self.assertEqual(frame["room"], EXPECTED_MAP[frame["event"]["component"]])
            for room in UPSTAIRS:
                expected = states["trading.research." + room]
                self.assertEqual(frame["room_states"][room] in (expected, "waiting"), True, (index, room))
                if frame["room_states"][room] == "waiting":
                    self.assertEqual(expected, "idle")
        self.assertEqual({r["room"] for r in scene["rooms"] if r["has_activity"]}, set(UPSTAIRS) | {"operations"})

    def test_waiting_and_handoffs_only_where_recorded(self):
        _, view = self.recorded_agent_view()
        scene = scene_from_view(view)
        first = scene["frames"][0]                                   # controller started
        self.assertEqual([first["room_states"][r] for r in UPSTAIRS], ["waiting"] * 4)
        self.assertEqual([first["room_states"][r] for r in DOWNSTAIRS], ["idle"] * 4)
        handoffs = [(f["handoff"]["from"], f["handoff"]["to"]) for f in scene["frames"] if f["handoff"]]
        self.assertEqual(handoffs, [("operations", "market_scout"), ("market_scout", "trend_agent"),
                                    ("trend_agent", "strategy_agent"), ("strategy_agent", "risk_review"),
                                    ("risk_review", "operations")])
        last = scene["frames"][-1]["room_states"]
        self.assertEqual([last[r] for r in UPSTAIRS], ["completed"] * 4)

    def test_failed_stage_blocks_without_invented_handoffs(self):
        from vicekrack.trading.agents.handlers import default_handlers
        handlers = default_handlers()
        handlers[1].analyze = lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
        _, view = self.recorded_agent_view(handlers)
        scene = scene_from_view(view)
        last = scene["frames"][-1]["room_states"]
        self.assertEqual([last[r] for r in UPSTAIRS], ["completed", "failed", "blocked", "blocked"])
        handoffs = [(f["handoff"]["from"], f["handoff"]["to"]) for f in scene["frames"] if f["handoff"]]
        self.assertEqual(handoffs, [("operations", "market_scout"), ("market_scout", "trend_agent")])
        self.assertEqual(view["outcome"], "failed")

    def test_simulation_only_timeline_leaves_rooms_empty(self):
        dataset = self.dataset()
        code, output = self.cli_json(sim_main, "sim-run", dataset["dataset_id"], "--save", "--record-events")
        self.assertEqual(code, 0)
        for identifier in (output["events"]["timeline_id"], output["run_id"]):
            scene = scene_from_view(load_timeline(identifier, self.root))
            active = {r["room"] for r in scene["rooms"] if r["has_activity"]}
            self.assertEqual(active, {"operations"})                 # no bot is shown working on a simulation
            self.assertTrue(all(f["room"] == "operations" and f["bot"] is None for f in scene["frames"]))
        self.assertEqual(scene_from_view(load_timeline(output["run_id"], self.root))["mode"], "reconstructed")

    def test_partial_timeline_is_shown_honestly(self):
        _, view = self.recorded_agent_view()
        folder = EventStore(self.root).folder("trading", view["timeline_id"])
        (folder / "events" / "000004.json").unlink()
        partial = EventStore(self.root).load(view["timeline_id"])
        scene = scene_from_view(partial)
        self.assertEqual(scene["timeline"]["completeness"], "partial")
        self.assertIn("missing_events", scene["issues"])
        self.assertEqual(len(scene["frames"]), 3)
        now = scene["current"]["room_states"]
        self.assertNotIn("working", now.values())
        self.assertEqual(now["market_scout"], "completed")           # recorded terminal state stays
        self.assertEqual((now["operations"], now["trend_agent"]), ("unknown", "unknown"))   # never "working"/"idle"

    def test_invalid_transitions_stop_frames(self):
        events = deepcopy(demo_events()[:3])
        events.append(dict(events[2], sequence=4, event_type="stage_completed", status="completed",
                           component="trading.research.risk_review", stage="risk_review"))
        timeline = demo_scene()["timeline"]
        scene = assemble(mode="demo", timeline=dict(timeline, event_count=4), components=sorted(EXPECTED_MAP), events=events)
        self.assertEqual(len(scene["frames"]), 3)
        self.assertIn("frames_stop_at_invalid_transition", scene["issues"])

    def test_live_then_interrupted(self):
        recorder = self.recorder(kind="research_agent_workflow", components=AGENT_COMPONENTS)
        recorder.emit("trading.research.controller", "workflow", "stage_started", "started")
        recorder.emit("trading.research.market_scout", "market_scout", "stage_started", "started", details={"position": 1})
        scene = scene_from_view(EventStore(self.root).load(recorder.timeline_id))
        self.assertEqual((scene["mode"], scene["timeline"]["live"]), ("observed", True))
        self.assertEqual(scene["current"]["room_states"]["market_scout"], "working")
        self.assertEqual(scene["current"]["room_states"]["trend_agent"], "waiting")
        recorder.writer.abandon()
        scene = scene_from_view(EventStore(self.root).load(recorder.timeline_id))
        self.assertEqual((scene["mode"], scene["timeline"]["completeness"]), ("recorded_replay", "interrupted"))
        self.assertEqual(scene["current"]["room_states"]["market_scout"], "unknown")
        self.assertEqual(scene["current"]["room_states"]["trend_agent"], "idle")          # no live workflow: no waiting
        self.assertEqual(scene["current"]["notes"]["trading.research.market_scout"], "not_live_last_known_working")

    def test_reconstructed_scene_is_deterministic(self):
        run = self.agents()
        self.assertEqual(scene_from_view(from_agent_run(run)), scene_from_view(from_agent_run(deepcopy(run))))


class ContentSceneTests(EventBase):
    def test_content_rooms_from_saved_production(self):
        case = _Content("run_content")
        case.setUp()
        try:
            case.run_content(self)
        finally:
            case.doCleanups()


class _Content(ProductionBase):
    def run_content(self, outer):
        result = self.produce(self.pipeline(FakeRenderer([KeyboardInterrupt()])) if False else self.pipeline())
        production_id = result["production_id"]
        scene = scene_from_view(load_timeline(production_id, self.root))
        outer.assertEqual(scene["mode"], "reconstructed")
        # Step 33: an (older) production lights only the Creator room and the labelled stations.
        outer.assertEqual({r["room"] for r in scene["rooms"] if r["has_activity"]}, {"creator", "operations"})
        outer.assertEqual({s["station"] for s in scene["stations"] if s["has_activity"]},
                          {"brief_builder", "script_validator", "scene_planner", "preview_renderer"})
        handoffs = [(f["handoff"]["from_station"], f["handoff"]["to"], f["handoff"]["to_station"])
                    for f in scene["frames"] if f["handoff"]]
        outer.assertEqual(handoffs, [("brief_builder", "creator", None), (None, "operations", "script_validator")])
        outer.assertTrue(all(f["event"]["recorded_at"] == CONTENT_NOW for f in scene["frames"]))
        outer.assertEqual(scene["current"]["room_states"]["creator"], "completed")
        outer.assertEqual([scene["current"]["room_states"][r] for r in ("researcher", "analyst", "reviewer")], ["idle"] * 3)
        # Upstairs stays empty for a content timeline.
        outer.assertFalse(any(r["has_activity"] for r in scene["rooms"] if r["room"] in UPSTAIRS))
        Pipeline(root=self.root, clock=lambda: CONTENT_NOW, renderer=FakeRenderer())  # constructing it runs nothing


class ApiTests(EventBase):
    def test_methods_hosts_and_origins(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            self.assertEqual(get("/api/scene?timeline=demo", method=method)[0], 405, method)
        for headers in ({"host": "evil.example"}, {"host": "127.0.0.1:9999"}, {"origin": "http://evil.example"},
                        {"origin": "null"}, {"sec-fetch-site": "cross-site"}, {"host": ""}):
            self.assertEqual(get("/api/timelines", headers)[0], 403, headers)
        self.assertEqual(get("/", {"host": f"localhost:{PORT}", "origin": f"http://localhost:{PORT}"})[0], 200)
        self.assertEqual(get("/api/status", {"sec-fetch-site": "same-origin"})[0], 200)

    def test_paths_and_ids_are_allowlisted(self):
        for target in ("/static/../vicekrack/hq/api.py", "/static/%2e%2e/api.py", "/../etc/passwd", "/static/app.js/..",
                       "/static/", "/static/app.js?x=1", "/vicekrack/hq/static/app.js", "/static\\app.js",
                       "/static/app.js%00", "/" + "a" * 400, "/api/scenes"):
            status, _, body = get(target)
            self.assertEqual(status, 404, target)
            self.assertNotIn(b"Traceback", body)
        for value in ("../../x", "tl-" + "a" * 23, "TL-" + "a" * 24, "demo&timeline=demo", "", "%2e%2e"):
            self.assertEqual(get("/api/scene?timeline=" + value)[0], 400, value)
        self.assertEqual(get("/api/scene?timeline=demo&x=1")[0], 400)
        status, _, body = get("/api/scene?timeline=tl-" + "a" * 24, root=self.root)
        self.assertEqual((status, json.loads(body)["error"]["code"]), (404, "event_timeline_not_found"))

    def test_responses_are_safe(self):
        for target in ("/", "/static/app.js", "/static/hq-core.js", "/static/styles.css", "/api/timelines",
                       "/api/scene?timeline=demo", "/nope"):
            status, headers, body = get(target, root=self.root)
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertNotIn(str(self.root).encode(), body)
        with patch("vicekrack.hq.api.scene", side_effect=RuntimeError("secret /home/user sk-" + "a" * 30)):
            status, _, body = get("/api/scene?timeline=demo")
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)
        self.assertNotIn(b"/home", body)

    def test_tampered_saved_run_is_rejected_without_details(self):
        _, output = self.cli_json(sim_main, "sim-run", self.dataset()["dataset_id"], "--save")
        path = self.root / "runtime/trading/simulation/runs" / f"{output['run_id']}.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"ending_cash": "', '"ending_cash": "9', 1), encoding="utf-8")
        status, _, body = get("/api/scene?timeline=" + output["run_id"], root=self.root)
        self.assertEqual((status, json.loads(body)["error"]["code"]), (409, "sim_run_corrupt"))
        self.assertNotIn(str(self.root).encode(), body)

    def test_api_is_read_only(self):
        dataset = self.dataset()
        _, sim = self.cli_json(sim_main, "sim-run", dataset["dataset_id"], "--save", "--record-events")
        _, agents = self.cli_json(agent_main, "agent-run", self.agents()["dataset"]["dataset_id"], "--save", "--record-events")
        before = tree(self.root)
        listing = json.loads(get("/api/timelines", root=self.root)[2])
        ids = {item["id"] for item in listing["items"]}
        self.assertTrue({"demo", sim["events"]["timeline_id"], sim["run_id"], agents["events"]["timeline_id"],
                         agents["run_id"]} <= ids)
        with patch("vicekrack.trading.simulation.engine.run_simulation", side_effect=AssertionError("ran")), \
                patch("vicekrack.trading.agents.controller.run_workflow", side_effect=AssertionError("ran")):
            for identifier in ids:
                self.assertEqual(get("/api/scene?timeline=" + identifier, root=self.root)[0], 200, identifier)
        self.assertEqual(tree(self.root), before)


class LoopbackServerTests(EventBase):
    """Real HTTP through the server on 127.0.0.1 (the test harness blocks socket.connect; this uses the
    C-level socket only for loopback, asserting the address first)."""

    def raw(self, port, request):
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(5)
        address = ("127.0.0.1", port)
        assert address[0] == "127.0.0.1"
        _socket.socket.connect(sock, address)
        sock.sendall(request)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        sock.close()
        return b"".join(chunks)

    def test_server_binds_loopback_and_serves_read_only(self):
        server = HQServer(0, self.root)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            ok = self.raw(port, f"GET / HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            self.assertTrue(ok.startswith(b"HTTP/1.0 200"))
            self.assertIn(b"Content-Security-Policy", ok)
            self.assertNotIn(b"Python/", ok)
            post = self.raw(port, f"POST /api/scene HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 2\r\n\r\n{{}}".encode())
            self.assertTrue(post.startswith(b"HTTP/1.0 405"))
            foreign = self.raw(port, b"GET /api/timelines HTTP/1.0\r\nHost: attacker.example\r\n\r\n")
            self.assertTrue(foreign.startswith(b"HTTP/1.0 403"))
            junk = self.raw(port, b"BREW /<script>alert(1)</script> HTCPCP/1.0\r\n\r\n")
            self.assertNotIn(b"<script>", junk)
        finally:
            server.shutdown()
            server.server_close()
        for bad in (80, 70000, "8765"):
            self.assertRaises(ValueError, HQServer, bad)

    def test_cli_rejects_bad_port(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = hq_main(["hq-serve", "--port", "80"])
        self.assertEqual((code, json.loads(output.getvalue())["error"]["code"]), (1, "hq_port_unavailable"))


class StaticSafetyTests(unittest.TestCase):
    def test_client_never_mutates_or_renders_html(self):
        sources = {name: (STATIC / name).read_text(encoding="utf-8") for name in ("app.js", "hq-core.js")}
        for name, source in sources.items():
            for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                              "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "localStorage",
                              "sessionStorage", "\"POST\"", "\"PUT\"", "\"DELETE\"", "\"PATCH\"", "postMessage"):
                with self.subTest(file=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)
            self.assertEqual(re.findall(r"https?://[^\"' ]+", source), ["http://www.w3.org/2000/svg"] if name == "app.js" else [])
        app = sources["app.js"]
        self.assertEqual(app.count("window.fetch("), 1)                     # one GET helper
        self.assertEqual(sorted(set(re.findall(r"\"(/api/[a-z/]+)", app))),        # Step 34 adds the results routes
                         ["/api/broker", "/api/content/at", "/api/content/latest", "/api/content/media", "/api/results",
                          "/api/results/at", "/api/results/summary", "/api/scene", "/api/session", "/api/sessions",
                      "/api/timelines"])                                   # Step 39 adds the read-only session routes
        # Step 35: source links open only on an explicit click, in a new tab without opener or referrer.
        self.assertEqual(app.count("window.open("), 1)
        self.assertIn('window.open(href, "_blank", "noopener,noreferrer")', app)
        self.assertNotIn('"href"', app)                                      # no anchors with live URLs are built
        self.assertIn('method: "GET"', app)
        self.assertIn("MAX_POLLS = 900", app)
        self.assertIn("document.hidden", app)
        self.assertNotIn("fetch", sources["hq-core.js"])                    # decoration logic has no network access
        decoration = app[app.index("function targetFor"):app.index("function spawnToken")]
        for forbidden in ("getJSON", "fetch", "loadScene", "S.replay.seek", "S.scene.frames"):
            self.assertNotIn(forbidden, decoration)

    def test_page_is_csp_compatible_and_self_contained(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html))
        self.assertIsNone(re.search(r"\sstyle=", html))
        self.assertIsNone(re.search(r"\son[a-z]+=", html))
        self.assertEqual(re.findall(r"https?://", html), [])
        css = (STATIC / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn("@import", css)
        self.assertNotIn("url(http", css)
        self.assertIn("prefers-reduced-motion", css)

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_client_logic_in_node(self):
        result = subprocess.run(["node", str(ROOT / "tests/hq_client_test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), {"passed": 18})


@unittest.skipUnless(os.environ.get("RUN_LOCAL_BROWSER_TESTS") == "1", "set RUN_LOCAL_BROWSER_TESTS=1 (needs Playwright + Chromium)")
class BrowserTests(EventBase):
    def test_house_in_a_real_browser(self):
        from playwright.sync_api import sync_playwright
        server = HQServer(0, self.root)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width, height in ((1600, 900), (390, 844)):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    problems = []
                    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
                    page.on("pageerror", lambda e: problems.append(str(e)))
                    page.goto(url)
                    page.wait_for_selector(".bot[data-bot='creator']")       # (the page's CSP forbids eval-based waits)
                    self.assertEqual(page.locator(".bot").count(), 8)
                    self.assertEqual(page.inner_text("#mode-badge"), "DEMO DATA · SYNTHETIC")
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    page.click("#btn-play")
                    page.keyboard.press("End")
                    self.assertEqual(page.inner_text("#position"), "36 / 36")
                    page.keyboard.press("Home")
                    self.assertEqual(page.inner_text("#position"), "0 / 36")
                    page.click("#btn-motion")
                    for _ in range(14):
                        page.click("#btn-forward")
                    self.assertEqual(page.get_attribute(".bot[data-bot='risk_review']", "data-state"), "blocked")
                    page.click("#inspector-body .link:has-text('Strategy Agent')")
                    self.assertIn("stage_error", page.inner_text("#inspector-body"))
                    page.keyboard.press("p")
                    self.assertTrue(page.is_visible("#mode-badge"))
                    self.assertFalse(page.is_visible(".sidebar"))
                    page.keyboard.press("Escape")
                    self.assertEqual(problems, [])
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
