"""Short Script contract: schema, format template, fallbacks, references, and safety."""

import json
import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from jsonschema import Draft202012Validator

from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.short_script import (
    FORMATS, LOCAL_METHODS, MAX_WORDS_PER_SECOND, SCHEMA_PATH, VISUAL_METHODS,
    max_words, validate_short_script,
)


def cooking_script():
    """A non-GTA script proving the contract is subject-neutral."""
    def visual(preferred, fallbacks, sources=(), prompt=None):
        return {"description": "Scene visual.", "preferred_method": preferred,
                "fallback_methods": list(fallbacks), "source_ids": list(sources),
                "generation_prompt": prompt, "avoid": []}
    return {
        "contract": "short_script", "version": "1.0", "script_id": "roast-potatoes-001",
        "content_profile": "home-cooking", "format": "vertical_short_15s", "aspect_ratio": "9:16",
        "duration_seconds": 15, "language": "en", "title": "Crispier roast potatoes",
        "angle": "One step most home cooks skip.",
        "sources": [{"source_id": "s1", "title": "Test kitchen notes", "publisher": "Example Kitchen",
                     "kind": "other", "url": "https://example.com/roast-potatoes",
                     "accessed_at": "2026-09-30T12:00:00Z"}],
        "claims": [{"claim_id": "c1", "text": "Parboiling roughens the potato surface.",
                    "status": "verified", "source_ids": ["s1"]}],
        "beats": [
            {"beat": "hook", "start_seconds": 0, "end_seconds": 3,
             "narration": "Your roast potatoes can be way crispier.", "on_screen_text": "CRISPIER POTATOES",
             "claim_ids": [], "sound_cue": None,
             "visual": visual("generated_image", ["text_card"], prompt="Golden roast potatoes, vertical")},
            {"beat": "context", "start_seconds": 3, "end_seconds": 7,
             "narration": "The trick happens before they hit the oven.", "on_screen_text": None,
             "claim_ids": [], "sound_cue": None, "visual": visual("motion_graphics", ["text_card"])},
            {"beat": "key_info", "start_seconds": 7, "end_seconds": 12,
             "narration": "Parboil them, then shake the pot to rough up the edges.",
             "on_screen_text": "PARBOIL + SHAKE", "claim_ids": ["c1"], "sound_cue": None,
             "visual": visual("sourced_media", ["animated_image", "text_card"], sources=["s1"])},
            {"beat": "payoff", "start_seconds": 12, "end_seconds": 15,
             "narration": "Roast hot and enjoy the crunch.", "on_screen_text": None,
             "claim_ids": [], "sound_cue": "crunch", "visual": visual("text_card", ["motion_graphics"])},
        ],
        "captions": {"enabled": True, "mode": "phrase"},
        "audio": {"voiceover": False, "music_mood": None},
        "provenance": {"created_by": "creator", "provider": "mock", "model": None,
                       "created_at": "2026-09-30T12:00:00Z"},
    }


class ShortScriptTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("No network in tests"))
        guard.start()
        self.addCleanup(guard.stop)
        self.script = read_json(ROOT / "examples/short-script-gta.json")

    def assert_invalid(self, script, code="invalid_short_script", **kwargs):
        with self.assertRaises(NetworkError) as raised:
            validate_short_script(script, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def visual(self, index=0):
        return self.script["beats"][index]["visual"]

    # Valid scripts and the verification gate

    def test_schema_is_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))

    def test_gta_example_is_valid(self):
        validate_short_script(self.script)

    def test_verification_gate(self):
        error = self.assert_invalid(self.script, "unverified_claims", require_verified_claims=True)
        self.assertIn("1 claim", error.message)
        self.script["claims"][3]["status"] = "verified"
        validate_short_script(self.script, require_verified_claims=True)

    def test_contract_is_subject_neutral(self):
        validate_short_script(cooking_script(), require_verified_claims=True)
        self.script["content_profile"] = "nba"
        validate_short_script(self.script)

    def test_validation_does_not_modify_input(self):
        before = deepcopy(self.script)
        validate_short_script(self.script)
        with self.assertRaises(NetworkError):
            validate_short_script(self.script, require_verified_claims=True)
        self.assertEqual(self.script, before)

    def test_no_provider_client_created(self):
        with patch("vicekrack.openai_provider.OpenAI", side_effect=AssertionError("No client")) as openai, \
             patch("vicekrack.anthropic_provider.Anthropic", side_effect=AssertionError("No client")) as anthropic:
            validate_short_script(self.script)
        openai.assert_not_called()
        anthropic.assert_not_called()

    # Schema rules

    def test_schema_rejections(self):
        mutations = {
            "missing field": lambda s: s.pop("beats"),
            "unknown top-level field": lambda s: s.update(extra=True),
            "unknown beat field": lambda s: s["beats"][0].update(extra=True),
            "unknown visual field": lambda s: s["beats"][0]["visual"].update(extra=True),
            "wrong contract": lambda s: s.update(contract="task"),
            "wrong version": lambda s: s.update(version="2.0"),
            "bad identifier": lambda s: s.update(script_id="Bad ID"),
            "bad profile": lambda s: s.update(content_profile=""),
            "unknown method": lambda s: s["beats"][0]["visual"].update(preferred_method="hologram"),
            "unknown fallback": lambda s: s["beats"][0]["visual"].update(fallback_methods=["hologram"]),
            "unknown format": lambda s: s.update(format="vertical_short_30s"),
            "bad language": lambda s: s.update(language="English"),
            "blank narration": lambda s: s["beats"][0].update(narration="   "),
            "boolean time": lambda s: s["beats"][0].update(start_seconds=False),
            "bad claim status": lambda s: s["claims"][0].update(status="rumor"),
            "bad provider": lambda s: s["provenance"].update(provider="other"),
            "not an object": lambda s: s.clear() or s.update(beats="x"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                script = deepcopy(self.script)
                mutate(script)
                self.assert_invalid(script)
        for value in (None, [], "script", 3):
            with self.subTest(value=value):
                self.assert_invalid(value)

    # Format template

    def test_format_template(self):
        template = FORMATS["vertical_short_15s"]
        self.assertEqual(template["duration_seconds"], 15)
        self.assertEqual([beat[0] for beat in template["beats"]], ["hook", "context", "key_info", "payoff"])
        mutations = {
            "swapped order": lambda s: s["beats"].reverse(),
            "three beats": lambda s: s["beats"].pop(),
            "five beats": lambda s: s["beats"].append(deepcopy(s["beats"][-1])),
            "gap": lambda s: s["beats"][1].update(start_seconds=4),
            "overlap": lambda s: s["beats"][0].update(end_seconds=4),
            "duration": lambda s: s.update(duration_seconds=30),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                script = deepcopy(self.script)
                mutate(script)
                self.assert_invalid(script)

    def test_fractional_times_equal_to_template_pass(self):
        self.script["beats"][1]["start_seconds"] = 3.0
        validate_short_script(self.script)

    # Narration budget

    def test_word_budget(self):
        self.assertEqual(MAX_WORDS_PER_SECOND, 3.5)
        self.assertEqual([max_words(s, e) for _, s, e in FORMATS["vertical_short_15s"]["beats"]], [10, 14, 17, 10])
        self.script["beats"][0]["narration"] = " ".join(["word"] * 10)
        validate_short_script(self.script)
        self.script["beats"][0]["narration"] = " ".join(["word"] * 11)
        error = self.assert_invalid(self.script)
        self.assertIn("beats[0].narration", error.message)

    # Visual fallbacks

    def test_fallback_rules(self):
        # Beat 1 prefers sourced_media and has a source, so only the fallback rule under test fires.
        final_rule = "final fallback must be motion_graphics or text_card"
        cases = {
            "empty": ([], "fallback_methods: schema rule"),
            "repeats preferred": (["sourced_media", "text_card"], "must not repeat the preferred method"),
            "duplicates": (["text_card", "text_card"], "fallback_methods: schema rule 'uniqueItems'"),
            "ends with ai video": (["text_card", "ai_video_clip"], final_rule),
            "ends with generated image": (["generated_image"], final_rule),
            "ends with animated image": (["animated_image"], final_rule),
        }
        for name, (fallbacks, reason) in cases.items():
            with self.subTest(name):
                script = deepcopy(self.script)
                script["beats"][1]["visual"]["fallback_methods"] = fallbacks
                self.assertIn(reason, self.assert_invalid(script).message)
        # "Ends with sourced media" on beat 3, which prefers motion_graphics.
        script = deepcopy(self.script)
        script["beats"][3]["visual"]["fallback_methods"] = ["sourced_media"]
        self.assertIn(final_rule, self.assert_invalid(script).message)
        for final in sorted(LOCAL_METHODS):
            with self.subTest(final=final):
                script = deepcopy(self.script)
                script["beats"][1]["visual"]["fallback_methods"] = ["animated_image", final]
                validate_short_script(script)

    def test_every_method_can_be_preferred_with_local_fallback(self):
        for method in VISUAL_METHODS:
            with self.subTest(method=method):
                script = deepcopy(self.script)
                final = "text_card" if method != "text_card" else "motion_graphics"
                script["beats"][1]["visual"].update(preferred_method=method, fallback_methods=[final],
                                                    generation_prompt="Original scene, vertical")
                validate_short_script(script)

    def test_method_inputs(self):
        self.visual(0)["generation_prompt"] = None
        self.assert_invalid(self.script)
        self.visual(0)["generation_prompt"] = "   "
        self.assert_invalid(self.script)
        self.script = read_json(ROOT / "examples/short-script-gta.json")
        self.visual(1)["source_ids"] = []
        self.assert_invalid(self.script)
        self.visual(1).update(preferred_method="animated_image", fallback_methods=["text_card"])
        self.assert_invalid(self.script)
        self.visual(1)["generation_prompt"] = "Original sunny coastline, vertical"
        validate_short_script(self.script)

    # References between sources, claims, and beats

    def test_reference_rules(self):
        mutations = {
            "unknown claim": lambda s: s["beats"][0]["claim_ids"].append("c99"),
            "unknown claim source": lambda s: s["claims"][0]["source_ids"].append("missing"),
            "unknown visual source": lambda s: s["beats"][1]["visual"]["source_ids"].append("missing"),
            "duplicate source": lambda s: s["sources"].append(deepcopy(s["sources"][0])),
            "duplicate claim": lambda s: s["claims"].append(deepcopy(s["claims"][0])),
            "unused claim": lambda s: s["beats"][3].update(claim_ids=[]),
            "verified without source": lambda s: s["claims"][0].update(source_ids=[]),
            "key info without claims": lambda s: (s["beats"][2].update(claim_ids=[]),
                                                  s["beats"][1]["claim_ids"].append("c3")),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                script = deepcopy(self.script)
                mutate(script)
                self.assert_invalid(script)

    def test_unverified_claim_may_have_no_sources_while_drafting(self):
        self.script["claims"][3]["source_ids"] = []
        validate_short_script(self.script)

    # Values the installed jsonschema format checker does not enforce

    def test_timestamps_are_real_dates(self):
        for value in ("2026-02-30T12:00:00Z", "garbage", "2026-09-30T12:00:00", "2026-09-30T12:00:00+00:00"):
            with self.subTest(value=value):
                script = deepcopy(self.script)
                script["provenance"]["created_at"] = value
                self.assert_invalid(script)
                script = deepcopy(self.script)
                script["sources"][0]["accessed_at"] = value
                self.assert_invalid(script)
        self.script["sources"][0]["accessed_at"] = "2026-09-30T12:00:00.5Z"
        validate_short_script(self.script)

    def test_urls_must_be_https(self):
        for value in ("http://example.com", "not a url", "https://", "https:///path",
                      "https://user:pass@example.com", "https://exa mple.com", "ftp://example.com", ""):
            with self.subTest(value=value):
                script = deepcopy(self.script)
                script["sources"][0]["url"] = value
                self.assert_invalid(script)
        self.script["sources"][0]["url"] = "https://www.rockstargames.com/newswire"
        validate_short_script(self.script)

    def test_optional_text_cannot_be_blank(self):
        for path in (("beats", 0, "on_screen_text"), ("beats", 0, "sound_cue"),
                     ("audio", "music_mood"), ("provenance", "model")):
            with self.subTest(path=path):
                script = deepcopy(self.script)
                target = script
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "  "
                self.assert_invalid(script)

    # Safety

    def test_non_finite_numbers_rejected(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                script = deepcopy(self.script)
                script["beats"][0]["end_seconds"] = value
                self.assert_invalid(script)

    def test_credentials_rejected(self):
        self.script["beats"][0]["narration"] = "sk-" + "a" * 30
        self.assert_invalid(self.script, "sensitive_state")
        self.script = read_json(ROOT / "examples/short-script-gta.json")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-private-value"}):
            self.script["angle"] = "contains synthetic-private-value"
            error = self.assert_invalid(self.script, "sensitive_state")
        self.assertNotIn("synthetic-private-value", error.message)

    def test_errors_never_echo_script_content(self):
        marker = "PRIVATE-MARKER-TEXT"
        script = deepcopy(self.script)
        script["title"] = marker * 10
        self.assertNotIn(marker, self.assert_invalid(script).message)
        script = deepcopy(self.script)
        script["beats"][0]["narration"] = " ".join([marker] * 11)
        self.assertNotIn(marker, self.assert_invalid(script).message)
        script = deepcopy(self.script)
        script[marker] = True
        self.assertNotIn(marker, self.assert_invalid(script).message)


if __name__ == "__main__":
    unittest.main()
