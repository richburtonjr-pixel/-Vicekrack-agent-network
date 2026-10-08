"""Step 37: explicit human review decisions for content previews.

Local fixtures only (mocked Creator, renderer, media probe and poster reader); no network,
no credits. The evidence age is pinned through `vicekrack.review._clock_now`. Expected
hashes and digests are computed here from the saved files, independently of the module.
"""

import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_artifact_binding import BoundRenderer, Step36Base
from test_events import tree
from test_hq_content import GOOD_MEDIA, MARKUP, PNG, TIMES, get
from vicekrack import production, review
from vicekrack.errors import NetworkError
from vicekrack.hq import content
from vicekrack.production import ProductionStore
from vicekrack.quality import ProbeUnavailable, QualityChecker
from vicekrack.review import ReviewRecorder, history, inspect, reviewable, utc, validate_review

FRESH = utc(TIMES[1])                              # verification record from 12:30, policy limit 7 days
STALE = utc("2026-10-20T00:00:00Z")
BEFORE_EVIDENCE = utc("2026-10-04T12:00:00Z")      # freshness cannot be established (record in the future)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_digest(report):
    return hashlib.sha256(json.dumps(report["binding"], sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


class ReviewBase(Step36Base):
    def setUp(self):
        super().setUp()
        self.now = FRESH
        clock = patch("vicekrack.review._clock_now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def check(self, prober=None, at=TIMES[1], record=False):
        self.clock = at
        events = self.events("quality") if record else None
        report, _ = QualityChecker(root=self.root, clock=lambda: self.clock, prober=prober or (lambda p: dict(GOOD_MEDIA)),
                                   poster_reader=lambda p: ((1080, 1920), [(89, 214, 193)] * 2), events=events).run(self.pid)
        if events is not None:
            events.close("completed")
        return report, (events.recorder.timeline_id if events else None)

    def make_draft(self):
        real = production.select_brief

        def corroborated_brief(*args, **kw):
            brief, entry = real(*args, **kw)
            extra = dict(brief["claims"][0], claim_id="c2", status="unverified",
                         text=brief["claims"][0]["text"].rstrip(".") + " according to early reports.")
            brief["claims"].append(extra)
            brief["verification"]["claims"].append(dict(brief["verification"]["claims"][0], claim_id="c2",
                                                        verification_status="corroborated"))
            return brief, entry
        with patch.object(production, "select_brief", corroborated_brief):
            result = self.pipeline(BoundRenderer()).produce(self.run_id, self.record_id, allow_draft_preview=True)
        self.pid = result["production_id"]
        self.folder = ProductionStore(self.root).folder(self.pid)

    def decide(self, report, decision="approved_for_preview", at=TIMES[2], digest=None, **options):
        options.setdefault("reviewer", "Rich (editor)")
        return ReviewRecorder(self.root, clock=lambda: at).record(
            self.pid, report["report_id"], decision=decision, binding=digest or canonical_digest(report)[:16], **options)

    def refused(self, code, report, **options):
        with self.assertRaises(NetworkError) as error:
            self.decide(report, **options)
        self.assertEqual(error.exception.code, code)
        return error.exception

    def reviews_dir(self):
        return self.root / "runtime/reviews" / self.pid

    def saved(self):
        return sorted(p.name for p in self.reviews_dir().iterdir()) if self.reviews_dir().exists() else []


class DecisionTests(ReviewBase):
    def test_each_decision_is_recorded_with_its_binding(self):
        self.make()
        report, _ = self.check()
        report_file = self.root / "runtime/quality" / f"{report['report_id']}.json"
        before = sha(report_file)
        first, path = self.decide(report, decision="changes_requested", notes="Scene 2 needs a calmer caption.")
        self.assertEqual(path.name, f"000001-{first['review_id']}.json")
        validate_review(json.loads(path.read_text()))
        self.assertEqual(first["quality_report"], {"report_id": report["report_id"], "sha256": before,
                                                   "result": "pass", "checked_at": report["checked_at"]})
        self.assertEqual(first["binding_digest"], canonical_digest(report))
        self.assertEqual(first["reviewer"], {"label": "Rich (editor)", "authenticated": False})
        self.assertEqual((first["recorded_at"], first["supersedes"], first["sequence"]), (TIMES[2], None, 1))
        self.assertEqual(first["scope"]["publishable"], False)
        self.assertTrue(first["scope"]["technical_findings_unchanged"])
        second, _ = self.decide(report, decision="rejected", at=TIMES[3], supersedes=first["review_id"])
        third, _ = self.decide(report, decision="approved_for_preview", at=TIMES[3], supersedes=second["review_id"])
        self.assertEqual((second["sequence"], third["sequence"], third["supersedes"]), (2, 3, second["review_id"]))
        self.assertEqual(sha(report_file), before)                   # technical findings are never changed
        doc = history(self.pid, self.root)
        self.assertEqual([r["decision"] for r in doc["reviews"]], ["approved_for_preview", "rejected", "changes_requested"])
        self.assertEqual(doc["summary"]["current_preview_approval"], True)
        self.assertEqual(ProductionStore(self.root).read(self.pid)["result"]["publishable"], False)
        self.assertEqual(inspect(self.pid, third["review_id"], self.root)["now"]["applicability"], "current")
        self.assertNotIn(str(self.root), path.read_text())

    def test_inputs_are_bounded_and_secrets_rejected(self):
        self.make()
        report, _ = self.check()
        self.refused("invalid_reviewer_label", report, reviewer="<script>alert(1)</script>")
        self.refused("invalid_reviewer_label", report, reviewer="")
        self.refused("invalid_notes", report, notes="x" * 2001)
        self.refused("invalid_notes", report, notes="bell \x07 inside")
        self.refused("invalid_decision", report, decision="approved_for_publishing")
        self.refused("invalid_acknowledgment", report, acknowledgments=["rights_cleared"])
        self.refused("binding_digest_required", report, digest="abc")
        self.refused("binding_digest_mismatch", report, digest="0" * 16)
        error = self.refused("sensitive_state", report, notes="key sk-" + "A" * 30)
        self.assertNotIn("sk-", str(error))                          # errors never repeat the note
        self.assertEqual(self.saved(), [])
        with self.assertRaises(NetworkError) as other:
            ReviewRecorder(self.root).record("prod-" + "0" * 24, report["report_id"], decision="rejected",
                                             reviewer="Rich", binding=canonical_digest(report))
        self.assertIn(other.exception.code, ("production_not_found", "invalid_production_id"))

    def test_notes_keep_markup_as_text(self):
        self.make()
        report, _ = self.check()
        record, path = self.decide(report, decision="changes_requested", notes=MARKUP + "\nsecond line")
        self.assertEqual(record["notes"], MARKUP + "\nsecond line")
        self.assertEqual(json.loads(path.read_text())["notes"], MARKUP + "\nsecond line")


class GateTests(ReviewBase):
    def test_binding_must_match(self):
        self.make()
        report, _ = self.check()
        script = self.path("creator", "script_path")
        script.write_text(script.read_text() + " ")
        for decision in ("approved_for_preview", "changes_requested", "rejected"):
            self.refused("binding_not_matching", report, decision=decision)
        legacy = dict(report, version="1.0", report_id="qr-" + "9" * 24)
        del legacy["binding"]
        (self.root / "runtime/quality" / f"{legacy['report_id']}.json").write_text(json.dumps(legacy))
        with self.assertRaises(NetworkError) as error:
            ReviewRecorder(self.root).record(self.pid, legacy["report_id"], decision="rejected", reviewer="Rich",
                                             binding="0" * 64)
        self.assertEqual(error.exception.code, "binding_not_matching")   # legacy reports are never reviewable
        self.assertEqual(self.saved(), [])

    def test_failed_report_cannot_be_approved(self):
        self.make()
        (self.manifest_dir() / "scene-2.png").write_bytes(PNG + b"swapped")
        report, _ = self.check()                                     # bound to these bytes, technical result fail
        self.assertEqual((report["result"], report["binding"]["status"]), ("fail", "bound"))
        self.refused("approval_blocked_technical_fail", report)
        record, _ = self.decide(report, decision="changes_requested")
        self.assertEqual(record["conditions"]["technical_result"], "fail")
        self.assertEqual(reviewable(self.pid, self.root, FRESH)[0]["decisions_allowed"], ["changes_requested", "rejected"])

    def test_unknown_evidence_freshness_refuses_approval_only(self):
        self.make()
        report, _ = self.check()
        self.now = BEFORE_EVIDENCE
        self.refused("approval_blocked_evidence_unknown", report)
        record, _ = self.decide(report, decision="rejected")
        self.assertEqual(record["conditions"]["evidence_now"]["status"], "unavailable")

    def test_draft_and_needs_review_require_acknowledgment(self):
        self.make_draft()
        # The draft fixture's brief disagrees with its record (provenance fails, see test_quality.DraftTests);
        # pass that one check here so the draft gate itself can be exercised on a needs_review report.
        from vicekrack.quality import Check
        with patch("vicekrack.quality.QualityChecker._check_provenance", lambda checker: Check("provenance")):
            report, _ = QualityChecker(root=self.root, clock=lambda: TIMES[1], prober=lambda p: dict(GOOD_MEDIA),
                                       poster_reader=lambda p: ((1080, 1920), [(255, 190, 85)] * 2)).run(self.pid)
        self.assertEqual(report["result"], "needs_review")
        required = reviewable(self.pid, self.root, FRESH)[0]["applicable_acknowledgments"]
        self.assertEqual(required, ["needs_review_result", "draft_restrictions"])
        error = self.refused("acknowledgment_required", report)
        self.assertIn("draft_restrictions", str(error))
        self.refused("acknowledgment_required", report, acknowledgments=["needs_review_result"])
        self.refused("acknowledgment_not_applicable", report, acknowledgments=required + ["stale_evidence"])
        record, _ = self.decide(report, acknowledgments=required)
        self.assertEqual((record["acknowledgments"], record["conditions"]["draft"]), (required, True))

    def test_unavailable_checks_and_stale_evidence_require_acknowledgment(self):
        self.make()

        def unavailable(path):
            raise ProbeUnavailable()
        report, _ = self.check(prober=unavailable)
        self.now = STALE
        found = reviewable(self.pid, self.root, STALE)[0]
        self.assertIn("video", found["conditions"]["unavailable_checks"])
        self.assertEqual(found["applicable_acknowledgments"], ["needs_review_result", "unavailable_checks", "stale_evidence"])
        self.refused("acknowledgment_required", report, acknowledgments=["needs_review_result", "unavailable_checks"])
        record, _ = self.decide(report, acknowledgments=found["applicable_acknowledgments"])
        self.assertEqual(record["conditions"]["evidence_now"]["status"], "stale")
        self.assertTrue(history(self.pid, self.root)["summary"]["current_preview_approval"])   # staleness acknowledged


class InvalidationTests(ReviewBase):
    def test_changed_or_missing_artifacts_invalidate_without_editing_history(self):
        self.make()
        report, _ = self.check()
        record, path = self.decide(report)
        before = sha(path)
        poster = self.manifest_dir() / "scene-3.png"
        original = poster.read_bytes()
        poster.write_bytes(PNG + b"changed later")
        row = history(self.pid, self.root)["reviews"][0]
        self.assertEqual((row["applicability"], row["artifact_binding_now"], row["current_preview_approval"]),
                         ("invalidated", "changed", False))
        self.assertIn("changed_poster_3", row["reasons"])
        poster.unlink()
        self.assertEqual(history(self.pid, self.root)["reviews"][0]["applicability"], "invalidated")
        poster.write_bytes(original)
        self.assertEqual(history(self.pid, self.root)["reviews"][0]["applicability"], "current")
        self.assertEqual(sha(path), before)                          # the record itself never changes

    def test_changed_or_missing_report_invalidates(self):
        self.make()
        report, _ = self.check()
        self.decide(report)
        report_file = self.root / "runtime/quality" / f"{report['report_id']}.json"
        original = report_file.read_text()
        report_file.write_text(json.dumps(json.loads(original), indent=4))   # same content, different bytes
        row = history(self.pid, self.root)["reviews"][0]
        self.assertEqual((row["applicability"], row["reasons"][0]), ("invalidated", "quality_report_changed_since_review"))
        report_file.unlink()
        row = history(self.pid, self.root)["reviews"][0]
        self.assertEqual((row["applicability"], row["artifact_binding_now"], row["current_preview_approval"]),
                         ("invalidated", "unavailable", False))

    def test_stale_evidence_is_separate_from_artifact_matching(self):
        self.make()
        report, _ = self.check()
        self.decide(report)
        self.now = STALE
        doc = history(self.pid, self.root)
        row = doc["reviews"][0]
        self.assertEqual((row["applicability"], row["artifact_binding_now"]), ("current", "matching"))
        self.assertEqual((row["current_preview_approval"], doc["evidence_now"]["status"]), (False, "stale"))
        self.assertIn("evidence_became_stale_since_review", row["reasons"])
        self.now = BEFORE_EVIDENCE
        self.assertIn("evidence_freshness_not_established_now", history(self.pid, self.root)["reviews"][0]["reasons"])


class HistoryTests(ReviewBase):
    def test_supersession_is_explicit_and_preserves_every_record(self):
        self.make()
        report, _ = self.check()
        first, first_path = self.decide(report, decision="changes_requested")
        before = sha(first_path)
        self.refused("supersedes_required", report)
        self.refused("review_conflict", report, supersedes="rev-" + "0" * 24)
        second, _ = self.decide(report, supersedes=first["review_id"])
        rows = {r["review_id"]: r for r in history(self.pid, self.root)["reviews"]}
        self.assertEqual((rows[first["review_id"]]["applicability"], rows[first["review_id"]]["superseded_by"]),
                         ("superseded", second["review_id"]))
        self.assertEqual(rows[second["review_id"]]["applicability"], "current")
        self.assertEqual(sha(first_path), before)
        self.assertEqual(len(self.saved()), 2)

    def test_concurrent_decisions_are_never_lost(self):
        self.make()
        report, _ = self.check()
        first, _ = self.decide(report, decision="changes_requested")
        # Two reviewers both read history (latest = first) and decide; the second is refused, not merged away.
        winner, _ = self.decide(report, decision="rejected", supersedes=first["review_id"])
        self.refused("review_conflict", report, supersedes=first["review_id"])
        # A writer that lands between validation and saving is detected too.
        store = ProductionStore(self.root)

        def sneak_in():
            other = dict(winner, review_id="rev-" + "e" * 24, sequence=3, supersedes=winner["review_id"])
            (self.reviews_dir() / f"000003-{other['review_id']}.json").write_text(json.dumps(other))
        with patch("vicekrack.review._between_checks", side_effect=sneak_in):
            self.refused("review_conflict", report, supersedes=winner["review_id"])
        self.assertEqual(len(self.saved()), 3)                       # the sneaked record is kept, nothing overwritten
        with store.lock(self.pid):                                   # a running check or resume holds the lock
            self.refused("production_locked", report, supersedes="rev-" + "e" * 24)

    def test_changes_during_review_are_refused(self):
        self.make()
        report, _ = self.check()
        script = self.path("creator", "script_path")
        with patch("vicekrack.review._between_checks", side_effect=lambda: script.write_text(script.read_text() + " ")):
            self.refused("artifacts_changed_during_review", report)
        report_file = self.root / "runtime/quality" / f"{report['report_id']}.json"
        script.write_text(script.read_text()[:-1])
        with patch("vicekrack.review._between_checks",
                   side_effect=lambda: report_file.write_text(report_file.read_text() + "\n")):
            self.refused("artifacts_changed_during_review", report)
        report_file.write_text(report_file.read_text()[:-1])

        def evidence_turns_stale():
            self.now = STALE
        with patch("vicekrack.review._between_checks", side_effect=evidence_turns_stale):
            self.refused("artifacts_changed_during_review", report)
        self.assertEqual(self.saved(), [])

    def test_corrupted_records_block_current_status_and_new_decisions(self):
        self.make()
        report, _ = self.check()
        first, path = self.decide(report)
        path.write_text(path.read_text()[:-20])
        doc = history(self.pid, self.root)
        self.assertEqual((doc["status"], doc["summary"]["current_preview_approval"]), ("history_corrupted", False))
        self.assertEqual(doc["corrupted"], [{"name": path.name, "code": "review_record_unreadable_or_invalid"}])
        self.refused("review_history_corrupted", report, supersedes=first["review_id"])
        path.unlink()
        good, good_path = self.decide(report)
        renamed = self.reviews_dir() / f"000002-{good['review_id']}.json"
        good_path.rename(renamed)
        self.assertEqual(history(self.pid, self.root)["corrupted"][0]["code"], "review_record_does_not_match_its_file")
        renamed.rename(good_path)
        forged = dict(good, review_id="rev-" + "f" * 24, sequence=2, supersedes="rev-" + "1" * 24)
        (self.reviews_dir() / f"000002-{forged['review_id']}.json").write_text(json.dumps(forged))
        self.assertEqual(history(self.pid, self.root)["corrupted"][0]["code"], "review_history_chain_broken")
        (self.reviews_dir() / "notes.txt").write_text("stray")
        codes = {c["code"] for c in history(self.pid, self.root)["corrupted"]}
        self.assertIn("unexpected_file_in_review_history", codes)

    def test_interrupted_writes_leave_no_record(self):
        self.make()
        report, _ = self.check()
        with patch("vicekrack.scout_cli.os.link", side_effect=OSError("disk full")):
            self.refused("review_write_failed", report)
        self.assertEqual(self.saved(), [])                           # no record, temporary file removed
        (self.reviews_dir() / "tmpabc123.tmp").write_text('{"half": ')   # what a crash could leave behind
        self.assertEqual(history(self.pid, self.root)["status"], "none_saved")
        record, _ = self.decide(report)
        self.assertEqual(history(self.pid, self.root)["summary"]["latest_review_id"], record["review_id"])

    def test_cli_record_list_inspect(self):
        self.make()
        report, _ = self.check()
        root = self.root
        wrappers = {"vicekrack.review_cli.ReviewRecorder": lambda: ReviewRecorder(root, clock=lambda: TIMES[2]),
                    "vicekrack.review_cli.history": lambda pid: history(pid, root),
                    "vicekrack.review_cli.reviewable": lambda pid: reviewable(pid, root),
                    "vicekrack.review_cli.inspect": lambda pid, rid: inspect(pid, rid, root)}
        for target, replacement in wrappers.items():
            p = patch(target, replacement)
            p.start()
            self.addCleanup(p.stop)
        from vicekrack.__main__ import main

        def run(*argv):
            with patch("sys.argv", ["vicekrack", *argv]), redirect_stdout(io.StringIO()) as out:
                code = main()
            return code, json.loads(out.getvalue())
        code, listed = run("review-list", self.pid)
        self.assertEqual((code, listed["status"]), (0, "none_saved"))
        digest = listed["reviewable_reports"][0]["binding_digest"]
        notes = self.root / "notes.txt"
        notes.write_text("Looks right.\nKeep the watermark.", encoding="utf-8")
        code, saved = run("review-record", self.pid, "--report", report["report_id"], "--binding", digest[:12],
                          "--decision", "approved_for_preview", "--reviewer", "Rich", "--notes-file", str(notes))
        self.assertEqual((code, saved["decision"], saved["scope"]["publishable"]), (0, "approved_for_preview", False))
        code, shown = run("review-inspect", self.pid, saved["review_id"])
        self.assertEqual((code, shown["record"]["notes"], shown["now"]["applicability"]),
                         (0, "Looks right.\nKeep the watermark.", "current"))
        code, refused = run("review-record", self.pid, "--report", report["report_id"], "--binding", digest,
                            "--decision", "rejected", "--reviewer", "Rich")
        self.assertEqual((code, refused["error"]["code"]), (1, "supersedes_required"))


class DeskTests(ReviewBase):
    def test_latest_shows_history_applicability_and_escaped_notes(self):
        self.make()
        report, _ = self.check()
        first, _ = self.decide(report, decision="changes_requested", notes=MARKUP)
        second, _ = self.decide(report, supersedes=first["review_id"], reviewer="Second reviewer")
        reviews = self.latest()["reviews"]
        self.assertEqual(reviews["status"], "available")
        self.assertEqual([r["review_id"] for r in reviews["reviews"]], [second["review_id"], first["review_id"]])
        self.assertEqual(reviews["reviews"][1]["notes"], MARKUP)    # data, never markup (rendered with textContent)
        self.assertEqual(reviews["reviews"][1]["applicability"], "superseded")
        self.assertEqual((reviews["summary"]["current_preview_approval"], reviews["reviews"][0]["reviewer_label"]),
                         (True, "Second reviewer"))
        self.assertFalse(reviews["reviews"][0]["reviewer_authenticated"])
        self.assertFalse(self.latest()["restrictions"]["publishable"])
        self.assertEqual(content.content_latest("demo")["reviews"]["status"], "not_in_demo")

    def test_replay_shows_only_reviews_saved_by_the_position(self):
        _, production_timeline = self.make(record=True)
        report, first_check = self.check(record=True, at=TIMES[1])
        record, _ = self.decide(report, at=TIMES[2])
        positions = content.content_latest(first_check, self.root)["timeline"]["event_count"]
        for position in range(positions + 1):
            doc = content.content_at(first_check, position, self.root)
            self.assertNotIn(record["review_id"], json.dumps(doc))
            self.assertIn(doc["reviews"]["status"], ("not_yet", "unavailable"))
        _, later_check = self.check(record=True, at=TIMES[3])        # a timeline recorded after the decision
        doc = content.content_at(later_check, 1, self.root)
        row = doc["reviews"]["reviews"][0]
        self.assertEqual((row["review_id"], row["applicability"], row["current_preview_approval"]),
                         (record["review_id"], "not_evaluated_at_position", False))
        reconstructed = content.content_at(self.pid, 1, self.root)
        self.assertEqual(reconstructed["reviews"]["status"], "unavailable")
        self.assertNotIn(record["review_id"], json.dumps(reconstructed))
        (self.reviews_dir() / f"000001-{record['review_id']}.json").write_text("{}")
        self.assertEqual(content.content_at(later_check, 1, self.root)["reviews"]["reasons"], ["review_history_corrupted"])
        self.assertIsNotNone(production_timeline)

    def test_desk_is_read_only_and_records_nothing(self):
        _, recorded = self.make(record=True)
        report, _ = self.check()
        self.decide(report, notes="private note")
        before = tree(self.root)
        for target in ("vicekrack.review.ReviewRecorder.record", "vicekrack.review._publish",
                       "vicekrack.quality.QualityChecker.run", "vicekrack.production.ProductionStore.write",
                       "vicekrack.production.ProductionStore.lock", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("must not run: " + target))
            guard.start()
            self.addCleanup(guard.stop)
        for timeline in (self.pid, recorded, "demo"):
            status, _, latest = get(f"/api/content/latest?timeline={timeline}", self.root)
            self.assertEqual(status, 200)
            for position in range(latest["timeline"]["event_count"] + 1):
                self.assertEqual(get(f"/api/content/at?timeline={timeline}&position={position}", self.root)[0], 200)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertNotEqual(get(f"/api/content/latest?timeline={self.pid}", self.root, method=method)[0], 200)
        self.assertNotEqual(get(f"/api/content/review?timeline={self.pid}", self.root)[0], 200)
        self.assertEqual(tree(self.root), before)


if __name__ == "__main__":
    unittest.main()
