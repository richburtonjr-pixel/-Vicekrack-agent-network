"""Step 19: dated official statements superseding older conflicting statements. Offline only."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from vicekrack import verification
from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT
from vicekrack.scout import build_candidate, keyword_patterns
from vicekrack.selection import empty_history, load_profile, rank_stories, select_brief, validate_profile
from vicekrack.story_brief import validate_story_brief
from vicekrack.verification import same_fact_conflict, supersessions, validate_record, verify_candidate
from vicekrack.verified_brief import build_verified_brief

RETRIEVED = "2026-10-04T12:00:00Z"
VERIFIED_AT = "2026-10-04T12:05:00Z"
NOW = "2026-10-04T12:30:00Z"
POLICY = {
    "policy_version": "1.0", "profile": "gta",
    "primary_sources": [{"source_id": "studio", "hosts": ["newsroom.example.com"]},
                        {"source_id": "parent", "hosts": ["ir.parent.example.com"]}],
    "secondary_sources": [{"source_id": "press-a", "hosts": ["a.example.org"]},
                          {"source_id": "press-b", "hosts": ["b.example.org"]}],
    "attribution_aliases": {"studio": "studio games"},
    "rules": {"min_independent_secondary_origins": 2, "match_threshold": 0.6, "near_duplicate_threshold": 0.85,
              "max_candidates": 500, "max_evidence_per_claim": 20, "max_record_age_days": 7},
    "brief_defaults": {"avoid": ["studio logos"], "disclosures": ["Fan-made."]},
}
POLICY_SHA = "1" * 64
SOURCES = {
    "studio": ("Studio Games", "newsroom.example.com", "official", "official_publisher"),
    "parent": ("Parent Interactive", "ir.parent.example.com", "official", "official_parent_company"),
    "press-a": ("Press A", "a.example.org", "press", "press"),
    "press-b": ("Press B", "b.example.org", "press", "press"),
}
_counter = iter(range(100_000))


def statement(date):
    return f"GTA VI launches on {date} for PlayStation 5 and Xbox Series X consoles worldwide."


def make(source_id, text, published, *, retrieved=RETRIEVED, title=None):
    """A Scout candidate. `published` is an RFC 822 date string or None (never the retrieval time)."""
    publisher, host, kind, category = SOURCES[source_id]
    source = {"source_id": source_id, "name": publisher, "publisher": publisher, "kind": kind,
              "category": category, "link_hosts": [host]}
    item = {"title": title or f"GTA VI news {next(_counter)}", "summary": text, "published": published or "",
            "link": f"https://{host}/{next(_counter)}"}
    candidate, reason = build_candidate(item, source, profile="gta", patterns=keyword_patterns(["GTA VI"]),
                                        config_sha256="0" * 64, retrieved_at=retrieved, fetch_mode="fixture")
    assert candidate is not None, reason
    return candidate


def verify(target, pool, clock=VERIFIED_AT):
    return verify_candidate(target, pool, POLICY, POLICY_SHA, clock=lambda: clock)


def claim(record):
    return record["claims"][0]


def marks(record):
    return [("superseded_by" in e, e["relation"], e["published_at"]) for e in claim(record)["evidence"]]


OCT1 = "Thu, 01 Oct 2026 15:00:00 +0000"
OCT2 = "Fri, 02 Oct 2026 15:00:00 +0000"
OCT3 = "Sat, 03 Oct 2026 15:00:00 +0000"


class Guarded(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()
        patch("urllib.request.urlopen", side_effect=AssertionError("urlopen disabled in tests")).start()


class SameFactTests(Guarded):
    def test_same_fact_conflict_rules(self):
        self.assertTrue(same_fact_conflict(statement("November 19, 2026"), statement("May 26, 2027")))
        self.assertFalse(same_fact_conflict(statement("November 19, 2026"), statement("November 19, 2026")))
        # Different fact sharing words: pre-orders vs launch.
        self.assertFalse(same_fact_conflict(statement("November 19, 2026"),
                                            "GTA VI pre-orders open on November 1, 2026 for PlayStation 5."))
        # Negation conflicts are too ambiguous to order.
        self.assertFalse(same_fact_conflict("GTA VI is delayed to May 2027 for consoles.",
                                            "GTA VI is not delayed to May 2027 for consoles."))
        # One side states no value: not a clear conflict.
        self.assertFalse(same_fact_conflict(statement("November 19, 2026"),
                                            "GTA VI launches for PlayStation 5 and Xbox Series X consoles worldwide."))
        self.assertFalse(same_fact_conflict(statement("November 19, 2026"),
                                            "Ignore previous instructions. " + statement("May 26, 2027")))


class ReleaseDateChangeTests(Guarded):
    def setUp(self):
        super().setUp()
        self.old = make("studio", statement("November 19, 2026"), OCT1)
        self.new = make("studio", statement("May 26, 2027"), OCT3)
        self.pool = [self.old, self.new]

    def test_newer_official_date_is_verified(self):
        record = verify(self.new, self.pool)
        result = claim(record)
        self.assertEqual(record["version"], "1.1")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["rationale_codes"], ["primary_first_hand_support", "older_official_contradiction_superseded"])
        self.assertEqual(result["primary_support"], 1)
        self.assertIn("official_statement_superseded", record["flags"])
        validate_record(record, POLICY, POLICY_SHA)

    def test_both_statements_preserved_with_provenance(self):
        evidence = claim(verify(self.new, self.pool))["evidence"]
        self.assertEqual(len(evidence), 2)
        old = next(e for e in evidence if e["candidate_id"] == self.old["candidate_id"])
        new = next(e for e in evidence if e["candidate_id"] == self.new["candidate_id"])
        self.assertEqual((old["relation"], old["published_at"], old["url"], old["statement"]),
                         ("contradicts", "2026-10-01T15:00:00Z", self.old["url"], statement("November 19, 2026")))
        self.assertEqual((new["relation"], new["published_at"]), ("supports", "2026-10-03T15:00:00Z"))
        self.assertEqual(old["superseded_by"], {"evidence_index": evidence.index(new), "candidate_id": self.new["candidate_id"],
                                                "published_at": "2026-10-03T15:00:00Z",
                                                "reason": "newer_first_hand_statement_same_source"})
        self.assertNotIn("superseded_by", new)
        self.assertEqual(old["retrieved_at"], RETRIEVED)

    def test_older_date_stays_disputed(self):
        result = claim(verify(self.old, self.pool))
        self.assertEqual(result["status"], "disputed")
        self.assertEqual(result["rationale_codes"],
                         ["primary_first_hand_support", "primary_contradiction", "official_support_superseded"])

    def test_order_independent(self):
        self.assertEqual(verify(self.new, self.pool), verify(self.new, list(reversed(self.pool))))

    def test_brief_cites_only_current_statement(self):
        brief = build_verified_brief([verify(self.new, self.pool)], POLICY, POLICY_SHA, clock=lambda: NOW)
        validate_story_brief(brief, require_verified_claims=True)
        self.assertEqual([s["url"] for s in brief["sources"]], [self.new["url"]])
        with self.assertRaises(NetworkError) as raised:
            build_verified_brief([verify(self.old, self.pool)], POLICY, POLICY_SHA, clock=lambda: NOW)
        self.assertEqual(raised.exception.code, "no_verified_claims")

    def test_superseded_support_never_cited(self):
        broad = make("studio", "GTA VI launches in 2026 for PlayStation 5 and Xbox Series X consoles worldwide.", OCT2)
        first = make("studio", statement("November 19, 2026"), OCT1)
        second = make("studio", statement("December 3, 2026"), OCT3)
        record = verify(broad, [broad, first, second])
        self.assertEqual(claim(record)["status"], "verified")
        superseded = [e["candidate_id"] for e in claim(record)["evidence"] if "superseded_by" in e]
        self.assertEqual(superseded, [first["candidate_id"]])
        brief = build_verified_brief([record], POLICY, POLICY_SHA, clock=lambda: NOW)
        self.assertNotIn(first["url"], [s["url"] for s in brief["sources"]])
        self.assertEqual(sorted(s["url"] for s in brief["sources"]), sorted([broad["url"], second["url"]]))


class RepeatedUpdateTests(Guarded):
    def test_three_official_dates(self):
        a = make("studio", statement("November 19, 2026"), OCT1)
        b = make("studio", statement("May 26, 2027"), OCT2)
        c = make("studio", statement("September 1, 2027"), OCT3)
        pool = [a, b, c]
        latest = claim(verify(c, pool))
        self.assertEqual(latest["status"], "verified")
        superseded = {e["candidate_id"]: e["superseded_by"]["candidate_id"] for e in latest["evidence"] if "superseded_by" in e}
        self.assertEqual(superseded, {a["candidate_id"]: c["candidate_id"], b["candidate_id"]: c["candidate_id"]})
        self.assertEqual(claim(verify(b, pool))["status"], "disputed")
        self.assertEqual(claim(verify(a, pool))["status"], "disputed")

    def test_restating_older_date_later_wins(self):
        a = make("studio", statement("November 19, 2026"), OCT1)
        b = make("studio", statement("May 26, 2027"), OCT2)
        a_again = make("studio", statement("November 19, 2026"), OCT3)
        pool = [a, b, a_again]
        self.assertEqual(claim(verify(a_again, pool))["status"], "verified")
        self.assertEqual(claim(verify(b, pool))["status"], "disputed")


class NoSupersessionTests(Guarded):
    def assert_still_disputed(self, old, new):
        record = verify(new, [old, new])
        self.assertEqual(claim(record)["status"], "disputed")
        self.assertFalse(any(flag for flag, _, _ in marks(record)))
        self.assertNotIn("official_statement_superseded", record["flags"])
        validate_record(record, POLICY, POLICY_SHA)

    def test_missing_dates(self):
        self.assert_still_disputed(make("studio", statement("November 19, 2026"), None),
                                   make("studio", statement("May 26, 2027"), OCT3))
        self.assert_still_disputed(make("studio", statement("November 19, 2026"), OCT1),
                                   make("studio", statement("May 26, 2027"), None))

    def test_collection_time_is_never_publication_time(self):
        old = make("studio", statement("November 19, 2026"), None, retrieved="2026-10-01T12:00:00Z")
        new = make("studio", statement("May 26, 2027"), None, retrieved="2026-10-04T12:00:00Z")
        self.assert_still_disputed(old, new)

    def test_equal_and_too_close_dates(self):
        self.assert_still_disputed(make("studio", statement("November 19, 2026"), OCT3),
                                   make("studio", statement("May 26, 2027"), OCT3))
        self.assert_still_disputed(make("studio", statement("November 19, 2026"), "Sat, 03 Oct 2026 15:00:00 +0000"),
                                   make("studio", statement("May 26, 2027"), "Sat, 03 Oct 2026 15:30:00 +0000"))

    def test_publication_after_collection_is_untrusted(self):
        self.assert_still_disputed(make("studio", statement("November 19, 2026"), OCT1),
                                   make("studio", statement("May 26, 2027"), "Mon, 05 Oct 2026 15:00:00 +0000"))

    def test_any_ambiguous_pair_blocks_the_source(self):
        a = make("studio", statement("November 19, 2026"), OCT1)
        b = make("studio", statement("May 26, 2027"), OCT3)
        undated = make("studio", statement("August 4, 2027"), None)
        record = verify(b, [a, b, undated])
        self.assertNotEqual(claim(record)["status"], "verified")
        self.assertFalse(any(flag for flag, _, _ in marks(record)))

    def test_different_official_sources_remain_disputed(self):
        self.assert_still_disputed(make("studio", statement("November 19, 2026"), OCT1),
                                   make("parent", statement("May 26, 2027"), OCT3))

    def test_unrelated_newer_statement_does_not_supersede(self):
        launch = make("studio", statement("November 19, 2026"), OCT1)
        preorder = make("studio", "GTA VI pre-orders open on November 1, 2026 for PlayStation 5 and Xbox Series X consoles worldwide.", OCT3)
        for target in (launch, preorder):
            record = verify(target, [launch, preorder])
            self.assertFalse(any(flag for flag, _, _ in marks(record)), target["title"])
            self.assertNotEqual(claim(record)["status"], "verified")

    def test_press_quoting_official_cannot_supersede(self):
        official = make("studio", statement("November 19, 2026"), OCT1)
        quote = make("press-a", "According to Studio Games, " + statement("May 26, 2027"), OCT3)
        record = verify(quote, [official, quote])
        self.assertEqual(claim(record)["status"], "rejected")
        self.assertFalse(any(flag for flag, _, _ in marks(record)))
        reported = make("studio", "GTA VI reportedly launches on May 26, 2027 for PlayStation 5 and Xbox Series X consoles worldwide.", OCT3)
        record = verify(reported, [official, reported])
        self.assertNotEqual(claim(record)["status"], "verified")
        self.assertFalse(any(flag for flag, _, _ in marks(record)))

    def test_newer_official_does_not_rescue_a_press_claim_with_other_facts(self):
        old = make("studio", statement("November 19, 2026"), OCT1)
        new = make("studio", statement("May 26, 2027"), OCT3)
        rumor = make("press-a", statement("March 3, 2027"), OCT3)
        self.assertEqual(claim(verify(rumor, [old, new, rumor]))["status"], "rejected")


class ReplayTests(Guarded):
    def setUp(self):
        super().setUp()
        self.old = make("studio", statement("November 19, 2026"), OCT1)
        self.new = make("studio", statement("May 26, 2027"), OCT3)
        self.record = verify(self.new, [self.old, self.new])

    def test_tampered_supersession_is_rejected(self):
        index = next(i for i, e in enumerate(claim(self.record)["evidence"]) if "superseded_by" in e)
        removed = deepcopy(self.record)
        del claim(removed)["evidence"][index]["superseded_by"]
        moved_date = deepcopy(self.record)
        claim(moved_date)["evidence"][index]["published_at"] = "2026-10-03T15:00:00Z"
        no_flag = deepcopy(self.record)
        no_flag["flags"] = []
        forged = verify(self.old, [self.old, self.new])
        other = 1 - next(i for i, e in enumerate(claim(forged)["evidence"]) if "superseded_by" in e)
        claim(forged)["evidence"][other]["superseded_by"] = dict(claim(forged)["evidence"][1 - other]["superseded_by"],
                                                                 evidence_index=1 - other)
        claim(forged)["status"] = "verified"
        for name, bad in (("removed", removed), ("moved_date", moved_date), ("no_flag", no_flag), ("forged", forged)):
            with self.subTest(name=name), self.assertRaises(NetworkError) as raised:
                validate_record(bad, POLICY, POLICY_SHA)
            self.assertEqual(raised.exception.code, "invalid_verification_record")

    def test_version_1_0_records_keep_step_17_rules(self):
        with patch.object(verification, "RECORD_VERSION", "1.0"), patch.object(verification, "supersessions", lambda e: {}):
            legacy = verify(self.new, [self.old, self.new])
        self.assertEqual((legacy["version"], claim(legacy)["status"]), ("1.0", "disputed"))
        validate_record(legacy, POLICY, POLICY_SHA)
        self.assertNotEqual(legacy["record_id"], self.record["record_id"])
        upgraded = deepcopy(legacy)
        claim(upgraded)["evidence"] = deepcopy(claim(self.record)["evidence"])
        with self.assertRaises(NetworkError):
            validate_record(upgraded, POLICY, POLICY_SHA)

    def test_supersessions_function_is_pure(self):
        evidence = deepcopy(claim(self.record)["evidence"])
        for item in evidence:
            item.pop("superseded_by", None)
        before = deepcopy(evidence)
        self.assertEqual(supersessions(evidence), supersessions(evidence))
        self.assertEqual(evidence, before)


class SelectionUpdateTests(Guarded):
    def setUp(self):
        super().setUp()
        self.profile, _ = load_profile("config/editorial.gta.json")
        self.old = make("studio", statement("November 19, 2026"), OCT1, title="GTA VI release date")
        self.new = make("studio", statement("May 26, 2027"), OCT3, title="GTA VI release date")
        self.history = self.history_after(self.old, [self.old], "2026-10-01T16:00:00Z", "2026-10-01T16:30:00Z")

    def history_after(self, candidate, pool, verified_at, now, history=None, profile=None):
        profile = profile or self.profile
        history = history or empty_history(profile)
        record = verify(candidate, pool, clock=verified_at)
        report = rank_stories([record], profile, "a" * 64, POLICY_SHA, history, now)
        _, entry = select_brief(record, report, profile=profile, profile_sha256="a" * 64, policy=POLICY,
                                policy_sha256=POLICY_SHA, history=history, now=now)
        return dict(history, entries=[entry, *history["entries"]])

    def rank(self, candidate, pool, history, profile=None):
        record = verify(candidate, pool)
        return record, rank_stories([record], profile or self.profile, "a" * 64, POLICY_SHA, history, NOW)["entries"][0]

    def test_genuine_official_update_is_selected(self):
        record, entry = self.rank(self.new, [self.old, self.new], self.history)
        self.assertEqual((entry["novelty"]["status"], entry["disposition"]), ("update", "select"))
        self.assertEqual(entry["novelty"]["related_selection_id"], self.history["entries"][0]["selection_id"])
        for reason in ("primary_verified", "supersedes_older_official", "update_to_previous"):
            self.assertIn(reason, entry["reasons"])
        report = rank_stories([record], self.profile, "a" * 64, POLICY_SHA, self.history, NOW)
        brief, _ = select_brief(record, report, profile=self.profile, profile_sha256="a" * 64, policy=POLICY,
                                policy_sha256=POLICY_SHA, history=self.history, now=NOW)
        validate_story_brief(brief, require_verified_claims=True)
        self.assertEqual(brief["editorial"]["novelty"], "update")
        self.assertIn("supersedes_older_official", brief["editorial"]["reasons"])
        self.assertEqual([c["text"] for c in brief["claims"]], [statement("May 26, 2027")])

    def test_update_allowed_even_above_duplicate_similarity(self):
        strict = deepcopy(self.profile)
        strict["history"].update(similar_threshold=0.4, duplicate_threshold=0.5)
        validate_profile(strict)
        history = self.history_after(self.old, [self.old], "2026-10-01T16:00:00Z", "2026-10-01T16:30:00Z", profile=strict)
        _, entry = self.rank(self.new, [self.old, self.new], history, strict)
        self.assertGreaterEqual(entry["novelty"]["similarity"], 0.5)
        self.assertEqual((entry["novelty"]["status"], entry["disposition"]), ("update", "select"))
        same_facts = make("studio", "GTA VI launches on May 26, 2027 for PlayStation 5 and Xbox Series X consoles.",
                          OCT3, title="GTA VI release date")
        history = self.history_after(self.new, [self.old, self.new], "2026-10-03T16:00:00Z", "2026-10-03T16:30:00Z",
                                     history=history, profile=strict)
        _, entry = self.rank(same_facts, [self.old, self.new, same_facts], history, strict)
        self.assertEqual((entry["novelty"]["status"], entry["disposition"]), ("duplicate", "reject"))

    def test_duplicate_protection_still_applies(self):
        history = self.history_after(self.new, [self.old, self.new], "2026-10-03T16:00:00Z", "2026-10-03T16:30:00Z",
                                     history=self.history)
        _, entry = self.rank(self.new, [self.old, self.new], history)
        self.assertEqual((entry["novelty"]["status"], entry["disposition"]), ("duplicate", "reject"))
        _, entry = self.rank(self.old, [self.old, self.new], history)
        self.assertEqual(entry["disposition"], "reject")

    def test_repeated_selection_updates(self):
        history = self.history_after(self.new, [self.old, self.new], "2026-10-03T16:00:00Z", "2026-10-03T16:30:00Z",
                                     history=self.history)
        latest = make("studio", statement("September 1, 2027"), "Sun, 04 Oct 2026 09:00:00 +0000", title="GTA VI release date")
        _, entry = self.rank(latest, [self.old, self.new, latest], history)
        self.assertEqual((entry["novelty"]["status"], entry["disposition"]), ("update", "select"))

    def test_other_gates_still_apply(self):
        undated = make("studio", statement("May 26, 2027"), None, title="GTA VI release date")
        _, entry = self.rank(undated, [self.old, undated], self.history)
        self.assertNotEqual(entry["disposition"], "select")
        self.assertIn("disputed_claims", entry["reasons"])
        other_source = make("parent", statement("May 26, 2027"), OCT3, title="GTA VI release date")
        _, entry = self.rank(other_source, [self.old, other_source], self.history)
        self.assertEqual(entry["disposition"], "reject")  # Disputed and nothing verified (Step 18 gate).
        self.assertEqual(entry["reasons"][:2], ["no_verified_claims", "disputed_claims"])
        stale_old = make("studio", statement("November 19, 2026"), "Tue, 01 Sep 2026 15:00:00 +0000")
        stale_new = make("studio", statement("May 26, 2027"), "Wed, 02 Sep 2026 15:00:00 +0000", title="GTA VI release date")
        _, entry = self.rank(stale_new, [stale_old, stale_new], self.history)
        self.assertEqual((entry["disposition"], entry["reasons"][0]), ("reject", "stale"))


if __name__ == "__main__":
    unittest.main()
