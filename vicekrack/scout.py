"""Scout (Step 16): bounded research intake from an explicit list of approved feeds.

The Scout reads only the RSS/Atom feeds named in a source-list config. It never follows
article links, crawls, searches, or calls a model. Every candidate it produces is
`unverified` by contract: the Scout discovers, a later Verification stage decides, and the
Creator writes only from verified Story Briefs. Discovery -> verification -> creation stay
separate.

Two fetch modes:
- fixture: reads local synthetic feed files (offline; used by default and in tests);
- http: real HTTPS requests, only when the caller explicitly enables live fetching.

Every limit is bounded (sources, bytes, items, candidates, time). A failing source is
recorded as an error code and the run continues. Raw exceptions, response bodies and
environment values are never included in errors or output.
"""

import hashlib
import http.client
import json
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from html.parser import HTMLParser
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jsonschema import Draft202012Validator

from .errors import NetworkError
from .persistence import reject_secrets


ROOT = Path(__file__).resolve().parent.parent
SOURCES_SCHEMA = ROOT / "schemas/scout-sources.schema.json"
CANDIDATE_SCHEMA = ROOT / "schemas/story-candidate.schema.json"

TITLE_CHARS, EXCERPT_CHARS, CLAIM_CHARS, MAX_CLAIMS = 200, 600, 300, 3
SCAN_CHARS = 5000            # Text examined for keywords/claims per item.
MIN_CLAIM_CHARS = 20
READ_CHUNK = 65536
BODY_DEADLINE_FACTOR = 2     # Whole-body wall clock limit = timeout_seconds * factor.
TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "igshid", "ocid", "cmpid"}
ATOM = "{http://www.w3.org/2005/Atom}"
USER_AGENT = "ViceKrack-Scout/1.0 (bounded research intake)"
SKIP_REASONS = ("not_relevant", "unsafe_link", "unapproved_link_host", "missing_title",
                "duplicate", "sensitive_content", "invalid_item", "run_limit")


class SourceError(Exception):
    """A fixed, safe error code for one source. Never carries raw diagnostics."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _RedirectBlocked(Exception):
    pass


@lru_cache(maxsize=None)
def _validator(path):
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- URLs

def check_https_url(value):
    """Return the lowercase host of a safe public https URL, or raise ValueError.

    Rejects other schemes, embedded logins, non-default ports, IP literals, localhost,
    single-label hosts, whitespace and control characters.
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError
    if any(c.isspace() or not c.isprintable() for c in value):
        raise ValueError
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or not host or "@" in parts.netloc:
        raise ValueError
    if parts.port not in (None, 443):
        raise ValueError
    if host == "localhost" or host.endswith(".localhost") or "." not in host:
        raise ValueError
    try:
        ip_address(host.strip("[]"))
    except ValueError:
        return host
    raise ValueError  # IP literals are never approved sources.


def canonical_url(value):
    """Stable form for deduplication: lowercase scheme/host, no fragment, no tracking params."""
    parts = urlsplit(value)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.hostname.lower(), path, urlencode(query), ""))


# ---------------------------------------------------------------- configuration

def _config_fail(location, reason):
    raise NetworkError("invalid_scout_config", f"Scout source list rejected at {location}: {reason}.")


def load_sources(path, root=ROOT):
    """Load and validate a source list inside the project. Returns (config, sha256)."""
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        _config_fail("$", "the file must remain inside the project")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        _config_fail("$", "cannot read valid JSON")
    validate_sources(config, root)
    return config, _sha256(_canonical_json(config))


def validate_sources(config, root=ROOT):
    reject_secrets(config)
    error = next(_validator(str(SOURCES_SCHEMA)).iter_errors(config), None)
    if error is not None:
        _config_fail(".".join(str(p) for p in error.absolute_path) or "$", f"schema rule '{error.validator}'")
    root = Path(root).resolve()
    seen = set()
    for index, source in enumerate(config["sources"]):
        where = f"sources[{index}]"
        if source["source_id"] in seen:
            _config_fail(f"{where}.source_id", "duplicate source_id")
        seen.add(source["source_id"])
        try:
            check_https_url(source["feed_url"])
        except ValueError:
            _config_fail(f"{where}.feed_url", "must be a public https URL without login or port")
        if "fixture" in source:
            fixture = (root / source["fixture"]).resolve()
            if not fixture.is_relative_to(root):
                _config_fail(f"{where}.fixture", "must remain inside the project")
        elif config["fetch_mode"] == "fixture" and source["enabled"]:
            _config_fail(f"{where}.fixture", "enabled sources need a fixture in fixture mode")


# ---------------------------------------------------------------- fetchers

class FixtureFetcher:
    """Offline fetcher: reads a source's local fixture file within the project."""

    mode = "fixture"

    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()

    def fetch(self, source, limits):
        if "fixture" not in source:
            raise SourceError("source_unavailable")
        path = (self.root / source["fixture"]).resolve()
        if not path.is_relative_to(self.root):
            raise SourceError("source_unavailable")
        try:
            with path.open("rb") as stream:
                data = stream.read(limits["max_bytes_per_source"] + 1)
        except OSError:
            raise SourceError("source_unavailable") from None
        if len(data) > limits["max_bytes_per_source"]:
            raise SourceError("source_too_large")
        return data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise _RedirectBlocked


class HttpFetcher:
    """Live fetcher: one bounded HTTPS GET per source; no redirects, cookies, auth or retries."""

    mode = "http"

    def __init__(self, opener=None, clock=time.monotonic):
        self.opener = opener or urllib.request.build_opener(_NoRedirect)
        self.clock = clock

    def fetch(self, source, limits):
        try:
            host = check_https_url(source["feed_url"])
        except ValueError:
            raise SourceError("source_unsafe_url") from None
        maximum, timeout = limits["max_bytes_per_source"], limits["timeout_seconds"]
        request = urllib.request.Request(source["feed_url"], method="GET", headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise SourceError("source_http_error")
                try:
                    final_host = check_https_url(response.geturl())
                except ValueError:
                    raise SourceError("source_redirect_blocked") from None
                if final_host != host:
                    raise SourceError("source_redirect_blocked")
                length = response.headers.get("Content-Length") if response.headers else None
                if length is not None and length.strip().isdigit() and int(length) > maximum:
                    raise SourceError("source_too_large")
                return self._read(response, maximum, timeout)
        except SourceError:
            raise
        except _RedirectBlocked:
            raise SourceError("source_redirect_blocked") from None
        except urllib.error.HTTPError:
            raise SourceError("source_http_error") from None
        except (TimeoutError, socket.timeout):
            raise SourceError("source_timeout") from None
        except urllib.error.URLError as error:
            code = "source_timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "source_unavailable"
            raise SourceError(code) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise SourceError("source_unavailable") from None

    def _read(self, response, maximum, timeout):
        deadline = self.clock() + timeout * BODY_DEADLINE_FACTOR
        reader = getattr(response, "read1", None) or response.read
        chunks, total = [], 0
        while True:
            if self.clock() > deadline:
                raise SourceError("source_timeout")
            chunk = reader(READ_CHUNK)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > maximum:
                raise SourceError("source_too_large")
            chunks.append(chunk)


# ---------------------------------------------------------------- parsing

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        elif tag in {"p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1
        elif tag in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def clean_text(raw, limit=None):
    """Plain text from feed HTML: tags/scripts removed, entities decoded, controls dropped."""
    if not raw:
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(raw[:SCAN_CHARS * 4])
        extractor.close()
        text = "".join(extractor.parts)
    except Exception:
        text = ""
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."
    return text


def _text(element, tag):
    found = element.find(tag)
    return (found.text or "") if found is not None else ""


def parse_feed(data):
    """Return raw items [{title, link, published, summary}] from RSS 2.0 or Atom bytes."""
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", data[:SCAN_CHARS * 400], re.IGNORECASE):
        raise SourceError("source_malformed")  # No DTDs: blocks entity-expansion attacks.
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        raise SourceError("source_malformed") from None
    items = []
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise SourceError("source_malformed")
        for item in channel.findall("item"):
            items.append({"title": _text(item, "title"), "link": _text(item, "link").strip(),
                          "published": _text(item, "pubDate").strip(), "summary": _text(item, "description")})
    elif root.tag == ATOM + "feed":
        for entry in root.findall(ATOM + "entry"):
            link = ""
            for candidate in entry.findall(ATOM + "link"):
                if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
                    link = candidate.get("href").strip()
                    break
            published = _text(entry, ATOM + "published") or _text(entry, ATOM + "updated")
            summary = _text(entry, ATOM + "summary") or _text(entry, ATOM + "content")
            items.append({"title": _text(entry, ATOM + "title"), "link": link,
                          "published": published.strip(), "summary": summary})
    else:
        raise SourceError("source_malformed")
    return items


def parse_timestamp(value):
    """Feed date (RFC 822 or ISO 8601) as a UTC 'YYYY-MM-DDTHH:MM:SSZ' string, or None."""
    if not value or len(value) > 64:
        return None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if not 1990 <= moment.year <= 2100:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- candidates

def keyword_patterns(keywords):
    return [(keyword, re.compile(r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])", re.IGNORECASE))
            for keyword in keywords]


def _title_fingerprint(title):
    return _sha256(" ".join(re.findall(r"[a-z0-9]+", title.lower())))


def candidate_id_for(profile, url):
    return "cand-" + _sha256(profile + "\n" + url)[:24]


def _claims(title, text, patterns):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    claims = []
    for sentence in sentences:
        sentence = sentence.strip()
        if MIN_CLAIM_CHARS <= len(sentence) <= CLAIM_CHARS and any(p.search(sentence) for _, p in patterns):
            claims.append({"claim_id": f"k{len(claims) + 1}", "text": sentence, "basis": "excerpt",
                           "status": "unverified"})
        if len(claims) == MAX_CLAIMS:
            break
    if not claims:
        claims.append({"claim_id": "k1", "text": title, "basis": "headline", "status": "unverified"})
    return claims


def build_candidate(item, source, *, profile, patterns, config_sha256, retrieved_at, fetch_mode):
    """Return (candidate, None) or (None, skip_reason) for one raw feed item."""
    try:
        host = check_https_url(item["link"])
    except ValueError:
        return None, "unsafe_link"
    if host not in source["link_hosts"]:
        return None, "unapproved_link_host"
    title = clean_text(item["title"], TITLE_CHARS)
    if not title:
        return None, "missing_title"
    full_text = clean_text(item["summary"], SCAN_CHARS)
    matched = [keyword for keyword, pattern in patterns if pattern.search(title) or pattern.search(full_text)]
    if not matched:
        return None, "not_relevant"
    url = canonical_url(item["link"])
    candidate = {
        "contract": "story_candidate", "version": "1.0",
        "candidate_id": candidate_id_for(profile, url),
        "content_profile": profile,
        "source": {key: source[key] for key in ("source_id", "name", "publisher", "kind", "category")},
        "url": url, "title": title,
        "published_at": parse_timestamp(item["published"]),
        "retrieved_at": retrieved_at,
        "excerpt": clean_text(item["summary"], EXCERPT_CHARS),
        "candidate_claims": _claims(title, full_text, patterns),
        "matched_keywords": matched,
        "fingerprints": {"url_sha256": _sha256(url), "title_sha256": _title_fingerprint(title)},
        "verification": {"status": "unverified", "required_before": "story_brief"},
        "provenance": {"created_by": "scout", "fetch_mode": fetch_mode, "sources_config_sha256": config_sha256},
    }
    try:
        validate_candidate(candidate)
    except NetworkError as error:
        return None, "sensitive_content" if error.code == "sensitive_state" else "invalid_item"
    return candidate, None


def validate_candidate(candidate):
    """Raise NetworkError unless candidate is a well-formed, unverified Story Candidate."""
    try:
        json.dumps(candidate, allow_nan=False)
    except (TypeError, ValueError):
        raise NetworkError("invalid_candidate", "Story Candidate must contain finite JSON values.") from None
    reject_secrets(candidate)
    error = next(_validator(str(CANDIDATE_SCHEMA)).iter_errors(candidate), None)
    if error is not None:
        location = ".".join(str(p) for p in error.absolute_path) or "$"
        raise NetworkError("invalid_candidate", f"Story Candidate rejected at {location}: schema rule '{error.validator}'.")
    try:
        check_https_url(candidate["url"])
        consistent = (canonical_url(candidate["url"]) == candidate["url"]
                      and candidate["candidate_id"] == candidate_id_for(candidate["content_profile"], candidate["url"])
                      and candidate["fingerprints"]["url_sha256"] == _sha256(candidate["url"])
                      and candidate["fingerprints"]["title_sha256"] == _title_fingerprint(candidate["title"]))
    except (ValueError, AttributeError):
        consistent = False
    ids = [claim["claim_id"] for claim in candidate["candidate_claims"]]
    if not consistent or ids != [f"k{i}" for i in range(1, len(ids) + 1)]:
        raise NetworkError("invalid_candidate", "Story Candidate identifiers or fingerprints are inconsistent.")


def run_scout(config, config_sha256, fetcher, *, existing_ids=(), existing_titles=(), clock=utc_now):
    """Read each enabled source once, in order. Returns (candidates, report).

    existing_ids: candidate IDs already stored (cross-run URL deduplication).
    existing_titles: (source_id, title_sha256) pairs already stored (cross-run headline
    deduplication within a source, e.g. syndicated or AMP copies).
    The report holds only IDs, counts and fixed error codes.
    """
    limits, profile = config["limits"], config["profile"]
    patterns = keyword_patterns(config["keywords"])
    seen = set(existing_ids)
    known_titles = set(existing_titles)
    candidates, rows = [], []
    started = clock()
    for source in config["sources"]:
        row = {"source_id": source["source_id"], "status": "disabled", "error_code": None,
               "items_read": 0, "candidates_new": 0, "skipped": {}}
        rows.append(row)
        if not source["enabled"]:
            continue
        if len(candidates) >= limits["max_candidates_per_run"]:
            row["status"] = "skipped_run_limit"
            continue
        retrieved_at = clock()
        try:
            items = parse_feed(fetcher.fetch(source, limits))
        except SourceError as error:
            row.update(status="failed", error_code=error.code)
            continue
        except Exception:
            # Never expose unexpected exception text; it may contain local details.
            row.update(status="failed", error_code="source_error")
            continue
        row["status"] = "ok"
        for item in items[:limits["max_items_per_source"]]:
            row["items_read"] += 1
            if len(candidates) >= limits["max_candidates_per_run"]:
                reason = "run_limit"
            else:
                candidate, reason = build_candidate(
                    item, source, profile=profile, patterns=patterns, config_sha256=config_sha256,
                    retrieved_at=retrieved_at, fetch_mode=fetcher.mode)
                if candidate is not None:
                    title_key = (source["source_id"], candidate["fingerprints"]["title_sha256"])
                    duplicate = candidate["candidate_id"] in seen or title_key in known_titles
                    # Record both fingerprints even for duplicates, so later copies match too.
                    seen.add(candidate["candidate_id"])
                    known_titles.add(title_key)
                    if duplicate:
                        reason = "duplicate"
                    else:
                        candidates.append(candidate)
                        row["candidates_new"] += 1
            if reason:
                row["skipped"][reason] = row["skipped"].get(reason, 0) + 1
    report = {
        "contract": "scout_run", "version": "1.0", "profile": profile,
        "fetch_mode": fetcher.mode, "sources_config_sha256": config_sha256,
        "started_at": started, "finished_at": clock(),
        "limits": deepcopy(limits),
        "sources": rows,
        "totals": {
            "sources_ok": sum(r["status"] == "ok" for r in rows),
            "sources_failed": sum(r["status"] == "failed" for r in rows),
            "sources_disabled": sum(r["status"] == "disabled" for r in rows),
            "sources_skipped": sum(r["status"] == "skipped_run_limit" for r in rows),
            "candidates_new": len(candidates),
            "duplicates": sum(r["skipped"].get("duplicate", 0) for r in rows),
            "items_skipped": sum(sum(r["skipped"].values()) for r in rows),
        },
        "truncated": any(r["skipped"].get("run_limit") or r["status"] == "skipped_run_limit" for r in rows),
        "candidate_ids": [c["candidate_id"] for c in candidates],
        "verified": False,
    }
    reject_secrets(report)
    return candidates, report
