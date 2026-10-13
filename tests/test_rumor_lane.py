"""Step 49: the opt-in rumor lane. Offline only; mocked rendering unless RUN_LOCAL_RENDER_TESTS=1."""

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from test_quality import GOOD_MEDIA, ManifestRenderer, fake_poster
from test_selection import (
    NOW, POLICY, POLICY_SHA, PROFILE, PROFILE_SHA, make, official, record,
)
from vicekrack.creator import MockScriptDrafter, build_request, draft_short_script
from vicekrack.errors import NetworkError
from vicekrack.orchestrator import read_json
from vicekrack.production import Pipeline, ProductionStore
from vicekrack.quality import QualityChecker
from vicekrack.rumor import BANDS, band_label, check_hedging, is_hedged, lane_config
from vicekrack.scene_plan import build_scene_plan
from vicekrack.scout_cli import scout_once
from vicekrack.selection import (
    empty_history, load_profile, rank_stories, select_brief, story_features, validate_profile,
)
from vicekrack.selection_cli import load_history, select_stories
from vicekrack.story_brief import validate_story_brief
from vicekrack.verification_cli import verify_stored

RUMOR_PROFILE, RUMOR_SHA = load_profile("config/editorial.gta.rumors.json")
SINGLE = "GTA VI will reportedly feature a playable Vice City airport, insiders say."
MULTI_A = "Insiders say GTA VI trailer three arrives in December 2026 for console players."
MULTI_B = "Leakers claim GTA VI trailer three arrives in December 2026 for console players everywhere."


def single_report(**kwargs):
    return record(make("press-a", SINGLE, **kwargs))


def multiple_reports():
    a, b = make("press-a", MULTI_A), make("press-b", MULTI_B)
    return record(a, b)


def rank(records, profile=RUMOR_PROFILE, sha=RUMOR_SHA, history=None):
    return rank_stories(records, profile, sha, POLICY_SHA, history or empty_history(profile), NOW)


def entry_for(report, rec):
    return next(e for e in report["entries"] if e["record_id"] == rec["record_id"])


def brief_for(rec, report, profile=RUMOR_PROFILE, sha=RUMOR_SHA, history=None):
    return select_brief(rec, report, profile=profile, profile_sha256=sha, policy=POLICY, policy_sha256=POLICY_SHA,
                        history=history or empty_history(profile), now=NOW)


class ProfileTests(unittest.TestCase):
    def test_shipped_profiles(self):
        self.assertIsNone(lane_config(PROFILE))                   # The default GTA profile stays verified-only.
        self.assertIsNone(lane_config(load_profile("config/editorial.mock.json")[0]))
        self.assertTrue(lane_config(RUMOR_PROFILE)["allow_single_report"])
        self.assertTrue(lane_config(load_profile("config/editorial.mock.rumors.json")[0])["enabled"])

    def test_disclosure_must_start_with_its_band(self):
        for label in ("single_report", "multiple_reports"):
            profile = deepcopy(RUMOR_PROFILE)
            profile["rumor_lane"]["disclosures"][label] = "Unconfirmed, maybe."
            with self.assertRaises(NetworkError) as raised:
                validate_profile(profile)
            self.assertEqual(raised.exception.code, "invalid_editorial_profile")

    def test_schema_rejects_unknown_fields_and_bad_scores(self):
        for mutate in (lambda lane: lane.update(extra=True), lambda lane: lane.update(select_min_score=101),
                       lambda lane: lane.pop("angle")):
            profile = deepcopy(RUMOR_PROFILE)
            mutate(profile["rumor_lane"])
            with self.assertRaises(NetworkError):
                validate_profile(profile)


class SelectionTests(unittest.TestCase):
    def test_default_profile_is_unchanged(self):
        single, multi = single_report(), multiple_reports()
        report = rank([single, multi], PROFILE, PROFILE_SHA)
        self.assertEqual(entry_for(report, single)["disposition"], "reject")
        self.assertIn("no_verified_claims", entry_for(report, single)["reasons"])
        self.assertEqual(entry_for(report, multi)["disposition"], "hold")
        self.assertIn("corroborated_only", entry_for(report, multi)["reasons"])
        self.assertTrue(all("lane" not in e and "rumor_label" not in e for e in report["entries"]))

    def test_rumors_are_selected_with_their_label(self):
        single, multi = single_report(), multiple_reports()
        report = rank([single, multi])
        one, many = entry_for(report, single), entry_for(report, multi)
        self.assertEqual((one["disposition"], one["lane"], one["rumor_label"]), ("select", "rumor", "single_report"))
        self.assertEqual((many["disposition"], many["lane"], many["rumor_label"]), ("select", "rumor", "multiple_reports"))
        self.assertIn("rumor_single_report", one["reasons"])
        self.assertIn("rumor_multiple_reports", many["reasons"])
        self.assertNotIn("unconfirmed_rumor", one["reasons"])     # "insiders say" is the point of the lane.
        self.assertIn("RUMOR LANE", one["rationale"])
        self.assertGreater(many["score"], one["score"])           # Two outlets outrank one.
        self.assertEqual(one["verification"]["verified_claim_ids"], [])

    def test_verified_story_is_unaffected_by_the_lane(self):
        rec = official("Studio Games confirms GTA VI launches on 26 May 2026 for consoles.")
        plain, lane = entry_for(rank([rec], PROFILE, PROFILE_SHA), rec), entry_for(rank([rec]), rec)
        self.assertNotIn("lane", lane)
        self.assertEqual({k: v for k, v in lane.items() if k != "rationale"},
                         {k: v for k, v in plain.items() if k != "rationale"})

    def test_single_reports_need_the_profile_switch(self):
        profile = deepcopy(RUMOR_PROFILE)
        profile["rumor_lane"]["allow_single_report"] = False
        single, multi = single_report(), multiple_reports()
        report = rank([single, multi], profile, "2" * 64)
        self.assertEqual(entry_for(report, single)["disposition"], "reject")
        self.assertIn("no_verified_claims", entry_for(report, single)["reasons"])
        self.assertEqual(entry_for(report, multi)["rumor_label"], "multiple_reports")

    def test_disabled_lane_behaves_like_no_lane(self):
        profile = deepcopy(RUMOR_PROFILE)
        profile["rumor_lane"]["enabled"] = False
        single = single_report()
        self.assertEqual(rank([single], profile, "3" * 64)["entries"][0]["disposition"], "reject")

    def test_gates_still_apply(self):
        stale = single_report(published="2026-08-01T10:00:00Z")
        old = single_report(published="2026-09-20T10:00:00Z")
        report = rank([stale, old])
        self.assertEqual(entry_for(report, stale)["disposition"], "reject")
        self.assertIn("stale", entry_for(report, stale)["reasons"])
        self.assertEqual(entry_for(report, old)["disposition"], "hold")
        self.assertIn("not_recent", entry_for(report, old)["reasons"])
        offtopic = record(make("press-a", "A new racing game will reportedly add a city map next year.",
                               title="Racing game roundup",
                               keywords=("racing game",)))
        self.assertIn("not_relevant", entry_for(rank([offtopic]), offtopic)["reasons"])

    def test_score_threshold(self):
        profile = deepcopy(RUMOR_PROFILE)
        profile["rumor_lane"]["select_min_score"] = 95
        entry = rank([single_report()], profile, "4" * 64)["entries"][0]
        self.assertEqual(entry["disposition"], "reject")
        self.assertIn("below_rumor_score", entry["reasons"])

    def test_disputed_and_contradicted_claims_never_qualify(self):
        target = make("press-a", "GTA VI trailer three arrives in December 2026 for console players.")
        rebuttal = make("official", "GTA VI trailer three does not arrive in December 2026 for console players.")
        rec = record(target, rebuttal)
        self.assertIn(rec["claims"][0]["status"], ("rejected", "disputed"))
        self.assertNotEqual(entry_for(rank([rec]), rec)["disposition"], "select")
        self.assertNotIn("lane", entry_for(rank([rec]), rec))

    def test_rumor_is_not_made_twice(self):
        single = single_report()
        report = rank([single])
        _, entry = brief_for(single, report)
        history = dict(empty_history(RUMOR_PROFILE), entries=[entry])
        again = rank([single], history=history)["entries"][0]
        self.assertEqual(again["disposition"], "reject")
        self.assertIn("duplicate_of_previous", again["reasons"])
        self.assertTrue(entry["claim_fingerprints"])                # Rumor claims are fingerprinted, in their
        self.assertEqual(story_features(single)["claim_fingerprints"], [])   # own namespace (none outside the lane).
        self.assertEqual(len(story_features(single, ["k1"])["claim_fingerprints"]), 1)


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.single, self.multi = single_report(), multiple_reports()
        self.report = rank([self.single, self.multi])

    def test_rumor_brief(self):
        for rec, label in ((self.single, "single_report"), (self.multi, "multiple_reports")):
            brief, _ = brief_for(rec, self.report)
            self.assertEqual((brief["editorial"]["lane"], brief["editorial"]["rumor_label"]), ("rumor", label))
            self.assertEqual({c["status"] for c in brief["claims"]}, {"unverified"})
            self.assertTrue(brief["constraints"]["disclosures"][0].startswith(BANDS[label]))
            self.assertIn("Fan-made.", brief["constraints"]["disclosures"])
            self.assertTrue(any("unconfirmed" in a for a in brief["constraints"]["avoid"]))
            self.assertEqual(brief["angle"], RUMOR_PROFILE["rumor_lane"]["angle"])
            statuses = {link["verification_status"] for link in brief["verification"]["claims"]}
            self.assertEqual(statuses, {"insufficient_evidence"} if label == "single_report" else {"corroborated"})

    def test_validator_protects_the_label(self):
        brief, _ = brief_for(self.single, self.report)
        stripped = deepcopy(brief)
        stripped["constraints"]["disclosures"] = ["Fan-made."]
        upgraded = deepcopy(brief)
        upgraded["claims"][0]["status"] = "verified"
        unlabelled = deepcopy(brief)
        del unlabelled["editorial"]["lane"], unlabelled["editorial"]["rumor_label"]
        half = deepcopy(brief)
        del half["editorial"]["rumor_label"]
        for broken in (stripped, upgraded, unlabelled, half):
            with self.assertRaises(NetworkError) as raised:
                validate_story_brief(broken)
            self.assertEqual(raised.exception.code, "invalid_story_brief")

    def test_report_entry_must_still_select_now(self):
        made = rank([self.single])
        _, entry = brief_for(self.single, made)
        history = dict(empty_history(RUMOR_PROFILE), entries=[entry])
        with self.assertRaises(NetworkError) as raised:
            brief_for(self.single, made, history=history)
        self.assertEqual(raised.exception.code, "selection_changed")


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.single, other = single_report(), official("Studio Games confirms GTA VI launches on 26 May 2026.")
        report = rank([self.single, other])
        self.brief, _ = brief_for(self.single, report)
        self.plain, _ = brief_for(other, report)

    def test_request_carries_the_label_only_for_rumors(self):
        self.assertEqual(build_request(self.brief)["rumor"]["label"], "single_report")
        self.assertNotIn("rumor", build_request(self.plain))

    def test_mock_script_is_hedged_and_labelled(self):
        script = draft_short_script(self.brief, created_at=NOW)
        key = next(b for b in script["beats"] if b["beat"] == "key_info")
        self.assertIn("reportedly", key["narration"].lower())     # Already hedged: no extra prefix.
        self.assertFalse(key["narration"].startswith("Reportedly:"))
        bare = deepcopy(self.brief)
        bare["claims"][0]["text"] = "GTA VI will feature a playable Vice City airport next year."
        bare_key = next(b for b in draft_short_script(bare, created_at=NOW)["beats"] if b["beat"] == "key_info")
        self.assertTrue(bare_key["narration"].startswith("Reportedly:"))
        self.assertTrue(script["disclosures"][0].startswith("RUMOR - UNCONFIRMED"))
        check_hedging(self.brief, script)
        plan = build_scene_plan(script, draft=True)
        self.assertEqual((plan["mode"], band_label(plan, "DRAFT - NOT FOR PRODUCTION")), ("draft", "RUMOR - UNCONFIRMED"))

    def test_unhedged_script_is_refused(self):
        script = draft_short_script(self.brief, created_at=NOW)
        for beat in script["beats"]:
            if beat["claim_ids"]:
                beat["narration"] = "GTA VI has a playable Vice City airport."
        with self.assertRaises(NetworkError) as raised:
            check_hedging(self.brief, script)
        self.assertEqual(raised.exception.code, "rumor_not_labeled")
        check_hedging(self.plain, script)                           # Only rumor-lane briefs are checked.

    def test_hedge_words(self):
        for text in ("GTA VI reportedly adds X", "Rumor: X", "Not confirmed yet, but X", "Leakers claim X",
                     "According to insiders, X"):
            self.assertTrue(is_hedged(text), text)
        for text in ("GTA VI adds X", "Confirmed: X", None, ""):
            self.assertFalse(is_hedged(text), text)

    def test_band_label_needs_a_draft_and_a_band(self):
        plan = {"blocked_for_production": False, "script": {"disclosures": ["RUMOR - UNCONFIRMED. x"]}}
        self.assertEqual(band_label(plan, "LOCAL PREVIEW"), "LOCAL PREVIEW")
        plan = {"blocked_for_production": True, "script": {"disclosures": ["Fan-made."]}}
        self.assertEqual(band_label(plan, "DRAFT"), "DRAFT")
        plan["script"]["disclosures"].append("MULTIPLE REPORTS - UNCONFIRMED. x")
        self.assertEqual(band_label(plan, "DRAFT"), "MULTIPLE REPORTS - UNCONFIRMED")


class UnhedgedDrafter:
    def draft(self, *, request, model):
        draft = MockScriptDrafter().draft(request=request, model=None)
        for beat in draft["beats"]:
            if beat["claim_ids"]:
                beat["narration"] = "It is happening for sure."
        return draft


class ProductionTests(unittest.TestCase):
    PATHS = {"editorial_profile": "config/editorial.mock.rumors.json"}

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        scout_once("config/scout-sources.mock.json", root=self.root, clock=lambda: "2026-10-04T12:00:00Z")
        verify_stored([], "config/verification.mock.json", verify_all=True, root=self.root,
                      clock=lambda: "2026-10-04T12:30:00Z")
        report, _ = select_stories([], "config/verification.mock.json", self.PATHS["editorial_profile"],
                                   select_all=True, root=self.root, clock=lambda: "2026-10-04T13:00:00Z")
        self.run_id = report["selection_run_id"]
        rumor = next(e for e in report["entries"] if e.get("lane") == "rumor" and e["disposition"] == "select")
        self.record_id, self.label = rumor["record_id"], rumor["rumor_label"]

    def pipeline(self, drafter=None):
        return Pipeline(root=self.root, clock=lambda: "2026-10-04T13:10:00Z", renderer=ManifestRenderer(),
                        drafter=drafter)

    def test_rumor_needs_draft_preview_before_any_state(self):
        with self.assertRaises(NetworkError) as raised:
            self.pipeline().produce(self.run_id, self.record_id, paths=self.PATHS)
        self.assertEqual(raised.exception.code, "rumor_requires_draft_preview")
        self.assertEqual(ProductionStore(self.root).list_ids(), [])

    def test_rumor_production_is_a_labelled_reviewed_draft(self):
        result = self.pipeline().produce(self.run_id, self.record_id, paths=self.PATHS, allow_draft_preview=True)
        self.assertEqual(result["status"], "completed")
        store = ProductionStore(self.root)
        state, folder = store.read(result["production_id"]), store.folder(result["production_id"])
        artifacts = {s["name"]: s["artifacts"] for s in state["stages"]}
        brief = read_json(folder / artifacts["brief"]["brief_path"])
        plan = read_json(folder / artifacts["plan"]["plan_path"])
        self.assertEqual((brief["editorial"]["lane"], plan["mode"], artifacts["validate"]["draft"]),
                         ("rumor", "draft", True))
        self.assertEqual(band_label(plan, "DRAFT"), BANDS[self.label])
        profile, _ = load_profile(self.PATHS["editorial_profile"])
        entry = next(e for e in load_history(profile, self.root)[0]["entries"]
                     if e.get("production_id") == result["production_id"])
        self.assertTrue(entry["claim_fingerprints"])
        self.assertEqual(entry["state"], "produced")

        checker = QualityChecker(root=self.root, clock=lambda: "2026-10-04T13:20:00Z",
                                 prober=lambda path: dict(GOOD_MEDIA), poster_reader=fake_poster)
        report, _ = checker.run(result["production_id"])
        draft = next(c for c in report["checks"] if c["check_id"] == "draft_restrictions")
        self.assertEqual(report["result"], "needs_review")
        self.assertEqual(draft["status"], "needs_review")
        self.assertIn("rumor_content", draft["reasons"])
        self.assertEqual(draft["details"]["rumor_label"], self.label)

    def test_unhedged_script_stops_before_planning(self):
        result = self.pipeline(UnhedgedDrafter()).produce(self.run_id, self.record_id, paths=self.PATHS,
                                                          allow_draft_preview=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], {"stage": "validate", "code": "rumor_not_labeled"})


@unittest.skipUnless(os.environ.get("RUN_LOCAL_RENDER_TESTS") == "1", "local encoder test")
class RenderTests(unittest.TestCase):
    def test_rumor_labels_fit_the_real_preview(self):
        from vicekrack.preview import render_preview
        for rec in (single_report(), multiple_reports()):
            report = rank([rec])
            brief, _ = brief_for(rec, report)
            plan = build_scene_plan(draft_short_script(brief, created_at=NOW), draft=True)
            with tempfile.TemporaryDirectory() as folder:
                result = render_preview(plan, allow_draft=True, directory=folder)
                manifest = json.loads(Path(result["manifest_file"]).read_text())
                self.assertEqual((manifest["source_blocked_for_production"], manifest["publishable"]), (True, False))


if __name__ == "__main__":
    unittest.main()
