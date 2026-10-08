"""Step 35: Living HQ content results desk. Local fixture data; mocked Creator, renderer and media probe.
No network beyond 127.0.0.1, no credits. The browser test (opt-in) renders one real local preview.

Expected visibility, hashes and binding states are derived here from the saved state and the
written files, independently of the desk module.
"""

import hashlib
import json
import os
import shutil
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from test_events import tree
from test_quality import GOOD_MEDIA, Base as QualityBase, ManifestRenderer
from vicekrack.content_events import KINDS, ContentEvents
from vicekrack.creator import MockScriptDrafter
from vicekrack.errors import NetworkError
from vicekrack.events.sink import Recorder
from vicekrack.events.store import EventStore
from vicekrack.hq import api, content
from vicekrack.hq.content_demo import demo_inputs
from vicekrack.hq.demo import demo_events
from vicekrack.hq.server import HQServer
from vicekrack.orchestrator import read_json
from vicekrack.production import Pipeline, ProductionStore
from vicekrack.quality import QualityChecker

PORT = 8765
HOST = {"host": f"127.0.0.1:{PORT}"}
PNG = b"\x89PNG\r\n\x1a\n"
TIMES = ["2026-10-04T13:10:00Z", "2026-10-04T13:11:00Z", "2026-10-04T13:12:00Z", "2026-10-04T13:13:00Z"]
MARKUP = '<img src=x onerror="document.title=1"> & <script>alert(1)</script>'


def get(target, root=None, method="GET", headers=None):
    status, response_headers, body = api.respond(method, target, dict(HOST, **(headers or {})), port=PORT, root=root)
    if response_headers.get("Content-Type", "").startswith("application/json"):
        body = json.loads(body)
    return status, response_headers, body


class PngRenderer(ManifestRenderer):
    """ManifestRenderer, but posters start with a real PNG signature (the bytes are still fake)."""

    def __call__(self, plan, **kwargs):
        result = super().__call__(plan, **kwargs)
        for poster in Path(result["manifest_file"]).parent.glob("scene-*.png"):
            poster.write_bytes(PNG + b"fake poster " + poster.name.encode())
        return result


class MarkupDrafter:
    """Mock Creator output with HTML-like text in fields the desk displays (contract-valid)."""

    def draft(self, *, request, model):
        draft = MockScriptDrafter().draft(request=request, model=None)
        draft["beats"][0]["visual"]["description"] = MARKUP
        draft["beats"][1]["sound_cue"] = "<b>bold</b> cue"
        return draft


class ContentBase(QualityBase):
    def setUp(self):
        super().setUp()
        self.clock = TIMES[0]

    def events(self, kind="production"):
        timeline_kind, components = KINDS[kind]
        return ContentEvents(kind, lambda run_id, correlation_id: Recorder(
            department="content", kind=timeline_kind, components=components, run_id=run_id, correlation_id=correlation_id,
            store=EventStore(self.root), clock=lambda: self.clock))

    def pipeline(self, renderer=None, events=None, drafter=None):
        return Pipeline(root=self.root, clock=lambda: self.clock, renderer=renderer or PngRenderer(), events=events,
                        drafter=drafter)

    def make(self, record=False, renderer=None, drafter=None):
        events = self.events() if record else None
        result = self.pipeline(renderer, events, drafter).produce(self.run_id, self.record_id)
        if events is not None:
            events.close(result["status"])
        self.pid = result["production_id"]
        self.folder = ProductionStore(self.root).folder(self.pid)
        return result, (events.recorder.timeline_id if events else None)

    def quality(self, record=False, at=None):
        if at:
            self.clock = at
        events = self.events("quality") if record else None
        report, _ = QualityChecker(root=self.root, clock=lambda: self.clock, prober=lambda path: dict(GOOD_MEDIA),
                                   poster_reader=lambda path: ((1080, 1920), [(89, 214, 193)] * 2), events=events).run(self.pid)
        if events is not None:
            events.close("completed")
        return report, (events.recorder.timeline_id if events else None)

    def state(self):
        return ProductionStore(self.root).read(self.pid)

    def path(self, stage, key):
        return self.folder / next(s for s in self.state()["stages"] if s["name"] == stage)["artifacts"][key]

    def latest(self, timeline=None):
        return content.content_latest(timeline or self.pid, self.root)

    def statuses(self, document):
        return {name: section["status"] for name, section in document["artifacts"].items()}


class VerifiedProductionTests(ContentBase):
    def test_latest_matches_saved_files(self):
        self.make()
        doc = self.latest()
        self.assertEqual(set(self.statuses(doc).values()), {"verified"})
        state = self.state()
        brief = read_json(self.path("brief", "brief_path"))
        script = read_json(self.path("creator", "script_path"))
        self.assertEqual(doc["artifacts"]["brief"]["data"]["claims"][0]["text"], brief["claims"][0]["text"])
        self.assertEqual([b["narration"] for b in doc["artifacts"]["script"]["data"]["beats"]],
                         [b["narration"] for b in script["beats"]])
        video = doc["artifacts"]["preview"]["data"]["video"]
        self.assertEqual(video["sha256"], hashlib.sha256(self.path("preview", "preview_file").read_bytes()).hexdigest())
        self.assertEqual([s["status"] for s in state["stages"]], [s["status"] for s in doc["stages"]])
        posters = [s["poster"] for s in doc["artifacts"]["plan"]["data"]["scenes"]]
        self.assertEqual({p["status"] for p in posters}, {"not_hash_bound"})
        self.assertEqual((doc["restrictions"]["publishable"], doc["restrictions"]["preview_only"], doc["restrictions"]["draft"]),
                         (False, True, False))
        self.assertEqual(doc["artifacts"]["verification"]["status"], "verified")
        self.assertTrue(all(source["link_allowed"] for source in doc["artifacts"]["brief"]["data"]["sources"]))
        self.assertEqual(doc["quality"]["status"], "none_saved")

    def test_text_is_returned_as_data_not_markup(self):
        self.make(drafter=MarkupDrafter())
        beats = self.latest()["artifacts"]["script"]["data"]["beats"]
        self.assertEqual((beats[0]["visual"]["description"], beats[1]["sound_cue"]), (MARKUP, "<b>bold</b> cue"))
        status, headers, _ = get(f"/api/content/latest?timeline={self.pid}", self.root)
        self.assertEqual((status, headers["Content-Type"]), (200, "application/json; charset=utf-8"))

    def test_safe_link_rules(self):
        for url in ("https://official.example.com/a?b=1", "http://example.org/x"):
            self.assertTrue(content.safe_link(url), url)
        for url in ("javascript:alert(1)", "data:text/html,x", "https://user:pw@example.com/", "ftp://example.com/x",
                    "https://", "https://exa mple.com/", "//example.com/x", "https://example.com:99999/", None, 7,
                    "https://example.com/" + "a" * 2100, "vbscript:x", "https://example.com/\x00"):
            self.assertFalse(content.safe_link(url), url)


class TamperTests(ContentBase):
    def test_tampered_brief_hides_it_and_everything_after(self):
        self.make()
        path = self.path("brief", "brief_path")
        path.write_text(path.read_text(encoding="utf-8").replace("Fixture", "Edited", 1), encoding="utf-8")
        doc = self.latest()
        self.assertEqual(self.statuses(doc), {"brief": "tampered", "script": "unavailable", "validation": "unavailable",
                                              "plan": "unavailable", "preview": "unavailable", "verification": "verified"})
        self.assertIsNone(doc["artifacts"]["brief"]["data"])
        self.assertNotIn("Edited", json.dumps(doc))
        self.assertNotIn("med-", json.dumps(doc))               # no media is offered from a broken chain

    def test_consistently_rehashed_script_is_still_rejected(self):
        self.make()
        path = self.path("creator", "script_path")
        script = read_json(path)
        script["claims"][0]["text"] = "A different claim the brief never allowed."
        path.write_text(json.dumps(script))
        state = self.state()
        state["stages"][1]["artifacts"]["script_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        ProductionStore(self.root).write(state)
        doc = self.latest()
        self.assertIn(doc["artifacts"]["script"]["status"], ("mismatched", "tampered"))
        self.assertNotIn("never allowed", json.dumps(doc))

    def test_video_swap_and_manifest_mismatch(self):
        self.make()
        self.path("preview", "preview_file").write_bytes(b"another video")
        doc = self.latest()
        self.assertEqual((doc["artifacts"]["preview"]["status"], doc["artifacts"]["preview"]["reason"]),
                         ("tampered", "artifact_hash_mismatch"))
        self.assertEqual(doc["artifacts"]["plan"]["status"], "verified")      # earlier artifacts stay visible

    def test_missing_files(self):
        self.make()
        self.path("plan", "plan_path").unlink()
        doc = self.latest()
        self.assertEqual((doc["artifacts"]["plan"]["status"], doc["artifacts"]["plan"]["reason"]), ("missing", "artifact_missing"))
        self.assertEqual(doc["artifacts"]["preview"]["status"], "unavailable")
        self.assertEqual(doc["artifacts"]["script"]["status"], "verified")

    def test_missing_and_fake_posters(self):
        self.make()
        manifest_dir = self.path("preview", "manifest_file").parent
        (manifest_dir / "scene-1.png").unlink()
        (manifest_dir / "scene-2.png").write_bytes(b"not a png")
        posters = [s["poster"] for s in self.latest()["artifacts"]["plan"]["data"]["scenes"]]
        self.assertEqual([(p["status"], p["reason"]) for p in posters[:2]],
                         [("missing", "poster_missing"), ("tampered", "poster_not_png")])
        self.assertEqual([p["media_id"] for p in posters[:2]], [None, None])

    @unittest.skipIf(os.name == "nt", "symbolic links need extra rights on Windows")
    def test_symlinks_are_never_followed(self):
        self.make()
        outside = self.root / "outside.mp4"
        video = self.path("preview", "preview_file")
        outside.write_bytes(video.read_bytes())
        video.unlink()
        video.symlink_to(outside)                               # same bytes and hash, but outside via a link
        doc = self.latest()
        self.assertEqual((doc["artifacts"]["preview"]["status"], doc["artifacts"]["preview"]["reason"]),
                         ("tampered", "artifact_path_invalid"))
        poster_dir = self.path("preview", "manifest_file").parent
        (poster_dir / "scene-3.png").unlink()
        (poster_dir / "scene-3.png").symlink_to(self.root / "outside.mp4")
        self.assertEqual(content.safe_file(poster_dir, "scene-3.png", 10 ** 9), (None, "path_invalid"))

    def test_windows_saved_paths_are_accepted_and_still_checked(self):
        self.make()
        state = self.state()
        for key in ("preview_file", "manifest_file"):              # how Windows saves them: backslash separators
            state["stages"][4]["artifacts"][key] = state["stages"][4]["artifacts"][key].replace("/", "\\")
        ProductionStore(self.root).write(state)
        doc = self.latest()
        self.assertEqual(doc["artifacts"]["preview"]["status"], "verified")
        video_id = doc["artifacts"]["preview"]["data"]["video"]["media_id"]
        self.assertEqual(get(f"/api/content/media?production={self.pid}&id={video_id}", self.root)[0], 200)
        state["stages"][4]["artifacts"]["preview_file"] = "previews\\..\\..\\state.json"
        ProductionStore(self.root).write(state)
        doc = self.latest()
        self.assertIn(doc["artifacts"]["preview"]["status"], ("mismatched", "tampered"))
        self.assertNotIn("med-", json.dumps(doc["artifacts"]["preview"]))
        self.assertEqual(content.safe_file(self.folder, "previews\\..\\..\\state.json", 10 ** 9), (None, "path_invalid"))

    def test_safe_file_rejects_escapes(self):
        self.make()
        for relative in ("../state.json", "/etc/passwd", "a/../../x", "..", "", "..\\state.json", "C:\\x", "C:x",
                         "\\\\server\\share", "previews/./x", "a\x00b", None):
            self.assertEqual(content.safe_file(self.folder, relative, 10 ** 9)[0], None, relative)

    def test_tampered_state_is_rejected(self):
        self.make()
        path = self.folder / "state.json"
        state = json.loads(path.read_text())
        state["status"] = "running"
        path.write_text(json.dumps(state))
        status, _, body = get(f"/api/content/latest?timeline={self.pid}", self.root)
        self.assertEqual((status, body["error"]["code"]), (409, "invalid_production_state"))
        self.assertNotIn(str(self.root), json.dumps(body))


class QualityBindingTests(ContentBase):
    def test_bound_report_matches_and_legacy_report_stays_unverified(self):
        # Step 36 replaced the Step 35 rule "never current": a bound (1.1) report can be re-checked by hash.
        self.make()
        report, _ = self.quality(at=TIMES[1])
        row = self.latest()["quality"]["reports"][0]
        self.assertEqual((row["report_id"], row["result"], row["binding"], row["report_version"]),
                         (report["report_id"], report["result"], "matching", "1.1"))
        legacy = {k: v for k, v in report.items() if k != "binding"}
        legacy.update(version="1.0", report_id="qr-" + "9" * 24)
        (self.root / "runtime/quality" / f"{legacy['report_id']}.json").write_text(json.dumps(legacy))
        rows = {r["report_id"]: r for r in self.latest()["quality"]["reports"]}
        self.assertEqual(rows[legacy["report_id"]]["binding"], "legacy_unverified")
        self.assertIn("report_records_no_artifact_hashes", rows[legacy["report_id"]]["binding_reasons"])

    def test_report_that_predates_the_artifacts_is_stale(self):
        # First attempt fails at the preview; a quality check runs on the incomplete production; then resume.
        from test_production import FakeRenderer
        result = self.pipeline(FakeRenderer([NetworkError("render_failed", "x")])).produce(self.run_id, self.record_id)
        self.pid = result["production_id"]
        self.folder = ProductionStore(self.root).folder(self.pid)
        early, _ = self.quality(at=TIMES[1])
        self.assertEqual(early["result"], "fail")
        self.clock = TIMES[2]
        self.assertEqual(self.pipeline(PngRenderer()).resume(self.pid)["status"], "completed")
        late, _ = self.quality(at=TIMES[3])
        rows = {r["report_id"]: r for r in self.latest()["quality"]["reports"]}
        self.assertEqual(rows[early["report_id"]]["binding"], "unavailable")    # it bound an unfinished production
        self.assertIn("checked_before_artifacts_finished", rows[early["report_id"]]["binding_reasons"])
        self.assertIn("production_status_changed_since_check", rows[early["report_id"]]["binding_reasons"])
        self.assertIn("not_bound_incomplete", rows[early["report_id"]]["binding_reasons"])
        self.assertEqual(rows[late["report_id"]]["binding"], "matching")
        self.assertEqual(list(rows), [late["report_id"], early["report_id"]])      # newest first

    def test_tampered_renamed_and_foreign_reports(self):
        self.make()
        report, _ = self.quality(at=TIMES[1])
        folder = self.root / "runtime/quality"
        good = folder / f"{report['report_id']}.json"
        forged = dict(report, result="pass", reasons=[], report_id="qr-" + "a" * 24)
        (folder / f"qr-{'b' * 24}.json").write_text(json.dumps(forged))                 # name does not match its ID
        other = dict(report, production_id="prod-" + "c" * 24, report_id="qr-" + "d" * 24)
        (folder / f"qr-{'d' * 24}.json").write_text(json.dumps(other))                  # another production
        (folder / f"qr-{'e' * 24}.json").write_text("{not json")
        quality = self.latest()["quality"]
        self.assertEqual([r["report_id"] for r in quality["reports"]], [report["report_id"]])
        self.assertEqual(quality["rejected_reports"], [{"report_id": "qr-" + "b" * 24, "code": "quality_report_id_mismatch"}])
        self.assertEqual(quality["unreadable_reports_skipped"], 1)
        good.write_text(good.read_text().replace('"technical_checks_only": true', '"technical_checks_only": false'))
        quality = self.latest()["quality"]
        self.assertEqual(quality["reports"], [])
        self.assertIn({"report_id": report["report_id"], "code": "quality_report_invalid"}, quality["rejected_reports"])
        self.assertEqual(quality["status"], "rejected")

    def test_no_check_runs_automatically(self):
        self.make()
        with patch("vicekrack.quality.QualityChecker.run", side_effect=AssertionError("ran")), \
                patch("vicekrack.quality.probe_media", side_effect=AssertionError("probed")):
            self.assertEqual(self.latest()["quality"]["status"], "none_saved")
        self.assertFalse((self.root / "runtime/quality").exists())


class ReplayTests(ContentBase):
    def visible(self, timeline, position):
        doc = content.content_at(timeline, position, self.root)
        return doc, {k for k, v in (doc["artifacts"] or {}).items() if v["status"] == "verified"}

    def test_reconstructed_timeline_reveals_stages_in_order(self):
        self.make()
        expected, seen = [], set()
        stage_artifact = {"brief": {"brief", "verification"}, "creator": {"script"}, "validate": {"validation"},
                          "plan": {"plan"}, "preview": {"preview"}}
        for entry in self.state()["trace"]:
            if entry["event"] == "completed":
                seen |= stage_artifact[entry["stage"]]
            expected.append(set(seen))
        for position in range(len(expected) + 1):
            doc, shown = self.visible(self.pid, position)
            self.assertEqual(shown, expected[position - 1] if position else set(), position)
            if "preview" not in shown:
                self.assertNotIn("med-", json.dumps(doc))       # no later preview is offered early
            if "script" not in shown:
                self.assertNotIn("Follow for more", json.dumps(doc))
            self.assertEqual(doc["quality"]["status"], "not_in_timeline")

    def test_recorded_attempts_failure_then_reuse(self):
        from test_production import FakeRenderer
        events = self.events()
        first = self.pipeline(FakeRenderer([NetworkError("render_failed", "x")]), events).produce(self.run_id, self.record_id)
        events.close(first["status"])
        self.pid = first["production_id"]
        attempt_one = events.recorder.timeline_id
        self.clock = TIMES[1]
        events = self.events()
        self.assertEqual(self.pipeline(PngRenderer(), events).resume(self.pid)["status"], "completed")
        events.close("completed")
        attempt_two = events.recorder.timeline_id
        count = len(EventStore(self.root).load(attempt_one)["events"])
        doc, shown = self.visible(attempt_one, count)
        self.assertNotIn("preview", shown)                      # the preview that exists now was made later
        self.assertEqual(next(s for s in doc["stages"] if s["stage"] == "preview")["state"], "failed")
        doc, shown = self.visible(attempt_two, 1)
        self.assertEqual(shown, set())
        doc, shown = self.visible(attempt_two, 5)               # pipeline start + 4 reused stages
        self.assertEqual(shown, {"brief", "verification", "script", "validation", "plan"})
        self.assertTrue(all(s["reused"] for s in doc["stages"] if s["stage"] in ("brief", "creator", "validate", "plan")))
        total = len(EventStore(self.root).load(attempt_two)["events"])
        self.assertIn("preview", self.visible(attempt_two, total)[1])
        attempts = self.latest()["attempts"]
        self.assertEqual([a["timeline_id"] for a in attempts], [attempt_one, attempt_two])
        self.assertEqual((attempts[0]["failures"][0]["stage"], attempts[1]["stages_reused"]),
                         ("preview", ["brief", "creator", "validate", "plan"]))

    def test_unprovable_history_is_unavailable(self):
        _, recorded = self.make(record=True)
        view = EventStore(self.root).load(recorded)
        cases = {
            "timeline_predates_production": lambda v: v.update(started_at="2026-10-01T00:00:00Z"),
            "artifact_completion_time_not_in_attempt": lambda v: [e.update(recorded_at="2026-10-04T13:09:00Z")
                                                                  for e in v["events"] if e["event_type"] == "stage_completed"],
            "timeline_has_issues": lambda v: v.update(issues=["missing_events"]),
        }
        for reason, change in cases.items():
            altered = deepcopy(view)
            change(altered)
            with self.subTest(reason=reason), patch("vicekrack.events.cli.load_timeline", return_value=altered):
                doc = content.content_at(recorded, len(view["events"]), self.root)
                self.assertEqual(doc["historical"]["status"], "unavailable")
                self.assertIn(reason, doc["historical"]["reasons"])
                self.assertIsNone(doc["artifacts"])
                self.assertEqual(content.content_latest(recorded, self.root)["view"], "latest")
        # A trace that may be truncated cannot prove what existed when.
        with patch("vicekrack.production_timeline.MAX_TRACE", 3):
            doc = content.content_at(self.pid, 4, self.root)
        self.assertIn("timeline_has_issues", doc["historical"]["reasons"])

    def test_timeline_of_another_production_is_rejected(self):
        _, recorded = self.make(record=True)
        view = deepcopy(EventStore(self.root).load(recorded))
        view["correlation_id"] = "cor-" + "0" * 24
        with patch("vicekrack.events.cli.load_timeline", return_value=view):
            status, _, body = get(f"/api/content/at?timeline={recorded}&position=1", self.root)
        self.assertEqual((status, body["error"]["code"]), (409, "timeline_production_mismatch"))

    def test_quality_timeline_shows_report_only_after_it_was_saved(self):
        self.make()
        self.clock = TIMES[1]
        report, recorded = self.quality(record=True)
        before = content.content_at(recorded, 1, self.root)
        after = content.content_at(recorded, 2, self.root)
        self.assertEqual((before["quality"]["status"], before["quality"]["reports"]), ("not_yet", []))
        self.assertNotIn(report["report_id"], json.dumps(before))
        self.assertEqual([r["report_id"] for r in after["quality"]["reports"]], [report["report_id"]])
        self.assertEqual(self.statuses(before)["preview"], "verified")    # finished before the check started

    def test_other_timelines_are_not_productions(self):
        dataset_free = get("/api/content/at?timeline=srun-" + "a" * 24 + "&position=0", self.root)
        self.assertEqual((dataset_free[0], dataset_free[2]["error"]["code"]), (422, "content_not_production"))
        status, _, body = get("/api/content/latest?timeline=prod-" + "f" * 24, self.root)
        self.assertEqual((status, body["error"]["code"]), (404, "production_not_found"))


class MediaTests(ContentBase):
    def ids(self):
        doc = self.latest()
        return doc["artifacts"]["preview"]["data"]["video"]["media_id"], \
            [s["poster"]["media_id"] for s in doc["artifacts"]["plan"]["data"]["scenes"]]

    def test_serves_validated_files_with_ranges(self):
        self.make()
        video_id, posters = self.ids()
        data = self.path("preview", "preview_file").read_bytes()
        url = f"/api/content/media?production={self.pid}&id={video_id}"
        status, headers, body = get(url, self.root)
        self.assertEqual((status, headers["Content-Type"], body, headers["Accept-Ranges"]), (200, "video/mp4", data, "bytes"))
        self.assertIn("media-src 'self'", headers["Content-Security-Policy"])
        cases = {"bytes=0-": (206, data, f"bytes 0-{len(data) - 1}/{len(data)}"),
                 "bytes=2-5": (206, data[2:6], f"bytes 2-5/{len(data)}"),
                 "bytes=-4": (206, data[-4:], f"bytes {len(data) - 4}-{len(data) - 1}/{len(data)}"),
                 "bytes=3-999999": (206, data[3:], f"bytes 3-{len(data) - 1}/{len(data)}")}
        for wanted, (code, part, content_range) in cases.items():
            status, headers, body = get(url, self.root, headers={"range": wanted})
            self.assertEqual((status, body, headers["Content-Range"]), (code, part, content_range), wanted)
        for wanted in ("bytes=999999-", "bytes=5-2", "bytes=0-1,4-5", "bytes=abc", "items=0-1", "bytes=-0", "bytes=-"):
            status, headers, body = get(url, self.root, headers={"range": wanted})
            self.assertEqual((status, headers["Content-Range"], body), (416, f"bytes */{len(data)}", b""), wanted)
        with patch("vicekrack.hq.api.MAX_RANGE_BYTES", 3):
            status, headers, body = get(url, self.root, headers={"range": "bytes=0-"})
        self.assertEqual((status, body, headers["Content-Range"]), (206, data[:3], f"bytes 0-2/{len(data)}"))
        with patch("vicekrack.hq.api.MAX_FULL_BYTES", 3):
            status, _, body = get(url, self.root)
        self.assertEqual((status, body["error"]["code"]), (413, "media_range_required"))
        status, headers, body = get(f"/api/content/media?production={self.pid}&id={posters[0]}", self.root)
        self.assertEqual((status, headers["Content-Type"], body[:8]), (200, "image/png", PNG))

    def test_media_boundaries(self):
        self.make()
        video_id, _ = self.ids()
        base = f"/api/content/media?production={self.pid}"
        for target in (base + "&id=med-" + "0" * 24, f"/api/content/media?production=prod-{'1' * 24}&id={video_id}"):
            self.assertIn(get(target, self.root)[0], (404,), target)
        for target in (base, base + "&id=../state.json", base + "&id=" + video_id + "&id=" + video_id,
                       f"/api/content/media?production=../x&id={video_id}", base + "&id=" + video_id + "&path=state.json",
                       base + "&id=MED-" + "0" * 24):
            status, _, body = get(target, self.root)
            self.assertIn(status, (400, 422), target)
        for target in ("/api/content/media/../../state.json", "/api/content/mediax", f"/runtime/productions/{self.pid}/state.json"):
            self.assertEqual(get(target, self.root)[0], 404, target)
        for method in ("POST", "PUT", "DELETE", "HEAD", "OPTIONS"):
            self.assertEqual(get(base + "&id=" + video_id, self.root, method=method)[0], 405)
        for headers in ({"host": "evil.example"}, {"origin": "http://evil.example"}, {"sec-fetch-site": "cross-site"}):
            self.assertEqual(get(base + "&id=" + video_id, self.root, headers=headers)[0], 403, headers)
        # A file changed after the page loaded is not served under the old ID.
        self.path("preview", "preview_file").write_bytes(b"changed")
        self.assertEqual(get(base + "&id=" + video_id, self.root)[0], 404)

    def test_poster_ids_belong_to_one_production(self):
        self.make()
        _, posters = self.ids()
        self.assertEqual(len(set(posters)), 4)
        self.assertTrue(all(p.startswith("med-") for p in posters))
        self.assertNotIn("scene-1.png", json.dumps(self.latest()["artifacts"]["plan"]))     # no paths are sent


class BoundaryTests(ContentBase):
    def test_read_only_and_runs_nothing(self):
        _, recorded = self.make(record=True)
        self.quality(at=TIMES[1])
        before = tree(self.root)
        guards = ["vicekrack.production.Pipeline._run", "vicekrack.production.ProductionStore.write",
                  "vicekrack.production.ProductionStore.lock", "vicekrack.preview.render_preview",
                  "vicekrack.creator.draft_short_script", "vicekrack.quality.QualityChecker.run",
                  "vicekrack.quality.probe_media", "vicekrack.events.store.EventStore.open", "socket.socket.connect"]
        for target in guards:
            guard = patch(target, side_effect=AssertionError("must not run: " + target))
            guard.start()
            self.addCleanup(guard.stop)
        doc = self.latest()
        video_id = doc["artifacts"]["preview"]["data"]["video"]["media_id"]
        for timeline in (self.pid, recorded, "demo"):
            status, _, latest = get(f"/api/content/latest?timeline={timeline}", self.root)
            self.assertEqual(status, 200)
            for position in range(latest["timeline"]["event_count"] + 1):
                self.assertEqual(get(f"/api/content/at?timeline={timeline}&position={position}", self.root)[0], 200)
        self.assertEqual(get(f"/api/content/media?production={self.pid}&id={video_id}", self.root,
                             headers={"range": "bytes=0-10"})[0], 206)
        self.assertEqual(tree(self.root), before)                 # no lock, state, report, event or media file touched

    def test_errors_never_leak(self):
        secret = "sk-" + "z" * 30
        for name, route in (("content_latest", "/api/content/latest?timeline=demo"),
                            ("content_at", "/api/content/at?timeline=demo&position=1"),
                            ("media_file", f"/api/content/media?production=prod-{'1' * 24}&id=med-{'2' * 24}")):
            with self.subTest(name=name), patch(f"vicekrack.hq.api.{name}", side_effect=RuntimeError(f"{secret} /home/x")):
                status, _, body = get(route)
            self.assertEqual(status, 500)
            self.assertNotIn(secret, json.dumps(body))
            self.assertNotIn("/home", json.dumps(body))

    def test_requests_are_allowlisted(self):
        for target in ("/api/content/at?timeline=demo", "/api/content/at?timeline=demo&position=-1",
                       "/api/content/at?timeline=demo&position=01", "/api/content/latest?timeline=demo&position=1",
                       "/api/content/latest?timeline=../x", "/api/content/latest"):
            status, _, body = get(target)
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_content_request"), target)
        status, _, body = get("/api/content/at?timeline=demo&position=999")
        self.assertEqual((status, body["error"]["code"]), (400, "content_position_out_of_range"))


class DemoTests(unittest.TestCase):
    def test_demo_is_labelled_deterministic_and_correlated(self):
        first, second = content.content_latest("demo"), content.content_latest("demo")
        self.assertEqual(first, second)
        self.assertTrue(first["demo"])
        self.assertIn("DEMO DATA", first["notice"])
        self.assertEqual(first["artifacts"]["preview"]["status"], "not_produced")    # no media files in the demo
        self.assertEqual(first["quality"]["reports"][0]["binding"], "unavailable")     # the demo has no files to bind
        view, state, _, _ = demo_inputs()
        self.assertEqual(len(view["events"]), len(demo_events()))
        established, reports, reasons = content.establish(view, state, demo=True)
        self.assertEqual((reasons, sorted(established)), ([], sorted(["brief", "creator", "validate", "plan", "preview"])))
        quality_at = list(reports.values())[0]
        self.assertEqual(content.content_at("demo", quality_at - 1)["quality"]["status"], "not_yet")
        self.assertEqual(content.content_at("demo", quality_at)["quality"]["status"], "available")

    def test_demo_fixture_is_a_valid_chain(self):
        data = json.loads((Path(content.__file__).parent / "content_demo.json").read_text())
        from vicekrack.scene_plan import validate_scene_plan
        from vicekrack.story_brief import validate_story_brief
        validate_story_brief(data["brief"])
        validate_scene_plan(data["plan"])
        self.assertEqual(data["plan"]["script"]["claims"], data["brief"]["claims"])


@unittest.skipUnless(os.environ.get("RUN_LOCAL_BROWSER_TESTS") == "1", "set RUN_LOCAL_BROWSER_TESTS=1 (needs Playwright + Chromium)")
class ContentBrowserTests(ContentBase):
    def test_content_desk_in_a_real_browser(self):
        from playwright.sync_api import sync_playwright
        from vicekrack.preview import render_preview
        self.make(renderer=render_preview, drafter=MarkupDrafter())           # one real local render (offline)
        self.quality(at=TIMES[1])
        server = HQServer(0, self.root)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width, height in ((1600, 900), (390, 844)):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    problems, other, media = [], [], []
                    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
                    page.on("pageerror", lambda e: problems.append(str(e)))
                    page.on("request", lambda r: other.append(r.url) if r.method != "GET" or not r.url.startswith(url) else None)
                    page.on("response", lambda r: media.append((r.status, r.headers.get("content-type"),
                                                                r.headers.get("content-range"))) if "/api/content/media" in r.url else None)
                    page.goto(url)
                    page.wait_for_selector(".bot[data-bot='creator']")
                    page.click("#btn-play")
                    page.click(".station-btn:has-text('Preview renderer')")   # a production station opens the desk
                    page.wait_for_selector("#content-view:not([hidden]) .facts")
                    self.assertIn("DEMO DATA", page.inner_text("#content-badge"))
                    self.assertTrue(page.is_visible("#content-restriction"))
                    page.keyboard.press("Home")
                    for _ in range(22):
                        page.click("#btn-forward")                             # demo: just after the brief completed
                    page.wait_for_selector("text=Demo text: the studio says")
                    self.assertIn("Not established at this replay position", page.inner_text("#content-body"))
                    page.click(".view-btn[data-view='timeline']")
                    page.click(".timeline-item:has-text('content production')")
                    page.wait_for_selector("#timeline-name:has-text('content production')")
                    page.keyboard.press("c")
                    page.wait_for_selector("#content-badge:has-text('SAVED PRODUCTION')")
                    page.keyboard.press("End")
                    page.wait_for_selector("video")
                    page.wait_for_selector("img.poster")
                    self.assertIn(MARKUP, page.inner_text("#content-body"))   # shown as text...
                    self.assertEqual(page.locator("#content-body img:not(.poster)").count(), 0)   # ...never as markup
                    self.assertEqual(page.locator("#content-body script, #content-body b").count(), 0)
                    self.assertNotEqual(page.title(), "1")
                    self.assertEqual(page.locator("#content-body a[href]").count(), 0)          # URLs are text
                    page.click("#ct-tab-latest")
                    page.wait_for_selector(".report")
                    report = page.inner_text(".report")
                    for label in ("Artifact binding\nMATCHING", "Technical result\nPASS", "Evidence freshness at check"):
                        self.assertIn(label, report)                            # three separate labels (Step 36)
                    self.assertIn("Poster hash-verified", page.inner_text("#content-body"))
                    self.assertIn("every poster hash-bound", page.inner_text("#content-body"))
                    self.assertIn("Evidence freshness now", page.inner_text("#content-body"))
                    shots = os.environ.get("HQ_SCREENSHOT_DIR")
                    if shots:
                        page.locator(".report").first.scroll_into_view_if_needed()
                        page.screenshot(path=str(Path(shots) / f"hq-step36-binding-{'phone' if width < 600 else 'desktop'}.png"))
                    self.assertTrue(any(s == 206 and t == "video/mp4" and r for s, t, r in media), media)
                    self.assertTrue(any(s == 200 and t == "image/png" for s, t, _ in media), media)
                    page.keyboard.press("p")
                    self.assertTrue(page.is_visible("#content-view"))
                    self.assertTrue(page.is_visible("#mode-badge"))
                    page.keyboard.press("Escape")
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    self.assertEqual((problems, other), ([], []))
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
