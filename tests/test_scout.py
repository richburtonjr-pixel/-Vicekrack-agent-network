"""Step 16: Scout research intake. Offline only: sockets are blocked and HTTP is faked."""

import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack import scout
from vicekrack.__main__ import main
from vicekrack.creator import draft_short_script
from vicekrack.errors import NetworkError
from vicekrack.orchestrator import ROOT, read_json
from vicekrack.scout import (
    FixtureFetcher, HttpFetcher, SourceError, build_candidate, canonical_url, check_https_url,
    clean_text, keyword_patterns, load_sources, parse_feed, parse_timestamp, run_scout,
    validate_candidate, validate_sources,
)
from vicekrack.scout_cli import _publish, existing_fingerprints, list_candidates, scout_once, store_paths
from vicekrack.story_brief import validate_story_brief

STAMP = "2026-10-04T12:00:00Z"
MOCK = "config/scout-sources.mock.json"
GTA = "config/scout-sources.gta.json"


def clock():
    return STAMP


def rss(*items, extra=""):
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link><description>{text}</description>{extra}</item>"
        for title, link, text in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{body}</channel></rss>'.encode()


class StaticFetcher:
    """Returns prepared bytes (or raises) per source_id; counts calls."""

    def __init__(self, responses, mode="fixture"):
        self.responses, self.mode, self.calls = responses, mode, []

    def fetch(self, source, limits):
        self.calls.append(source["source_id"])
        value = self.responses[source["source_id"]]
        if isinstance(value, Exception):
            raise value
        return value


def config_with(**changes):
    config = read_json(ROOT / MOCK)
    config.update(deepcopy(changes))
    return config


class Guarded(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        # A regression can never silently turn these tests into live requests.
        patch("socket.socket.connect", side_effect=AssertionError("Network disabled in tests")).start()
        patch("urllib.request.urlopen", side_effect=AssertionError("urlopen disabled in tests")).start()


class SourceConfigTests(Guarded):
    def test_shipped_source_lists(self):
        mock, _ = load_sources(MOCK)
        self.assertEqual(mock["fetch_mode"], "fixture")
        gta, _ = load_sources(GTA)
        self.assertEqual(gta["fetch_mode"], "http")
        by_id = {s["source_id"]: s for s in gta["sources"]}
        self.assertFalse(by_id["rockstar-newswire"]["enabled"])
        self.assertEqual({s["category"] for s in gta["sources"]},
                         {"official_publisher", "official_parent_company", "press"})
        for source in gta["sources"]:
            check_https_url(source["feed_url"])
        self.assertIn("GTA VI", gta["keywords"])

    def test_config_hash_is_deterministic(self):
        self.assertEqual(load_sources(MOCK)[1], load_sources(MOCK)[1])

    def test_rejected_configs(self):
        base = read_json(ROOT / MOCK)
        source = base["sources"][0]

        def with_source(**fields):
            value = deepcopy(base)
            value["sources"][0].update(fields)
            return value

        def with_limit(**fields):
            value = deepcopy(base)
            value["limits"].update(fields)
            return value

        duplicate = deepcopy(base)
        duplicate["sources"][1]["source_id"] = source["source_id"]
        no_fixture = deepcopy(base)
        del no_fixture["sources"][0]["fixture"]
        too_many = deepcopy(base)
        too_many["sources"] = [dict(source, source_id=f"s{i}") for i in range(21)]
        cases = {
            "duplicate": duplicate, "no_fixture": no_fixture, "too_many_sources": too_many,
            "http_feed": with_source(feed_url="http://official.example.com/feed.xml"),
            "ip_feed": with_source(feed_url="https://127.0.0.1/feed.xml"),
            "ipv6_feed": with_source(feed_url="https://[::1]/feed.xml"),
            "localhost": with_source(feed_url="https://localhost/feed.xml"),
            "login": with_source(feed_url="https://user:pw@official.example.com/feed.xml"),
            "port": with_source(feed_url="https://official.example.com:8443/feed.xml"),
            "file_scheme": with_source(feed_url="file:///etc/passwd"),
            "fixture_escape": with_source(fixture="../outside.xml"),
            "wildcard_host": with_source(link_hosts=["*.example.com"]),
            "bad_category": with_source(category="blog"),
            "bytes": with_limit(max_bytes_per_source=2_000_001),
            "items": with_limit(max_items_per_source=51),
            "timeout": with_limit(timeout_seconds=31),
            "zero_timeout": with_limit(timeout_seconds=0),
            "candidates": with_limit(max_candidates_per_run=101),
            "extra": dict(base, crawl_depth=3),
            "mode": dict(base, fetch_mode="crawl"),
            "no_keywords": dict(base, keywords=[]),
        }
        for name, config in cases.items():
            with self.subTest(name=name), self.assertRaises(NetworkError) as raised:
                validate_sources(config)
            self.assertEqual(raised.exception.code, "invalid_scout_config")

    def test_credentials_in_config_rejected(self):
        value = read_json(ROOT / MOCK)
        value["sources"][0]["api_key"] = "synthetic"
        with self.assertRaises(NetworkError) as raised:
            validate_sources(value)
        self.assertEqual(raised.exception.code, "sensitive_state")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-active-credential"}):
            value = read_json(ROOT / MOCK)
            value["description"] = "synthetic-active-credential"
            with self.assertRaises(NetworkError) as raised:
                validate_sources(value)
            self.assertEqual(raised.exception.code, "sensitive_state")

    def test_config_path_must_stay_in_project(self):
        for path in ("../outside.json", "/etc/hosts", "config/missing.json"):
            with self.subTest(path=path), self.assertRaises(NetworkError) as raised:
                load_sources(path)
            self.assertEqual(raised.exception.code, "invalid_scout_config")


class UrlTests(Guarded):
    def test_url_safety(self):
        self.assertEqual(check_https_url("https://WWW.Example.com/a?b=1"), "www.example.com")
        self.assertEqual(check_https_url("https://example.com:443/x"), "example.com")
        for bad in ("http://example.com", "https://", "ftp://example.com", "https://user@example.com/",
                    "https://example.com:8080/", "https://10.0.0.1/", "https://[::1]/", "https://localhost/",
                    "https://intranet/", "https://a.localhost/", "https://exa mple.com/", "https://e.com/\x00",
                    "javascript:alert(1)", "", None, 42, "https://e.com/" + "a" * 2050):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_https_url(bad)

    def test_canonical_url(self):
        self.assertEqual(
            canonical_url("https://Press.Example.org/a/b/?utm_source=x&id=7&fbclid=y&UTM_Medium=z#frag"),
            "https://press.example.org/a/b?id=7")
        self.assertEqual(canonical_url("https://e.com"), "https://e.com/")
        self.assertEqual(canonical_url("https://e.com/a?b=2&a=1"), "https://e.com/a?b=2&a=1")


class ParsingTests(Guarded):
    def test_rss_and_atom_fixtures(self):
        official = parse_feed((ROOT / "examples/scout/official-feed.xml").read_bytes())
        self.assertEqual(len(official), 5)
        self.assertEqual(official[0]["published"], "Fri, 02 Oct 2026 15:30:00 +0000")
        press = parse_feed((ROOT / "examples/scout/press-feed.atom").read_bytes())
        self.assertEqual([item["link"] for item in press][:2], [
            "https://press.example.org/2026/10/gta-6-launch-window/", "https://press.example.org/2026/10/leonida-map"])
        self.assertEqual(press[1]["published"], "")

    def test_malformed_and_dangerous_feeds(self):
        cases = [
            b"", b"not xml", b"<rss><channel><item></rss>", b"<html><body>hi</body></html>",
            b"<rss version='2.0'></rss>",
            b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><rss><channel/></rss>',
            b'<!ENTITY x SYSTEM "file:///etc/passwd">',
            b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>',
        ]
        for data in cases:
            with self.subTest(data=data[:30]), self.assertRaises(SourceError) as raised:
                parse_feed(data)
            self.assertEqual(raised.exception.code, "source_malformed")

    def test_clean_text(self):
        self.assertEqual(clean_text("<p>A &amp; B</p><script>evil()</script><style>x{}</style>\x07C"), "A & B C")
        long = clean_text("word " * 500, 50)
        self.assertLessEqual(len(long), 50)
        self.assertTrue(long.endswith("..."))
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text("<b>unclosed"), "unclosed")

    def test_timestamps(self):
        self.assertEqual(parse_timestamp("Fri, 02 Oct 2026 15:30:00 +0200"), "2026-10-02T13:30:00Z")
        self.assertEqual(parse_timestamp("2026-10-03T08:15:00Z"), "2026-10-03T08:15:00Z")
        self.assertEqual(parse_timestamp("2026-10-03T08:15:00-04:00"), "2026-10-03T12:15:00Z")
        self.assertEqual(parse_timestamp("2026-10-03T08:15:00"), "2026-10-03T08:15:00Z")
        for bad in ("", "yesterday", "Fri, 99 Oct 2026", "1850-01-01T00:00:00Z", "x" * 100, None):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_timestamp(bad))


class RunTests(Guarded):
    def run_mock(self, config=None, existing_ids=(), existing_titles=()):
        config = config or read_json(ROOT / MOCK)
        return run_scout(config, "0" * 64, FixtureFetcher(), existing_ids=existing_ids,
                         existing_titles=existing_titles, clock=clock)

    def test_deterministic_mock_run(self):
        first = self.run_mock()
        self.assertEqual(first, self.run_mock())
        candidates, report = first
        self.assertEqual([c["title"] for c in candidates], [
            "Fixture: GTA VI trailer event scheduled",
            "Fixture: Analysts discuss the Grand Theft Auto VI launch window",
            "Fixture: Leonida map speculation roundup"])
        self.assertEqual(report["totals"], {"sources_ok": 2, "sources_failed": 0, "sources_disabled": 1,
                                            "sources_skipped": 0, "candidates_new": 3, "duplicates": 2,
                                            "items_skipped": 5})
        self.assertEqual(report["sources"][0]["skipped"], {"duplicate": 1, "not_relevant": 1,
                                                           "unsafe_link": 1, "unapproved_link_host": 1})
        self.assertEqual(report["sources"][1]["skipped"], {"duplicate": 1})
        self.assertFalse(report["truncated"])
        self.assertFalse(report["verified"])

    def test_provenance_preserved(self):
        candidates, _ = self.run_mock()
        official, press, undated = candidates
        self.assertEqual(official["source"], {"source_id": "fixture-official", "name": "Fixture Official Newsroom",
                                              "publisher": "Example Studio", "kind": "official",
                                              "category": "official_publisher"})
        self.assertEqual(official["url"], "https://official.example.com/news/gta-vi-trailer-event")
        self.assertEqual(official["published_at"], "2026-10-02T15:30:00Z")
        self.assertEqual(official["retrieved_at"], STAMP)
        self.assertEqual(official["matched_keywords"], ["GTA VI"])
        self.assertEqual(official["excerpt"], "Fixture text: the studio says a GTA VI trailer event is scheduled "
                                              "for next week. The fixture also says the event will stream online. "
                                              "This sentence has no keyword.")
        self.assertEqual(official["candidate_claims"], [{
            "claim_id": "k1", "basis": "excerpt", "status": "unverified",
            "text": "Fixture text: the studio says a GTA VI trailer event is scheduled for next week."}])
        self.assertNotIn("alert", press["excerpt"])
        self.assertEqual(press["url"], "https://press.example.org/2026/10/gta-6-launch-window")
        self.assertIsNone(undated["published_at"])
        self.assertEqual(undated["matched_keywords"], ["Leonida"])
        for candidate in candidates:
            validate_candidate(candidate)
            self.assertEqual(candidate["provenance"]["fetch_mode"], "fixture")

    def test_everything_stays_unverified(self):
        candidates, report = self.run_mock()
        for candidate in candidates:
            self.assertEqual(candidate["verification"], {"status": "unverified", "required_before": "story_brief"})
            self.assertEqual({c["status"] for c in candidate["candidate_claims"]}, {"unverified"})
            tampered = deepcopy(candidate)
            tampered["verification"]["status"] = "verified"
            with self.assertRaises(NetworkError):
                validate_candidate(tampered)
            tampered = deepcopy(candidate)
            tampered["candidate_claims"][0]["status"] = "verified"
            with self.assertRaises(NetworkError):
                validate_candidate(tampered)

    def test_candidates_cannot_bypass_verification_into_creator(self):
        candidate = self.run_mock()[0][0]
        with self.assertRaises(NetworkError):
            validate_story_brief(candidate)
        with self.assertRaises(NetworkError):
            draft_short_script(candidate)

    def test_tampered_candidates_rejected(self):
        candidate = self.run_mock()[0][0]
        mutations = [
            lambda c: c.update(url="https://official.example.com/other"),
            lambda c: c.update(candidate_id="cand-" + "0" * 24),
            lambda c: c.update(title="Different headline"),
            lambda c: c.update(url=c["url"] + "?utm_source=x"),
            lambda c: c.update(extra=True),
            lambda c: c["candidate_claims"].append(dict(c["candidate_claims"][0], claim_id="k3")),
            lambda c: c.update(published_at="2026-10-02"),
            lambda c: c["source"].update(kind="rumor"),
            lambda c: c.update(excerpt="x" * 601),
        ]
        for index, mutate in enumerate(mutations):
            value = deepcopy(candidate)
            mutate(value)
            with self.subTest(index=index), self.assertRaises(NetworkError):
                validate_candidate(value)

    def test_cross_run_duplicates(self):
        candidates, _ = self.run_mock()
        ids = {c["candidate_id"] for c in candidates}
        titles = {(c["source"]["source_id"], c["fingerprints"]["title_sha256"]) for c in candidates}
        again, report = self.run_mock(existing_ids=ids, existing_titles=titles)
        self.assertEqual(again, [])
        self.assertEqual(report["totals"]["duplicates"], 5)
        # A syndicated copy is still caught when only the original's ID is stored.
        again, _ = self.run_mock(existing_ids=ids)
        self.assertEqual(again, [])

    def test_bounded_items_and_candidates(self):
        config = config_with(limits={"max_items_per_source": 2, "max_bytes_per_source": 1000000,
                                     "timeout_seconds": 10, "max_candidates_per_run": 1})
        candidates, report = run_scout(config, "0" * 64, FixtureFetcher(), clock=clock)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(report["sources"][0]["items_read"], 2)
        # Once the run cap is reached, remaining items are counted as run_limit.
        self.assertEqual(report["sources"][0]["skipped"], {"run_limit": 1})
        self.assertEqual(report["sources"][1]["status"], "skipped_run_limit")
        self.assertTrue(report["truncated"])

    def test_large_feed_is_bounded(self):
        items = [(f"GTA VI item {i} " + "long " * 100, f"https://official.example.com/n/{i}", "<p>" + "GTA 6 sentence here. " * 400 + "</p>")
                 for i in range(500)]
        fetcher = StaticFetcher({"fixture-official": rss(*items), "fixture-press": b"<rss><channel/></rss>"})
        candidates, report = run_scout(read_json(ROOT / MOCK), "0" * 64, fetcher, clock=clock)
        self.assertEqual(report["sources"][0]["items_read"], 20)
        self.assertEqual(len(candidates), 20)
        for candidate in candidates:
            self.assertLessEqual(len(candidate["title"]), 200)
            self.assertLessEqual(len(candidate["excerpt"]), 600)
            self.assertLessEqual(len(candidate["candidate_claims"]), 3)
            self.assertTrue(all(len(c["text"]) <= 300 for c in candidate["candidate_claims"]))
            self.assertLess(len(json.dumps(candidate)), 4000)

    def test_oversized_fixture(self):
        config = config_with(limits={"max_items_per_source": 20, "max_bytes_per_source": 1024,
                                     "timeout_seconds": 10, "max_candidates_per_run": 50})
        _, report = run_scout(config, "0" * 64, FixtureFetcher(), clock=clock)
        self.assertEqual([r["error_code"] for r in report["sources"][:2]], ["source_too_large", "source_too_large"])

    def test_failing_sources_are_isolated_and_sanitized(self):
        fetcher = StaticFetcher({
            "fixture-official": RuntimeError("secret local path /home/user/.ssh and key sk-" + "a" * 30),
            "fixture-press": (ROOT / "examples/scout/press-feed.atom").read_bytes()})
        candidates, report = run_scout(read_json(ROOT / MOCK), "0" * 64, fetcher, clock=clock)
        self.assertEqual(report["sources"][0]["status"], "failed")
        self.assertEqual(report["sources"][0]["error_code"], "source_error")
        self.assertEqual(report["sources"][1]["status"], "ok")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(fetcher.calls, ["fixture-official", "fixture-press"])
        dumped = json.dumps(report)
        self.assertNotIn("secret", dumped)
        self.assertNotIn(".ssh", dumped)
        for code in ("source_timeout", "source_malformed", "source_unavailable"):
            fetcher = StaticFetcher({"fixture-official": SourceError(code), "fixture-press": b"<<<"})
            _, report = run_scout(read_json(ROOT / MOCK), "0" * 64, fetcher, clock=clock)
            self.assertEqual([r["error_code"] for r in report["sources"][:2]], [code, "source_malformed"])
            self.assertEqual(report["totals"]["sources_failed"], 2)

    def test_missing_fixture_file(self):
        config = read_json(ROOT / MOCK)
        config["sources"][0]["fixture"] = "examples/scout/missing.xml"
        _, report = run_scout(config, "0" * 64, FixtureFetcher(), clock=clock)
        self.assertEqual(report["sources"][0]["error_code"], "source_unavailable")

    def test_credential_shaped_content_never_stored(self):
        source = read_json(ROOT / MOCK)["sources"][0]
        patterns = keyword_patterns(["GTA VI"])
        options = dict(profile="gta", patterns=patterns, config_sha256="0" * 64, retrieved_at=STAMP, fetch_mode="fixture")
        leaked = {"title": "GTA VI leak sk-" + "b" * 30, "link": "https://official.example.com/x",
                  "published": "", "summary": ""}
        self.assertEqual(build_candidate(leaked, source, **options), (None, "sensitive_content"))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-live-credential"}):
            active = dict(leaked, title="GTA VI story", summary="contains synthetic-live-credential")
            self.assertEqual(build_candidate(active, source, **options), (None, "sensitive_content"))

    def test_item_level_url_rules(self):
        source = read_json(ROOT / MOCK)["sources"][0]
        options = dict(profile="gta", patterns=keyword_patterns(["GTA VI"]), config_sha256="0" * 64,
                       retrieved_at=STAMP, fetch_mode="fixture")
        base = {"title": "GTA VI news", "published": "", "summary": ""}
        for link, reason in (("javascript:alert(1)", "unsafe_link"), ("", "unsafe_link"),
                             ("https://official.example.com.evil.net/x", "unapproved_link_host"),
                             ("https://192.168.0.1/x", "unsafe_link")):
            with self.subTest(link=link):
                self.assertEqual(build_candidate(dict(base, link=link), source, **options), (None, reason))
        self.assertEqual(build_candidate(dict(base, title=" ", link="https://official.example.com/x"),
                                         source, **options), (None, "missing_title"))

    def test_keyword_matching_uses_word_boundaries(self):
        patterns = keyword_patterns(["GTA 6", "GTA VI"])
        self.assertTrue(patterns[0][1].search("Is gta 6 delayed?"))
        self.assertFalse(patterns[0][1].search("GTA 60 fps mods"))
        self.assertFalse(patterns[1][1].search("GTA VII"))


class FakeResponse:
    def __init__(self, body=b"", status=200, url="https://ir.take2games.com/rss/news-releases.xml", headers=None, chunk=None):
        self.body, self.status, self.url = body, status, url
        self.headers = headers or {}
        self.chunk = chunk
        self.offset = 0

    def geturl(self):
        return self.url

    def read1(self, size):
        size = min(size, self.chunk or size)
        data = self.body[self.offset:self.offset + size]
        self.offset += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, result):
        self.result, self.requests = result, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class HttpFetcherTests(Guarded):
    def setUp(self):
        super().setUp()
        config = read_json(ROOT / GTA)
        self.limits = config["limits"]
        self.source = next(s for s in config["sources"] if s["source_id"] == "take-two-ir")

    def fetch(self, result, clock=None, source=None, limits=None):
        opener = FakeOpener(result)
        fetcher = HttpFetcher(opener=opener, **({"clock": clock} if clock else {}))
        return fetcher.fetch(source or self.source, limits or self.limits), opener

    def assert_code(self, result, code, **kwargs):
        with self.assertRaises(SourceError) as raised:
            self.fetch(result, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_success_and_request_shape(self):
        body = rss(("GTA VI", "https://ir.take2games.com/news/1", "text"))
        data, opener = self.fetch(FakeResponse(body))
        self.assertEqual(data, body)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, self.source["feed_url"])
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, self.limits["timeout_seconds"])
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertTrue(headers["user-agent"].startswith("ViceKrack-Scout/"))
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)

    def test_failures_map_to_safe_codes(self):
        cases = [
            (scout._RedirectBlocked(), "source_redirect_blocked"),
            (FakeResponse(url="https://other.example.com/feed"), "source_redirect_blocked"),
            (FakeResponse(url="http://ir.take2games.com/rss"), "source_redirect_blocked"),
            (FakeResponse(status=500), "source_http_error"),
            (urllib.error.HTTPError(self.source["feed_url"], 404, "secret", {}, None), "source_http_error"),
            (urllib.error.URLError(TimeoutError("secret")), "source_timeout"),
            (socket.timeout("secret"), "source_timeout"),
            (TimeoutError(), "source_timeout"),
            (urllib.error.URLError("connection refused secret"), "source_unavailable"),
            (ConnectionResetError("secret"), "source_unavailable"),
            (ValueError("secret"), "source_unavailable"),
            (FakeResponse(headers={"Content-Length": "5000000"}), "source_too_large"),
            (FakeResponse(body=b"x" * 1_000_001), "source_too_large"),
        ]
        for result, code in cases:
            with self.subTest(code=code, result=type(result).__name__):
                self.assert_code(result, code)

    def test_slow_body_hits_deadline(self):
        ticks = iter(range(0, 1000, 15))
        self.assert_code(FakeResponse(body=b"x" * 1000, chunk=10), "source_timeout", clock=lambda: next(ticks))

    def test_unsafe_feed_url_never_requested(self):
        opener = FakeOpener(AssertionError("must not be called"))
        for url in ("http://ir.take2games.com/rss", "https://127.0.0.1/rss", "https://localhost/rss"):
            with self.subTest(url=url), self.assertRaises(SourceError) as raised:
                HttpFetcher(opener=opener).fetch(dict(self.source, feed_url=url), self.limits)
            self.assertEqual(raised.exception.code, "source_unsafe_url")
        self.assertEqual(opener.requests, [])

    def test_redirect_handler_blocks(self):
        with self.assertRaises(scout._RedirectBlocked):
            scout._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/")

    def test_default_fetcher_creates_no_connection(self):
        fetcher = HttpFetcher()
        self.assertEqual(fetcher.mode, "http")


class CliTests(Guarded):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        patch("vicekrack.scout_cli.ROOT", self.folder).start()

    def command(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["vicekrack", *args]), redirect_stdout(output):
            code = main()
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_scout_sources(self):
        code, result, _ = self.command("scout-sources")
        self.assertEqual((code, result["fetch_mode"], result["requires_live_flag"]), (0, "fixture", False))
        code, result, _ = self.command("scout-sources", "--sources", GTA)
        self.assertEqual((code, result["requires_live_flag"]), (0, True))
        self.assertEqual(len(result["sources"]), 4)
        code, result, _ = self.command("scout-sources", "--sources", "../x.json")
        self.assertEqual((code, result["error"]["code"]), (1, "invalid_scout_config"))

    def test_offline_scout_saves_candidates_and_report(self):
        code, result, raw = self.command("scout")
        self.assertEqual(code, 0)
        self.assertEqual((result["candidates_new"], result["fetch_mode"], result["verified"], result["published"]),
                         (3, "fixture", False, False))
        self.assertNotIn("Fixture:", raw)  # Run output reports counts, not external text.
        candidates_dir, runs_dir = store_paths(self.folder)
        files = sorted(candidates_dir.glob("*.json"))
        self.assertEqual(len(files), 3)
        for path in files:
            candidate = read_json(path)
            validate_candidate(candidate)
            self.assertEqual(path.stem, candidate["candidate_id"])
        report = read_json(Path(result["run_file"]))
        self.assertEqual(report["run_id"], result["run_id"])
        self.assertEqual(sorted(report["candidate_ids"]), [p.stem for p in files])
        self.assertEqual(list(candidates_dir.glob("*.tmp")) + list(runs_dir.glob("*.tmp")), [])
        code, result, _ = self.command("scout")
        self.assertEqual((code, result["candidates_new"], result["duplicates"]), (0, 0, 5))
        self.assertEqual(len(list(candidates_dir.glob("*.json"))), 3)
        self.assertEqual(len(list(runs_dir.glob("*.json"))), 2)

    def test_live_sources_need_explicit_flag(self):
        with patch.object(HttpFetcher, "fetch", side_effect=AssertionError("No network")) as fetch:
            code, result, _ = self.command("scout", "--sources", GTA)
        self.assertEqual((code, result["error"]["code"]), (1, "live_fetch_not_enabled"))
        fetch.assert_not_called()
        self.assertFalse((self.folder / "runtime").exists())

    def test_live_mode_with_faked_transport(self):
        feeds = {
            "https://ir.take2games.com/rss/news-releases.xml": rss(
                ("Take-Two update on Grand Theft Auto VI", "https://ir.take2games.com/news/1",
                 "Fixture text: an update mentions Grand Theft Auto VI timing.")),
            "https://www.gamespot.com/feeds/news/": rss(
                ("GTA 6 story", "https://www.gamespot.com/articles/gta-6/", "Fixture text: GTA 6 news.")),
        }

        class Opener:
            def open(self, request, timeout):
                if request.full_url not in feeds:
                    raise urllib.error.URLError("unreachable")
                return FakeResponse(feeds[request.full_url], url=request.full_url)

        report, run_file, _ = scout_once(GTA, live=True, root=self.folder, fetcher=HttpFetcher(opener=Opener()),
                                         clock=clock)
        self.assertEqual(report["fetch_mode"], "http")
        self.assertEqual(report["totals"]["candidates_new"], 2)
        statuses = {r["source_id"]: (r["status"], r["error_code"]) for r in report["sources"]}
        self.assertEqual(statuses, {"rockstar-newswire": ("disabled", None), "take-two-ir": ("ok", None),
                                    "gamespot-news": ("ok", None), "ign-games": ("failed", "source_unavailable")})
        rows = list_candidates(self.folder)
        self.assertEqual({r["source_id"] for r in rows}, {"take-two-ir", "gamespot-news"})
        self.assertTrue(run_file.is_file())

    def test_fetcher_must_match_mode(self):
        with self.assertRaises(NetworkError) as raised:
            scout_once(MOCK, root=self.folder, fetcher=HttpFetcher(opener=FakeOpener(AssertionError())))
        self.assertEqual(raised.exception.code, "invalid_scout_config")

    def test_scout_list(self):
        self.command("scout")
        code, result, _ = self.command("scout-list")
        self.assertEqual(code, 0)
        rows = result["candidates"]
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["published_at"] for r in rows], ["2026-10-03T08:15:00Z", "2026-10-02T15:30:00Z", None])
        self.assertEqual({r["verification"] for r in rows}, {"unverified"})
        self.assertFalse(result["verified"])
        code, result, _ = self.command("scout-list", "--limit", "1")
        self.assertEqual(len(result["candidates"]), 1)
        candidates_dir, _ = store_paths(self.folder)
        first = sorted(candidates_dir.glob("*.json"))[0]
        tampered = read_json(first)
        tampered["verification"]["status"] = "verified"
        first.write_text(json.dumps(tampered))
        (candidates_dir / ("cand-" + "f" * 24 + ".json")).write_text("{broken")
        rows = list_candidates(self.folder)
        self.assertEqual(sum("error" in r for r in rows), 2)
        self.assertEqual(sum("error" not in r for r in rows), 2)

    def test_publish_never_overwrites(self):
        folder = self.folder / "pub"
        self.assertTrue(_publish({"a": 1}, folder, "x.json"))
        self.assertFalse(_publish({"a": 2}, folder, "x.json"))
        self.assertEqual(read_json(folder / "x.json"), {"a": 1})
        self.assertEqual(list(folder.glob("*.tmp")), [])
        with patch("vicekrack.scout_cli.os.link", side_effect=PermissionError("secret")), \
             self.assertRaises(NetworkError) as raised:
            _publish({"a": 3}, folder, "y.json")
        self.assertEqual(raised.exception.code, "scout_write_failed")
        self.assertEqual(list(folder.glob("*.tmp")), [])

    def test_unreadable_stored_candidate_still_reserves_id(self):
        candidates_dir, _ = store_paths(self.folder)
        candidates_dir.mkdir(parents=True)
        reserved = run_scout(read_json(ROOT / MOCK), "0" * 64, FixtureFetcher(), clock=clock)[0][0]["candidate_id"]
        (candidates_dir / f"{reserved}.json").write_text("corrupt")
        ids, titles = existing_fingerprints(candidates_dir)
        self.assertEqual((ids, titles), ({reserved}, set()))
        report, _, _ = scout_once(MOCK, root=self.folder, clock=clock)
        self.assertNotIn(reserved, report["candidate_ids"])
        self.assertEqual((candidates_dir / f"{reserved}.json").read_text(), "corrupt")

    def test_storage_error_code(self):
        with patch("vicekrack.scout_cli._publish", side_effect=NetworkError("scout_write_failed", "x")):
            code, result, _ = self.command("scout")
        self.assertEqual((code, result["error"]["code"]), (1, "scout_write_failed"))


if __name__ == "__main__":
    unittest.main()
