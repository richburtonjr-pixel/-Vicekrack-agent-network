"""Step 36: verifiable content artifacts and quality-report binding.

Local fixtures only: mocked Creator, renderer, media probe and poster reader. No network, no
credits. Expected hashes are computed here from the written files, independently of the code
under test.
"""

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_events import tree
from test_hq_content import PNG, TIMES, ContentBase, PngRenderer, get
from test_quality import GOOD_MEDIA
from vicekrack import artifact_binding as ab
from vicekrack.errors import NetworkError
from vicekrack.hq import content
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.preview import render_preview
from vicekrack.production import Pipeline, ProductionStore
from vicekrack.quality import QualityChecker, binding_of, load_report, validate_report
from vicekrack.scene_plan import build_scene_plan


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BoundRenderer(PngRenderer):
    """Writes a 1.1 package (poster hashes and sizes), like the Step 36 renderer."""

    def __call__(self, plan, **kwargs):
        result = super().__call__(plan, **kwargs)
        folder = Path(result["manifest_file"]).parent
        video = folder / "preview.mp4"
        video.write_bytes(b"fake mp4 video " * 20 + plan["plan_id"].encode())
        manifest = json.loads((folder / "manifest.json").read_text())
        manifest.update(version="1.1", video_sha256=sha(video), video_bytes=video.stat().st_size)
        for scene in manifest["scenes"]:
            poster = folder / scene["poster"]
            scene.update(poster_sha256=sha(poster), poster_bytes=poster.stat().st_size)
        (folder / "manifest.json").write_text(json.dumps(manifest))
        return result


class Step36Base(ContentBase):
    def make(self, record=False, renderer=None, drafter=None):
        return super().make(record, renderer or BoundRenderer(), drafter)

    def manifest_dir(self):
        return self.path("preview", "manifest_file").parent

    def row(self):
        return self.latest()["quality"]["reports"][0]

    def binding(self, report):
        return ab.verify_binding(report, self.state(), self.folder, self.root)


class RendererManifestTests(unittest.TestCase):
    """The real renderer (encoder mocked) writes manifest 1.1 and refuses to publish a bad package."""

    def setUp(self):
        self.plan = build_scene_plan(read_json(ROOT / "examples/short-script-cooking.json"))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / "previews"

    def card(self, plan, scene, path, modules):
        path.write_bytes(PNG + f"poster {scene['index']}".encode())

    def encode(self, exe, args, cwd):
        if args[-1] == "preview.mp4":
            (cwd / "preview.mp4").write_bytes(b"fixture-video" * 100)
        elif args[-1].endswith(".mp4"):
            (cwd / args[-1]).write_bytes(b"segment")

    def render(self, card=None):
        with patch("vicekrack.preview.dependencies", return_value=(None, None, None, "ffmpeg")), \
                patch("vicekrack.preview.make_card", side_effect=card or self.card), \
                patch("vicekrack.preview.invoke", side_effect=self.encode), \
                patch("vicekrack.preview.uuid4", return_value=SimpleNamespace(hex="fixed")):
            return render_preview(self.plan, directory=self.folder)

    def test_manifest_binds_every_poster_and_the_video(self):
        result = self.render()
        folder = Path(result["manifest_file"]).parent
        manifest = read_json(Path(result["manifest_file"]))
        ab.validate_manifest(manifest)
        self.assertEqual(manifest["version"], "1.1")
        self.assertEqual((manifest["video_sha256"], manifest["video_bytes"]),
                         (sha(folder / "preview.mp4"), (folder / "preview.mp4").stat().st_size))
        for scene in manifest["scenes"]:
            poster = folder / scene["poster"]
            self.assertEqual((scene["poster_sha256"], scene["poster_bytes"]), (sha(poster), poster.stat().st_size))
        self.assertIsNone(ab.check_package(folder, manifest))
        self.assertNotIn("quality", json.dumps(manifest))           # manifests never refer to reports
        self.assertFalse(manifest["publishable"])

    def test_bad_package_is_never_published(self):
        def not_png(plan, scene, path, modules):
            path.write_bytes(b"not a png")
        with self.assertRaises(NetworkError) as error:
            self.render(not_png)
        self.assertEqual(error.exception.code, "invalid_render_output")
        self.assertEqual(list(self.folder.iterdir()), [])            # no package, no staging left behind


class PackageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        video = self.folder / "preview.mp4"
        video.write_bytes(b"v" * 200)
        self.manifest = {"contract": "preview_render", "version": "1.1", "plan_id": "a" * 64,
                         "input_sha256": "0" * 64, "preview_only": True, "publishable": False,
                         "source_blocked_for_production": False, "width": 1080, "height": 1920, "fps": 24,
                         "duration_seconds": 15, "audio_present": False, "video": "preview.mp4",
                         "video_sha256": sha(video), "video_bytes": 200, "limitations": [], "scenes": []}
        for index in (1, 2):
            poster = self.folder / f"scene-{index}.png"
            poster.write_bytes(PNG + bytes([index]) * 10)
            self.manifest["scenes"].append({"index": index, "method": "text_card", "start_seconds": (index - 1) * 7.5,
                                            "end_seconds": index * 7.5, "poster": poster.name,
                                            "poster_sha256": sha(poster), "poster_bytes": 18})

    def test_valid_package(self):
        self.assertIsNone(ab.check_package(self.folder, self.manifest))

    def test_changed_missing_and_unsafe_files(self):
        (self.folder / "scene-2.png").write_bytes(PNG + b"\x09" * 10)
        self.assertEqual(ab.check_package(self.folder, self.manifest), "poster_hash_mismatch")
        (self.folder / "scene-2.png").write_bytes(b"\x00" * 18)
        self.assertEqual(ab.check_package(self.folder, self.manifest), "poster_not_png")
        (self.folder / "scene-2.png").unlink()
        self.assertEqual(ab.check_package(self.folder, self.manifest), "poster_missing")
        traversal = deepcopy(self.manifest)
        traversal["video"] = "../preview.mp4"
        self.assertEqual(ab.check_package(self.folder, traversal), "manifest_invalid")
        (self.folder / "preview.mp4").write_bytes(b"w" * 200)
        self.assertEqual(ab.check_package(self.folder, self.manifest), "video_hash_mismatch")

    def test_malformed_hashes_and_versions_are_rejected(self):
        for mutate in (lambda m: m["scenes"][0].update(poster_sha256="ABC"),
                       lambda m: m["scenes"][0].update(poster_sha256="A" * 64),
                       lambda m: m["scenes"][0].pop("poster_sha256"),
                       lambda m: m["scenes"][1].update(poster="scene-1.png"),
                       lambda m: m.update(video_bytes=-1),
                       lambda m: m.update(version="1.2")):
            manifest = deepcopy(self.manifest)
            mutate(manifest)
            with self.assertRaises(NetworkError):
                ab.validate_manifest(manifest)
            self.assertEqual(ab.check_package(self.folder, manifest), "manifest_invalid")

    def test_legacy_manifest_is_readable_but_not_bound(self):
        legacy = deepcopy(self.manifest)
        legacy["version"] = "1.0"
        del legacy["video_bytes"]
        for scene in legacy["scenes"]:
            del scene["poster_sha256"], scene["poster_bytes"]
        self.assertIs(ab.validate_manifest(legacy), legacy)
        self.assertFalse(ab.posters_bound(legacy))
        self.assertEqual(ab.check_package(self.folder, legacy), "manifest_not_bound")
        mixed = deepcopy(legacy)
        mixed["scenes"][0]["poster_sha256"] = "0" * 64              # 1.0 cannot carry 1.1 fields
        with self.assertRaises(NetworkError):
            ab.validate_manifest(mixed)


class BindingTests(Step36Base):
    def test_successful_binding_records_exact_files_and_no_secrets(self):
        self.make()
        report, path = QualityChecker(root=self.root, clock=lambda: TIMES[1], prober=lambda p: dict(GOOD_MEDIA),
                                      poster_reader=lambda p: ((1080, 1920), [(89, 214, 193)] * 2)).run(self.pid)
        validate_report(report)
        self.assertEqual((report["version"], report["result"]), ("1.1", "pass"))
        binding = report["binding"]
        self.assertEqual((binding["status"], binding["integrity_only"]), ("bound", True))
        rows = {(r["role"], r["index"]): r for r in binding["artifacts"]}
        expected = {("production_state", None): self.folder / "state.json",
                    ("brief", None): self.path("brief", "brief_path"),
                    ("script", None): self.path("creator", "script_path"),
                    ("scene_plan", None): self.path("plan", "plan_path"),
                    ("preview_manifest", None): self.path("preview", "manifest_file"),
                    ("video", None): self.path("preview", "preview_file")}
        expected.update({("poster", n): self.manifest_dir() / f"scene-{n}.png" for n in range(1, 5)})
        for key, file in expected.items():
            self.assertEqual((rows[key]["sha256"], rows[key]["bytes"]), (sha(file), file.stat().st_size), key)
        record = rows[("verification_record", None)]
        self.assertEqual(record["sha256"], sha(self.root / f"runtime/verification/records/{record['id']}.json"))
        self.assertEqual({r["name"] for r in binding["configuration"]}, set(ab.CONFIG_NAMES))
        for row in binding["configuration"]:
            self.assertRegex(row["path"], r"^config/[A-Za-z0-9._-]+\.json$")
            self.assertEqual(row["sha256"], sha(ROOT / row["path"]))
        text = json.dumps(report)
        self.assertNotIn(str(self.root), text)                     # safe references only, never absolute paths
        self.assertNotIn("quality_report", {r["role"] for r in binding["artifacts"]})   # no circular hashing
        self.assertIn("does not verify facts", binding["note"])
        self.assertEqual(self.binding(report)["status"], "matching")
        self.assertEqual(sha(path), sha(self.root / "runtime/quality" / f"{report['report_id']}.json"))

    def test_quality_binding_cli_is_read_only(self):
        self.make()
        report, _ = self.quality(at=TIMES[1])
        before = tree(self.root)
        with patch("vicekrack.quality.QualityChecker.run", side_effect=AssertionError("ran")), \
                patch("vicekrack.quality.probe_media", side_effect=AssertionError("probed")):
            result = binding_of(report["report_id"], self.root)
        self.assertEqual((result["binding"]["status"], result["technical_result"], result["evidence_freshness_at_check"]),
                         ("matching", "pass", "pass"))
        self.assertEqual(tree(self.root), before)
        with patch("vicekrack.quality_cli.binding_of", lambda report_id: binding_of(report_id, self.root)), \
                patch("sys.argv", ["vicekrack", "quality-binding", report["report_id"]]), redirect_stdout(io.StringIO()) as out:
            from vicekrack.__main__ import main
            code = main()
        self.assertEqual((code, json.loads(out.getvalue())["binding"]["status"]), (0, "matching"))
        with self.assertRaises(NetworkError):
            binding_of("../etc/passwd", self.root)

    def test_changed_and_missing_posters(self):
        self.make()
        report, _ = self.quality(at=TIMES[1])
        (self.manifest_dir() / "scene-2.png").write_bytes(PNG + b"swapped")
        self.assertEqual(self.binding(report)["changed"], ["poster_2"])
        row = self.row()
        self.assertEqual((row["binding"], row["binding_changed"], row["result"]), ("changed", ["poster_2"], "pass"))
        posters = [s["poster"] for s in self.latest()["artifacts"]["plan"]["data"]["scenes"]]
        self.assertEqual((posters[1]["status"], posters[1]["reason"], posters[1]["media_id"]),
                         ("tampered", "poster_hash_mismatch", None))
        self.assertEqual(posters[0]["status"], "verified")
        later, _ = self.quality(at=TIMES[2])                        # a new check fails it
        self.assertEqual(later["result"], "fail")
        self.assertIn("poster_hash_mismatch", later["reasons"])
        (self.manifest_dir() / "scene-3.png").unlink()
        self.assertEqual(self.binding(report)["changed"], ["poster_2", "poster_3"])

    def test_altered_video_and_script(self):
        self.make()
        report, _ = self.quality(at=TIMES[1])
        video = self.path("preview", "preview_file")
        original = video.read_bytes()
        video.write_bytes(original + b"x")
        self.assertEqual(self.binding(report)["changed"], ["video"])
        self.assertEqual(self.row()["binding"], "changed")
        video.write_bytes(original)
        self.assertEqual(self.row()["binding"], "matching")          # back to the exact bound bytes
        script = self.path("creator", "script_path")
        script.write_text(script.read_text() + " ")
        self.assertIn("script", self.binding(report)["changed"])
        self.assertEqual(self.row()["binding"], "changed")

    def test_configuration_and_evidence_changes(self):
        self.make()
        report, _ = self.quality(at=TIMES[1])
        project = self.root / "project"
        for row in report["binding"]["configuration"]:
            (project / row["path"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / row["path"], project / row["path"])
        state = self.state()
        self.assertEqual(ab.verify_binding(report, state, self.folder, self.root, project)["status"], "matching")
        policy = next(r for r in report["binding"]["configuration"] if r["name"] == "verification_policy")
        (project / policy["path"]).write_text((project / policy["path"]).read_text() + "\n")
        self.assertEqual(ab.verify_binding(report, state, self.folder, self.root, project)["changed"],
                         ["config_verification_policy"])
        record = next(r for r in report["binding"]["artifacts"] if r["role"] == "verification_record")
        path = self.root / f"runtime/verification/records/{record['id']}.json"
        path.write_text(path.read_text() + " ")
        self.assertEqual(self.binding(report)["changed"], ["verification_record"])

    def test_change_during_inspection_is_never_reported_as_bound(self):
        self.make()
        script = self.path("creator", "script_path")
        poster = self.manifest_dir() / "scene-1.png"

        def meddling_prober(path):
            script.write_text(script.read_text() + " ")
            poster.write_bytes(PNG + b"replaced while inspecting")
            return dict(GOOD_MEDIA)
        report, _ = QualityChecker(root=self.root, clock=lambda: TIMES[1], prober=meddling_prober,
                                   poster_reader=lambda p: ((1080, 1920), [(89, 214, 193)] * 2)).run(self.pid)
        self.assertEqual(report["binding"]["status"], "changed_during_inspection")
        self.assertIn("changed_script", report["binding"]["reasons"])
        self.assertIn("artifacts_changed_during_inspection", report["reasons"])
        self.assertNotEqual(report["result"], "pass")
        row = self.row()
        self.assertEqual((row["binding"], row["binding_reasons"]), ("unavailable", ["not_bound_changed_during_inspection"]))

    def test_production_never_accepts_an_unverified_bound_package(self):
        class SwappingRenderer(BoundRenderer):
            def __call__(self, plan, **kwargs):
                result = super().__call__(plan, **kwargs)
                (Path(result["manifest_file"]).parent / "scene-4.png").write_bytes(PNG + b"swapped after hashing")
                return result
        result = self.pipeline(SwappingRenderer()).produce(self.run_id, self.record_id)
        self.pid = result["production_id"]
        state = ProductionStore(self.root).read(self.pid)
        preview = next(s for s in state["stages"] if s["name"] == "preview")
        self.assertNotEqual(result["status"], "completed")
        self.assertNotEqual(preview["status"], "completed")
        self.assertIn("invalid_render_output", json.dumps(state))

    def test_malformed_report_binding_is_rejected(self):
        self.make()
        report, _ = self.quality(at=TIMES[1])
        for mutate in (lambda r: r["binding"]["artifacts"][0].update(sha256="xyz"),
                       lambda r: r["binding"]["artifacts"][0].update(ref="/etc/passwd"),
                       lambda r: r["binding"]["artifacts"][0].update(ref="../state.json"),
                       lambda r: r["binding"]["configuration"][0].update(path="/home/user/policy.json"),
                       lambda r: r["binding"].update(status="trusted"),
                       lambda r: r.pop("binding"),
                       lambda r: r.update(version="1.0")):            # 1.0 must not carry a binding
            broken = deepcopy(report)
            mutate(broken)
            if broken.get("version") == "1.0" and "binding" not in broken:
                continue
            with self.assertRaises(NetworkError):
                validate_report(broken)
        folder = self.root / "runtime/quality"
        forged = deepcopy(report)
        forged["binding"]["artifacts"][0]["sha256"] = "Z" * 64
        (folder / f"{report['report_id']}.json").write_text(json.dumps(forged))
        quality = self.latest()["quality"]
        self.assertEqual(quality["reports"], [])
        self.assertEqual(quality["rejected_reports"], [{"report_id": report["report_id"], "code": "quality_report_invalid"}])
        with self.assertRaises(NetworkError):
            load_report(report["report_id"], self.root)


class LegacyTests(Step36Base):
    def test_legacy_package_and_report_stay_legacy(self):
        super(Step36Base, self).make()                              # Step 35 renderer: a 1.0 manifest
        manifest_path = self.path("preview", "manifest_file")
        self.assertEqual(read_json(manifest_path)["version"], "1.0")
        report, _ = self.quality(at=TIMES[1])
        self.assertEqual((report["binding"]["status"], report["result"]), ("bound", "pass"))
        consistency = next(c for c in report["checks"] if c["check_id"] == "manifest_consistency")
        self.assertFalse(consistency["details"]["posters_hash_bound"])
        legacy = {k: v for k, v in report.items() if k != "binding"}
        legacy.update(version="1.0", report_id="qr-" + "9" * 24)
        legacy_path = self.root / "runtime/quality" / f"{legacy['report_id']}.json"
        legacy_path.write_text(json.dumps(legacy))
        before = tree(self.root)
        doc = self.latest()
        posters = [s["poster"] for s in doc["artifacts"]["plan"]["data"]["scenes"]]
        self.assertEqual({p["status"] for p in posters}, {"not_hash_bound"})      # never upgraded
        self.assertEqual((doc["artifacts"]["preview"]["data"]["manifest_version"],
                          doc["artifacts"]["preview"]["data"]["posters_hash_bound"]), ("1.0", False))
        rows = {r["report_id"]: r for r in doc["quality"]["reports"]}
        self.assertEqual((rows[legacy["report_id"]]["binding"], rows[legacy["report_id"]]["report_version"]),
                         ("legacy_unverified", "1.0"))
        self.assertEqual(rows[report["report_id"]]["binding"], "matching")   # a new report still records poster bytes
        self.assertEqual(binding_of(legacy["report_id"], self.root)["binding"]["status"], "legacy_unverified")
        self.assertEqual(tree(self.root), before)                  # nothing rewritten, no attestation added
        self.assertNotIn("binding", read_json(legacy_path))
        self.assertEqual(read_json(manifest_path)["version"], "1.0")


class DashboardTests(Step36Base):
    def test_historical_views_withhold_later_reports(self):
        self.make()
        self.clock = TIMES[1]
        report, recorded = self.quality(record=True)
        before = content.content_at(recorded, 1, self.root)
        after = content.content_at(recorded, 2, self.root)
        self.assertNotIn(report["report_id"], json.dumps(before))
        self.assertEqual(after["quality"]["reports"][0]["binding"], "matching")
        self.assertFalse(after["restrictions"]["publishable"])

    def test_read_only_revalidation_and_three_separate_labels(self):
        _, recorded = self.make(record=True)
        report, _ = self.quality(at=TIMES[1])
        before = tree(self.root)
        for target in ("vicekrack.quality.QualityChecker.run", "vicekrack.quality.probe_media",
                       "vicekrack.production.ProductionStore.write", "vicekrack.production.ProductionStore.lock",
                       "vicekrack.preview.render_preview", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("must not run: " + target))
            guard.start()
            self.addCleanup(guard.stop)
        row = self.row()
        self.assertEqual((row["binding"], row["result"], row["evidence_freshness_at_check"]), ("matching", "pass", "pass"))
        self.assertIn(self.latest()["artifacts"]["verification"]["freshness_now"]["status"], ("fresh", "stale"))
        doc = self.latest()
        poster_id = doc["artifacts"]["plan"]["data"]["scenes"][0]["poster"]["media_id"]
        self.assertEqual(get(f"/api/content/media?production={self.pid}&id={poster_id}", self.root)[0], 200)
        (self.manifest_dir() / "scene-1.png").write_bytes(PNG + b"swapped after load")
        status, _, body = get(f"/api/content/media?production={self.pid}&id={poster_id}", self.root)
        self.assertNotEqual(status, 200)                            # re-validated when served
        self.assertEqual(get(f"/api/content/latest?timeline={self.pid}", self.root)[2]["quality"]["reports"][0]["binding"],
                         "changed")
        (self.manifest_dir() / "scene-1.png").write_bytes(PNG + b"fake poster scene-1.png")
        self.assertEqual(tree(self.root), before)
        self.assertEqual(get(f"/api/content/latest?timeline={self.pid}&run=1", self.root)[0], 400)


if __name__ == "__main__":
    unittest.main()
