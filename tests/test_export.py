"""Step 38: portable, locally viewable content-preview packages.

Local fixtures only (mocked Creator, renderer, media probe and poster reader); no network,
no credits. Evidence age is pinned through `vicekrack.review._clock_now`. Expected hashes
are computed here from the written files, independently of the exporter.
"""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_hq_content import MARKUP, PNG, TIMES, MarkupDrafter
from test_review import STALE, ReviewBase
from vicekrack.errors import NetworkError
from vicekrack.export import MANIFEST, PreviewExporter, verify_package
from vicekrack.production import ProductionStore

ALLOWED = {"index.html", "media/preview.mp4", "content/script.json", "content/provenance.json",
           "quality/quality-report.json", "review/review-summary.json", MANIFEST}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def files_of(folder):
    return {p.relative_to(folder).as_posix(): p for p in Path(folder).rglob("*") if p.is_file()}


def sources_tree(root):
    """Every source file the exporter may read, with its hash (must never change)."""
    out = {}
    for sub in ("runtime/productions", "runtime/quality", "runtime/reviews", "runtime/verification"):
        for path in sorted((root / sub).rglob("*")) if (root / sub).exists() else []:
            if path.is_file() and not path.name.endswith(".lock"):
                out[path.relative_to(root).as_posix()] = sha(path)
    return out


class ExportBase(ReviewBase):
    def export(self, report, purpose="review_copy", **options):
        return PreviewExporter(self.root, clock=lambda: TIMES[3]).export(self.pid, report["report_id"], purpose=purpose,
                                                                         **options)

    def refused(self, code, report, purpose="review_copy", **options):
        with self.assertRaises(NetworkError) as error:
            self.export(report, purpose, **options)
        self.assertEqual(error.exception.code, code)
        exports = self.root / "runtime/exports"
        self.assertEqual(sorted(p.name for p in exports.iterdir()) if exports.exists() else [], self.packages_before)
        return error.exception

    def setUp(self):
        super().setUp()
        self.packages_before = []

    def ready(self, **make):
        self.make(**make)
        report, _ = self.check()
        return report

    def rewrite_manifest(self, folder, mutate):
        path = Path(folder) / MANIFEST
        manifest = json.loads(path.read_text())
        mutate(manifest)
        path.write_text(json.dumps(manifest, indent=2))


class PurposeTests(ExportBase):
    def test_review_copy_contents_and_labels(self):
        report = self.ready()
        before = sources_tree(self.root)
        manifest, folder = self.export(report)
        found = files_of(folder)
        self.assertEqual(set(found), ALLOWED | {f"media/scene-{n}.png" for n in range(1, 5)})
        self.assertEqual({r["path"] for r in manifest["files"]}, set(found) - {MANIFEST})   # never lists itself
        for row in manifest["files"]:
            self.assertEqual((row["bytes"], row["sha256"]), (found[row["path"]].stat().st_size, sha(found[row["path"]])))
        self.assertEqual(sha(found["media/preview.mp4"]), sha(self.path("preview", "preview_file")))
        self.assertEqual(sha(found["content/script.json"]), sha(self.path("creator", "script_path")))
        self.assertEqual(sha(found["media/scene-2.png"]), sha(self.manifest_dir() / "scene-2.png"))
        self.assertEqual(sha(found["quality/quality-report.json"]),
                         sha(self.root / "runtime/quality" / f"{report['report_id']}.json"))
        self.assertEqual((manifest["purpose"], manifest["exported_at"], manifest["restrictions"]["publishable"],
                          manifest["restrictions"]["for_review"]), ("review_copy", TIMES[3], False, True))
        page = found["index.html"].read_text()
        self.assertIn("FOR REVIEW - not approved, not for publishing", page)
        self.assertIn("publishable: false", page)
        self.assertIn("not factual verification and not rights clearance", page)
        self.assertIn(TIMES[3], page)
        for derived in ("content/provenance.json", "review/review-summary.json"):
            document = json.loads(found[derived].read_text())
            self.assertTrue(document["derived"])
            self.assertRegex(document["statement"], r"not (the|an) original")
        provenance = json.loads(found["content/provenance.json"].read_text())
        self.assertEqual(provenance["sources_of_truth"]["brief"]["sha256"], sha(self.path("brief", "brief_path")))
        result = verify_package(folder)
        self.assertEqual((result["status"], result["problems"], result["files_checked"]), ("consistent", [], 10))
        self.assertEqual(sources_tree(self.root), before)                         # sources never change
        self.assertNotIn(str(self.root), json.dumps(manifest) + page)

    def test_approved_preview_requires_a_current_approval_and_keeps_its_acknowledgments(self):
        report = self.ready()
        self.refused("no_current_preview_approval", report, "approved_preview")
        first, _ = self.decide(report, decision="changes_requested")
        self.refused("no_current_preview_approval", report, "approved_preview")
        approval, _ = self.decide(report, supersedes=first["review_id"])
        manifest, folder = self.export(report, "approved_preview")
        self.assertEqual((manifest["source"]["approval_review_id"], manifest["restrictions"]["for_review"],
                          manifest["restrictions"]["publishable"]), (approval["review_id"], False, False))
        summary = json.loads((folder / "review/review-summary.json").read_text())
        self.assertEqual((summary["approval"]["review_id"], summary["approval"]["acknowledgments"]),
                         (approval["review_id"], []))
        self.assertEqual([d["applicability_at_export"] for d in summary["decisions"]], ["current", "superseded"])
        self.assertIn("snapshot", summary["statement"])
        page = (folder / "index.html").read_text()
        self.assertIn("APPROVED FOR PREVIEW ONLY - not approved for publishing", page)
        self.assertNotIn("FOR REVIEW - not approved", page)
        self.assertEqual(verify_package(folder)["status"], "consistent")
        self.packages_before = [folder.name]
        self.now = STALE                                                          # approval stops being current
        self.refused("no_current_preview_approval", report, "approved_preview")
        self.assertEqual(self.export(report)[0]["snapshot_at_export"]["evidence"]["status"], "stale")

    def test_acknowledged_approval_of_a_needs_review_report(self):
        from test_quality import GOOD_MEDIA
        from vicekrack.quality import ProbeUnavailable
        self.make()

        def unavailable(path):
            raise ProbeUnavailable()
        report, _ = self.check(prober=unavailable)
        self.assertEqual(report["result"], "needs_review")
        acks = ["needs_review_result", "unavailable_checks"]
        approval, _ = self.decide(report, acknowledgments=acks)
        manifest, folder = self.export(report, "approved_preview")
        summary = json.loads((folder / "review/review-summary.json").read_text())
        self.assertEqual(summary["approval"]["acknowledgments"], acks)
        self.assertEqual(manifest["snapshot_at_export"]["technical_result"], "needs_review")
        self.assertIn("unavailable_checks", (folder / "index.html").read_text())
        self.assertIsNotNone(GOOD_MEDIA)

    def test_draft_marking_is_kept(self):
        self.make_draft()
        from vicekrack.quality import Check, QualityChecker
        from test_quality import GOOD_MEDIA
        with patch("vicekrack.quality.QualityChecker._check_provenance", lambda checker: Check("provenance")):
            report, _ = QualityChecker(root=self.root, clock=lambda: TIMES[1], prober=lambda p: dict(GOOD_MEDIA),
                                       poster_reader=lambda p: ((1080, 1920), [(255, 190, 85)] * 2)).run(self.pid)
        manifest, folder = self.export(report)
        self.assertEqual((manifest["restrictions"]["draft"], manifest["restrictions"]["watermark"]),
                         (True, "DRAFT - NOT FOR PRODUCTION"))
        self.assertIn("DRAFT - NOT FOR PRODUCTION", (folder / "index.html").read_text())


class GateTests(ExportBase):
    def test_technical_failure_changed_and_missing_artifacts(self):
        self.make()
        self.original_poster = (self.manifest_dir() / "scene-2.png").read_bytes()
        (self.manifest_dir() / "scene-2.png").write_bytes(PNG + b"swapped")
        failed, _ = self.check()
        self.refused("technical_result_fail", failed)
        self.refused("technical_result_fail", failed, "approved_preview")
        good = self.ready_again()
        video = self.path("preview", "preview_file")
        video.write_bytes(video.read_bytes() + b"x")
        self.refused("binding_not_matching", good)
        video.write_bytes(video.read_bytes()[:-1])
        (self.manifest_dir() / "scene-3.png").unlink()
        self.refused("required_artifact_missing", good)

    def ready_again(self):
        (self.manifest_dir() / "scene-2.png").write_bytes(self.original_poster)
        report, _ = self.check(at=TIMES[2])
        self.assertEqual(report["result"], "pass")
        return report

    def test_legacy_report_is_refused(self):
        report = self.ready()
        legacy = {k: v for k, v in report.items() if k != "binding"}
        legacy.update(version="1.0", report_id="qr-" + "9" * 24)
        (self.root / "runtime/quality" / f"{legacy['report_id']}.json").write_text(json.dumps(legacy))
        self.refused("report_not_bound", legacy)

    def test_legacy_preview_manifest_with_a_bound_report(self):
        from test_hq_content import PngRenderer
        super(ExportBase, self).make(renderer=PngRenderer())               # Step 35 renderer: a 1.0 preview manifest
        bound, _ = self.check()
        manifest, folder = self.export(bound)
        self.assertEqual(manifest["source"]["preview_manifest_version"], "1.0")
        self.assertEqual(verify_package(folder)["status"], "consistent")     # posters bound by the report itself

    def test_corrupted_review_history(self):
        report = self.ready()
        first, path = self.decide(report)
        path.write_text(path.read_text()[:-10])
        self.refused("review_history_corrupted", report)
        self.refused("review_history_corrupted", report, "approved_preview")

    def test_incomplete_production(self):
        from test_production import FakeRenderer
        result = self.pipeline(FakeRenderer([NetworkError("render_failed", "x")])).produce(self.run_id, self.record_id)
        self.pid = result["production_id"]
        self.folder = ProductionStore(self.root).folder(self.pid)
        early, _ = self.check()
        self.refused("production_not_completed", early)
        with self.assertRaises(NetworkError) as error:
            PreviewExporter(self.root).export(self.pid, early["report_id"], purpose="publish")
        self.assertEqual(error.exception.code, "invalid_export_purpose")


class PrivacyTests(ExportBase):
    def test_defaults_exclude_labels_notes_state_and_configuration(self):
        report = self.ready()
        self.decide(report, reviewer="Reviewer Zed", notes="private note about scene 2")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-" + "Q" * 30}):
            manifest, folder = self.export(report)
        blob = b"".join(p.read_bytes() for p in files_of(folder).values())
        state = (self.folder / "state.json").read_bytes()
        for secret in (b"Reviewer Zed", b"private note about scene 2", b"sk-test-", str(self.root).encode(),
                       b'"selection_run_id"', b'"trace"', state[:200]):
            self.assertNotIn(secret, blob)                                    # no labels, notes, secrets or state
        self.assertFalse(any(p.endswith((".wav", ".lock")) or "config" in p for p in files_of(folder)))
        self.assertEqual(manifest["privacy"], {"reviewer_labels_included": False, "review_notes_included": False,
                                               "narration_source_included": False, "production_state_included": False,
                                               "configuration_included": False})
        summary = json.loads((folder / "review/review-summary.json").read_text())
        self.assertNotIn("reviewer_label", summary["decisions"][0])
        self.assertEqual(len(summary["decisions"][0]["record_sha256"]), 64)  # source hash kept

    def test_explicit_inclusion(self):
        report = self.ready()
        self.decide(report, reviewer="Reviewer Zed", notes=MARKUP)
        manifest, folder = self.export(report, include_reviewer_labels=True, include_review_notes=True)
        item = json.loads((folder / "review/review-summary.json").read_text())["decisions"][0]
        self.assertEqual((item["reviewer_label"], item["notes"], item["reviewer_authenticated"]),
                         ("Reviewer Zed", MARKUP, False))
        page = (folder / "index.html").read_text()
        self.assertIn("Reviewer Zed (self-declared)", page)
        self.assertNotIn(MARKUP, page)
        self.assertIn("&lt;img src=x onerror=&quot;document.title=1&quot;&gt;", page)
        self.assertEqual(verify_package(folder)["status"], "consistent")


class VerifyTests(ExportBase):
    def package(self):
        report = self.ready()
        return self.export(report)

    def copy(self, folder):
        target = Path(tempfile.mkdtemp()) / "moved"
        self.addCleanup(shutil.rmtree, target.parent, True)
        shutil.copytree(folder, target)
        return target

    def problems(self, folder):
        result = verify_package(folder)
        self.assertEqual(result["status"], "inconsistent")
        return result["problems"]

    def test_relocated_package_verifies_without_the_production(self):
        _, folder = self.package()
        moved = self.copy(folder)
        shutil.rmtree(self.root / "runtime")                                   # the original production is gone
        self.assertEqual(verify_package(moved)["status"], "consistent")
        from vicekrack.__main__ import main
        with patch("sys.argv", ["vicekrack", "export-verify", str(moved)]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(), 0)
        self.assertIn("not signatures", json.loads(out.getvalue())["note"])

    def test_tampering_and_missing_files(self):
        _, folder = self.package()
        cases = []
        moved = self.copy(folder)
        poster = moved / "media/scene-1.png"
        poster.write_bytes(poster.read_bytes()[:-1] + b"\x00")              # same size, different bytes
        cases.append((moved, "hash_mismatch"))
        moved = self.copy(folder)
        (moved / "content/script.json").unlink()
        cases.append((moved, "listed_file_missing"))
        moved = self.copy(folder)
        (moved / "media/extra.png").write_bytes(PNG)
        cases.append((moved, "unlisted_file_in_package"))
        moved = self.copy(folder)                                              # consistent rewrite of the manifest...
        (moved / "media/scene-1.png").write_bytes(PNG + b"other")
        self.rewrite_manifest(moved, lambda m: [r.update(sha256=sha(moved / "media/scene-1.png"),
                                                         bytes=(moved / "media/scene-1.png").stat().st_size)
                                                for r in m["files"] if r["path"] == "media/scene-1.png"])
        cases.append((moved, "payload_not_bound_by_report"))                   # ...still disagrees with the report
        moved = self.copy(folder)
        page = moved / "index.html"
        page.write_text(page.read_text().replace("</main>", '<script src="https://tracker.example/x.js"></script></main>'))
        self.rewrite_manifest(moved, lambda m: [r.update(sha256=sha(page), bytes=page.stat().st_size)
                                                for r in m["files"] if r["path"] == "index.html"])
        cases.append((moved, "page_contains_script"))
        moved = self.copy(folder)
        self.rewrite_manifest(moved, lambda m: m.update(purpose="approved_preview",
                                                        purpose_label="APPROVED FOR PREVIEW ONLY - not approved for publishing"))
        cases.append((moved, "package_manifest_invalid"))
        moved = self.copy(folder)
        self.rewrite_manifest(moved, lambda m: m["restrictions"].update(publishable=True))
        cases.append((moved, "package_manifest_invalid"))
        moved = self.copy(folder)
        (moved / MANIFEST).write_text("{broken")
        cases.append((moved, "package_manifest_unreadable"))
        for moved, code in cases:
            self.assertIn(code, self.problems(moved), code)
        self.assertEqual(verify_package(folder)["status"], "consistent")      # the original is untouched

    def test_unsafe_paths_and_links(self):
        report = self.ready()
        _, folder = self.export(report)
        moved = self.copy(folder)
        self.rewrite_manifest(moved, lambda m: m["files"][0].update(path="../outside.txt"))
        self.assertIn("package_manifest_invalid", self.problems(moved))
        if os.name != "nt":
            moved = self.copy(folder)
            outside = moved.parent / "outside.png"
            outside.write_bytes((moved / "media/scene-1.png").read_bytes())
            (moved / "media/scene-1.png").unlink()
            (moved / "media/scene-1.png").symlink_to(outside)
            self.assertIn("link_in_package", self.problems(moved))
            poster = self.manifest_dir() / "scene-4.png"                      # a production artifact via a link
            real = self.root / "elsewhere.png"
            real.write_bytes(poster.read_bytes())
            poster.unlink()
            poster.symlink_to(real)
            self.packages_before = [folder.name]
            self.refused("unsafe_or_invalid_artifact_path", report)
            poster.unlink()
            poster.write_bytes(real.read_bytes())
            exports = self.root / "runtime/exports"
            shutil.move(exports, self.root / "exports-real")
            exports.symlink_to(self.root / "exports-real")
            self.packages_before = [folder.name]
            with self.assertRaises(NetworkError) as error:
                self.export(report)
            self.assertEqual(error.exception.code, "invalid_export_storage")


class WriteTests(ExportBase):
    def test_concurrent_changes_are_detected(self):
        report = self.ready()
        video = self.path("preview", "preview_file")
        with patch("vicekrack.export._between_checks", side_effect=lambda: video.write_bytes(video.read_bytes() + b"x")):
            self.refused("sources_changed_during_export", report)
        video.write_bytes(video.read_bytes()[:-1])
        report_file = self.root / "runtime/quality" / f"{report['report_id']}.json"
        with patch("vicekrack.export._between_checks", side_effect=lambda: report_file.write_text(report_file.read_text() + " ")):
            self.refused("sources_changed_during_export", report)
        report_file.write_text(report_file.read_text()[:-1])

        def review_lands():
            first, _ = self.decide(report)              # holds no lock in this seam? recording needs it: refused
        with patch("vicekrack.export._between_checks", side_effect=review_lands):
            with self.assertRaises(NetworkError) as error:
                self.export(report)
            self.assertEqual(error.exception.code, "production_locked")    # the export holds the production lock
        with ProductionStore(self.root).lock(self.pid):
            self.refused("production_locked", report)
        self.assertFalse(any(p.name.startswith(".staging") for p in (self.root / "runtime/exports").iterdir())
                         if (self.root / "runtime/exports").exists() else False)

    def test_interrupted_write_and_no_overwrite(self):
        report = self.ready()
        with patch("vicekrack.export.os.rename", side_effect=OSError("power cut")):
            self.refused("export_write_failed", report)
        with patch("vicekrack.export.os.fsync", side_effect=OSError("disk full")):
            self.refused("export_write_failed", report)
        with patch("vicekrack.export.secrets.token_hex", return_value="a" * 24):
            manifest, folder = self.export(report)
            before = {p: sha(f) for p, f in files_of(folder).items()}
            self.packages_before = [folder.name]
            self.refused("package_exists", report)
        self.assertEqual({p: sha(f) for p, f in files_of(folder).items()}, before)
        (self.root / "runtime/exports" / (".staging-pkg-" + "b" * 24)).mkdir()     # what a crash could leave
        self.assertEqual(verify_package(folder)["status"], "consistent")

    def test_cli_export(self):
        report = self.ready()
        root = self.root
        from vicekrack.__main__ import main
        with patch("vicekrack.export_cli.PreviewExporter", lambda: PreviewExporter(root, clock=lambda: TIMES[3])), \
                patch("sys.argv", ["vicekrack", "export-preview", self.pid, "--report", report["report_id"],
                                   "--purpose", "review_copy"]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(), 0)
        result = json.loads(out.getvalue())
        self.assertEqual((result["purpose"], result["restrictions"]["publishable"]), ("review_copy", False))
        self.assertEqual(verify_package(result["package_dir"])["status"], "consistent")
        with patch("vicekrack.export_cli.PreviewExporter", lambda: PreviewExporter(root)), \
                patch("sys.argv", ["vicekrack", "export-preview", self.pid, "--report", report["report_id"],
                                   "--purpose", "approved_preview"]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], "no_current_preview_approval")


class PageTests(ExportBase):
    def test_html_escapes_text_and_uses_only_relative_files(self):
        self.make(drafter=MarkupDrafter())
        report, _ = self.check()
        _, folder = self.export(report)
        page = (folder / "index.html").read_text()
        self.assertNotIn(MARKUP, page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script", page.lower())
        import re
        for target in re.findall(r'(?:href|src|poster)="([^"]*)"', page):
            self.assertNotRegex(target, r"^(?:[a-z]+:|//|/|\.\.)")         # relative, inside the package
        self.assertNotIn("fonts.googleapis", page)
        self.assertEqual(verify_package(folder)["problems"], [])

    @unittest.skipUnless(os.environ.get("RUN_LOCAL_BROWSER_TESTS") == "1", "set RUN_LOCAL_BROWSER_TESTS=1")
    def test_offline_page_in_a_real_browser(self):
        from playwright.sync_api import sync_playwright
        from vicekrack.preview import render_preview
        self.make(renderer=render_preview, drafter=MarkupDrafter())              # one real local render
        report, _ = self.check()
        self.decide(report, reviewer="Rich (editor)", notes=MARKUP)
        _, folder = self.export(report, "approved_preview", include_reviewer_labels=True, include_review_notes=True)
        moved = Path(tempfile.mkdtemp()) / "package"
        self.addCleanup(shutil.rmtree, moved.parent, True)
        shutil.copytree(folder, moved)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width, height in ((1280, 900), (390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height}, java_script_enabled=True)
                remote, problems = [], []
                context.route("**/*", lambda route: (remote.append(route.request.url), route.abort())
                              if not route.request.url.startswith("file://") else route.continue_())
                page = context.new_page()
                page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: problems.append(str(e)))
                page.goto((moved / "index.html").as_uri())
                page.wait_for_load_state("load")
                self.assertIn("APPROVED FOR PREVIEW ONLY", page.inner_text("body"))
                self.assertIn(MARKUP, page.inner_text("body"))                   # shown as text...
                self.assertNotEqual(page.title(), "1")                           # ...never run
                self.assertEqual(page.locator("script, img:not([src^='media/'])").count(), 0)
                self.assertTrue(page.evaluate("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)"))
                self.assertEqual(page.evaluate("document.querySelector('video').getAttribute('src')"), "media/preview.mp4")
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                self.assertEqual((remote, problems), ([], []))
                shots = os.environ.get("HQ_SCREENSHOT_DIR")
                if shots:
                    kind = "phone" if width < 600 else "desktop"
                    page.screenshot(path=str(Path(shots) / f"hq-step38-package-{kind}.png"))
                    page.locator("section").nth(5).screenshot(path=str(Path(shots) / f"hq-step38-review-{kind}.png"))
                context.close()
            browser.close()
        self.assertEqual(verify_package(moved)["status"], "consistent")


if __name__ == "__main__":
    unittest.main()
