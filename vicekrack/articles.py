"""Bounded article fetching (Step 20): richer evidence for Verification.

For a stored Scout candidate, fetch the candidate's own article page (never other links:
no crawling) and extract plain article sentences as Article Evidence. Fetching is opt-in:
fixture mode reads local files; http mode requires the caller to enable live requests.

Network rules, checked on the first request and again on every redirect hop:
- https only, default port, no login, no query string or fragment;
- host and path prefix explicitly approved for that candidate's source;
- every DNS answer must be a public address; the connection is made to the address that
  was checked (DNS rebinding cannot swap in a private one), with normal TLS verification
  against the hostname;
- bounded redirects, per-operation timeout, whole-article deadline, byte cap, HTML only,
  identity encoding; no cookies or authentication headers; no automatic retries.

Extraction keeps <article>/<main> text, dropping scripts, styles, navigation, headers,
footers, asides, forms, hidden elements and headings, and drops sentences that look like
instructions to an AI. Article Evidence is always `unverified`: the Verifier treats its
sentences as evidence under its existing rules. Errors are fixed codes; raw exceptions,
response bodies and headers are never stored or printed.
"""

import hashlib
import http.client
import json
import re
import socket
import ssl
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html.parser import HTMLParser
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from jsonschema import Draft202012Validator

from .errors import NetworkError
from .persistence import reject_secrets
from .scout import check_https_url
from .verification import INSTRUCTION_TEXT

ROOT = Path(__file__).resolve().parent.parent
POLICY_SCHEMA = ROOT / "schemas/article-sources.schema.json"
EVIDENCE_SCHEMA = ROOT / "schemas/article-evidence.schema.json"
USER_AGENT = "ViceKrack-ArticleFetch/1.0 (bounded evidence intake)"
ALLOWED_TYPES = ("text/html", "application/xhtml+xml")
REDIRECT_CODES = {301, 302, 303, 307, 308}
DEADLINE_FACTOR = 2
READ_CHUNK = 65536
MIN_SENTENCE, MAX_SENTENCE = 20, 300
FEED_DATE_TOLERANCE = timedelta(days=1)
JSON_LD_LIMIT = 50_000

SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg",
             "template", "button", "select", "textarea", "figure", "canvas", "object", "embed", "dialog"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = {"p", "li", "blockquote", "div", "section", "br", "tr", "td", "dd", "dt"} | HEADING_TAGS
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
             "track", "wbr"}
DATE_ORIGINS = ("meta_article_published_time", "json_ld_date_published", "time_element")


class ArticleError(Exception):
    """A fixed, safe failure code for one article. Never carries raw diagnostics."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@lru_cache(maxsize=None)
def _validator(path):
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _parse(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now():
    return _stamp(datetime.now(timezone.utc))


# ---------------------------------------------------------------- policy

def validate_policy(policy, root=ROOT):
    reject_secrets(policy)
    error = next(_validator(str(POLICY_SCHEMA)).iter_errors(policy), None)

    def fail(reason):
        raise NetworkError("invalid_article_policy", f"Article fetch policy rejected: {reason}.")

    if error is not None:
        fail(f"schema rule '{error.validator}' at {'.'.join(str(p) for p in error.absolute_path) or '$'}")
    ids = [s["source_id"] for s in policy["sources"]]
    if len(ids) != len(set(ids)):
        fail("duplicate source_id")
    fixtures = policy.get("fixtures", {})
    if policy["fetch_mode"] == "http" and fixtures:
        fail("fixtures are only allowed in fixture mode")
    root = Path(root).resolve()
    for url, path in fixtures.items():
        try:
            check_https_url(url)
        except ValueError:
            fail("fixture URLs must be public https URLs")
        if not (root / path).resolve().is_relative_to(root):
            fail("fixture files must remain inside the project")


def load_policy(path, root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise NetworkError("invalid_article_policy", "Article fetch policy must remain inside the project.")
    try:
        policy = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise NetworkError("invalid_article_policy", "Cannot read a valid article fetch policy.") from None
    validate_policy(policy, root)
    return policy


def source_rule(policy, source_id):
    return next((s for s in policy["sources"] if s["source_id"] == source_id and s["enabled"]), None)


# ---------------------------------------------------------------- URL and address safety

def check_article_url(url, rule):
    """Return (host, path) if url is an approved article URL for this source rule."""
    try:
        host = check_https_url(url)
    except ValueError:
        raise ArticleError("article_blocked_url") from None
    parts = urlsplit(url)
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ArticleError("article_blocked_url")
    if host not in rule["hosts"]:
        raise ArticleError("article_blocked_host")
    path = parts.path or "/"
    if "/../" in path + "/" or "/./" in path + "/" or "\\" in path or "%2e" in path.lower() or "%2f" in path.lower():
        raise ArticleError("article_blocked_url")
    if not any(path.startswith(prefix) for prefix in rule["path_prefixes"]):
        raise ArticleError("article_blocked_path")
    return host, path


def is_public_address(value):
    try:
        address = ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_reserved


def resolve_public(host, resolver=socket.getaddrinfo):
    """All DNS answers must be public; returns the first. Blocks private/local networks."""
    try:
        answers = resolver(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise ArticleError("article_unavailable") from None
    addresses = [answer[4][0] for answer in answers if answer and len(answer) > 4]
    if not addresses:
        raise ArticleError("article_unavailable")
    if not all(is_public_address(address) for address in addresses):
        raise ArticleError("article_blocked_network")
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to an already validated IP, verifying TLS against the hostname (SNI)."""

    def __init__(self, host, address, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


# ---------------------------------------------------------------- fetchers

class FixtureArticleFetcher:
    """Offline: same URL checks, then reads the policy's local fixture file."""

    mode = "fixture"

    def __init__(self, policy, root=ROOT):
        self.policy, self.root = policy, Path(root).resolve()

    def fetch(self, url, rule, limits):
        check_article_url(url, rule)
        relative = self.policy.get("fixtures", {}).get(url)
        if relative is None:
            raise ArticleError("article_unavailable")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ArticleError("article_unavailable")
        try:
            with path.open("rb") as stream:
                body = stream.read(limits["max_bytes_per_article"] + 1)
        except OSError:
            raise ArticleError("article_unavailable") from None
        if len(body) > limits["max_bytes_per_article"]:
            raise ArticleError("article_too_large")
        return {"final_url": url, "redirects": [], "content_type": "text/html", "charset": "utf-8", "body": body}


class HttpArticleFetcher:
    """Live: one bounded GET per hop, revalidating every redirect. No retries."""

    mode = "http"

    def __init__(self, resolver=socket.getaddrinfo, connection_factory=PinnedHTTPSConnection, clock=time.monotonic):
        self.resolver, self.connection_factory, self.clock = resolver, connection_factory, clock

    def fetch(self, url, rule, limits):
        timeout = limits["timeout_seconds"]
        deadline = self.clock() + timeout * DEADLINE_FACTOR
        current, redirects, seen = url, [], {url}
        while True:
            host, path = check_article_url(current, rule)
            address = resolve_public(host, self.resolver)
            if self.clock() > deadline:
                raise ArticleError("article_timeout")
            connection = None
            try:
                connection = self.connection_factory(host, address, timeout)
                connection.request("GET", path, headers={
                    "Host": host, "User-Agent": USER_AGENT, "Accept": "text/html, application/xhtml+xml;q=0.9",
                    "Accept-Encoding": "identity", "Connection": "close"})
                response = connection.getresponse()
                status = response.status
                if status in REDIRECT_CODES:
                    location = response.getheader("Location")
                    if not location or len(redirects) >= limits["max_redirects"]:
                        raise ArticleError("article_redirect_blocked")
                    target = urljoin(current, location.strip())
                    if target in seen:
                        raise ArticleError("article_redirect_blocked")
                    try:
                        check_article_url(target, rule)
                    except ArticleError:
                        raise ArticleError("article_redirect_blocked") from None
                    seen.add(target)
                    redirects.append(target)
                    current = target
                    continue
                if status in (404, 410):
                    raise ArticleError("article_unavailable")
                if status != 200:
                    raise ArticleError("article_http_error")
                content_type, charset = _content_type(response.getheader("Content-Type"))
                if content_type not in ALLOWED_TYPES:
                    raise ArticleError("article_unsupported_type")
                encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise ArticleError("article_unsupported_type")
                length = response.getheader("Content-Length")
                if length and length.strip().isdigit() and int(length) > limits["max_bytes_per_article"]:
                    raise ArticleError("article_too_large")
                body = self._read(response, limits["max_bytes_per_article"], deadline)
                return {"final_url": current, "redirects": redirects, "content_type": content_type,
                        "charset": charset, "body": body}
            except ArticleError:
                raise
            except (TimeoutError, socket.timeout):
                raise ArticleError("article_timeout") from None
            except ssl.SSLError:
                raise ArticleError("article_unavailable") from None
            except (OSError, http.client.HTTPException, ValueError):
                raise ArticleError("article_unavailable") from None
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass

    def _read(self, response, maximum, deadline):
        chunks, total = [], 0
        while True:
            if self.clock() > deadline:
                raise ArticleError("article_timeout")
            chunk = response.read(READ_CHUNK)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > maximum:
                raise ArticleError("article_too_large")
            chunks.append(chunk)


def _content_type(header):
    if not header:
        return "", None
    parts = [p.strip() for p in header.split(";")]
    charset = next((p.split("=", 1)[1].strip().strip('"').lower() for p in parts[1:]
                    if p.lower().startswith("charset=")), None)
    return parts[0].lower(), charset


# ---------------------------------------------------------------- extraction

class _ArticleParser(HTMLParser):
    """Collects text blocks by region, page date metadata and title. Never runs scripts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # (tag, skipped)
        self.skip = 0
        self.regions = {"article": [], "main": [], "body": []}
        self.depth = {"article": 0, "main": 0}
        self.current = []
        self.title_parts, self.in_title = [], False
        self.ld_blocks, self.in_ld = [], False
        self.dates = []          # (origin, value)

    def _flush(self):
        text = " ".join(self.current).strip()
        self.current = []
        if not text:
            return
        self.regions["body"].append(text)
        for name in ("article", "main"):
            if self.depth[name]:
                self.regions[name].append(text)

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
            if key in ("article:published_time", "datepublished"):
                origin = "meta_article_published_time" if key == "article:published_time" else "time_element"
                self.dates.append((origin, attrs.get("content", "")))
            return
        if tag == "time" and ("pubdate" in attrs or attrs.get("itemprop", "").lower() == "datepublished"):
            self.dates.append(("time_element", attrs.get("datetime", "")))
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.in_ld = True
            self.ld_blocks.append([])  # Each JSON-LD block is parsed on its own.
        if tag == "title":
            self.in_title = True
        if tag in VOID_TAGS:
            if tag == "br":
                self._flush()
            return
        if tag in self.depth:
            self._flush()  # Region boundary: text never leaks across <article>/<main> edges.
        style = attrs.get("style", "").replace(" ", "").lower()
        hidden = ("hidden" in attrs or attrs.get("aria-hidden", "").lower() == "true"
                  or "display:none" in style or "visibility:hidden" in style)
        skipped = tag in SKIP_TAGS or tag in HEADING_TAGS or hidden
        if tag in BLOCK_TAGS:
            self._flush()
        self.stack.append((tag, skipped))
        if skipped:
            self.skip += 1
        if tag in self.depth:
            self.depth[tag] += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_ld = False
        if tag == "title":
            self.in_title = False
        if not any(name == tag for name, _ in self.stack):
            return
        if tag in BLOCK_TAGS or tag in self.depth:
            self._flush()
        while self.stack:
            name, skipped = self.stack.pop()
            if skipped:
                self.skip -= 1
            if name in self.depth:
                self.depth[name] -= 1
            if name == tag:
                break

    def handle_data(self, data):
        if self.in_ld:
            if self.ld_blocks and sum(len(p) for block in self.ld_blocks for p in block) < JSON_LD_LIMIT:
                self.ld_blocks[-1].append(data)
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        if not self.skip:
            self.current.append(data)

    def close(self):
        super().close()
        self._flush()


def _clean(text):
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    return re.sub(r"\s+", " ", text).strip()


def _json_ld_dates(raw):
    """datePublished values from JSON-LD data (parsed as data, never executed)."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return []
    found, stack, steps = [], [data], 0
    while stack and steps < 500:
        steps += 1
        node = stack.pop()
        if isinstance(node, dict):
            value = node.get("datePublished")
            if isinstance(value, str):
                found.append(value)
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _precise_utc(value):
    """UTC 'YYYY-MM-DDTHH:MM:SSZ' for a full ISO timestamp with timezone, else None."""
    value = (value or "").strip()
    if "T" not in value or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None or not 1990 <= moment.year <= 2100:
        return None
    return _stamp(moment)


def resolve_publication(page_dates, feed_published, fetched_at):
    """(published_at, origin). Uses page metadata or the feed date; never the fetch time."""
    precise = [(origin, _precise_utc(value)) for origin, value in page_dates if value]
    valid = [(origin, stamp) for origin, stamp in precise if stamp]
    fetched = _parse(fetched_at)
    if valid:
        values = {stamp for _, stamp in valid}
        if len(values) > 1:
            return None, "ambiguous"
        stamp = values.pop()
        if _parse(stamp) > fetched:
            return None, "future"
        if feed_published and abs(_parse(stamp) - _parse(feed_published)) > FEED_DATE_TOLERANCE:
            return None, "conflicts_with_feed"
        origin = min((o for o, _ in valid), key=DATE_ORIGINS.index)
        return stamp, origin
    if precise:
        return None, "imprecise"
    if feed_published and _parse(feed_published) <= fetched:
        return feed_published, "feed"
    return None, "unavailable"


def extract_article(body, charset, *, limits):
    """Return extraction dict: sentences, excluded counts, truncated, page dates, title."""
    try:
        text = body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    parser = _ArticleParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        raise ArticleError("article_malformed") from None
    blocks = parser.regions["article"] or parser.regions["main"] or parser.regions["body"]
    dates = list(parser.dates) + [("json_ld_date_published", value) for block in parser.ld_blocks
                                  for value in _json_ld_dates("".join(block))]
    sentences, excluded, truncated, total = [], {"instruction_like": 0, "too_long": 0}, False, 0
    for block in blocks:
        for sentence in re.split(r"(?<=[.!?])\s+", _clean(block)):
            sentence = sentence.strip()
            if len(sentence) < MIN_SENTENCE:
                continue
            if INSTRUCTION_TEXT.search(sentence):
                excluded["instruction_like"] += 1
                continue
            if len(sentence) > MAX_SENTENCE:
                excluded["too_long"] += 1
                continue
            if sentence in sentences:
                continue
            if len(sentences) >= limits["max_sentences_per_article"] or total + len(sentence) > limits["max_text_chars"]:
                truncated = True
                break
            sentences.append(sentence)
            total += len(sentence)
        if truncated:
            break
    title = _clean("".join(parser.title_parts))[:200] or None
    return {"sentences": sentences, "excluded": excluded, "truncated": truncated, "dates": dates, "title": title}


# ---------------------------------------------------------------- evidence

def article_id_for(candidate_id, content_sha256):
    return "art-" + hashlib.sha256(f"{candidate_id}\n{content_sha256}".encode()).hexdigest()[:24]


def build_evidence(candidate, fetched, *, fetched_at, fetch_mode, limits):
    extraction = extract_article(fetched["body"], fetched["charset"], limits=limits)
    if not extraction["sentences"]:
        raise ArticleError("article_no_text")
    published, origin = resolve_publication(extraction["dates"], candidate["published_at"], fetched_at)
    content_sha = _sha256_bytes(fetched["body"])
    evidence = {
        "contract": "article_evidence", "version": "1.0",
        "article_id": article_id_for(candidate["candidate_id"], content_sha),
        "candidate_id": candidate["candidate_id"], "content_profile": candidate["content_profile"],
        "source": dict(candidate["source"]),
        "requested_url": candidate["url"], "final_url": fetched["final_url"], "redirects": list(fetched["redirects"]),
        "fetched_at": fetched_at, "fetch_mode": fetch_mode, "http_status": 200,
        "content_type": fetched["content_type"], "content_sha256": content_sha,
        "text_sha256": _sha256_bytes("\n".join(extraction["sentences"]).encode("utf-8")),
        "published_at": published, "published_at_origin": origin, "page_title": extraction["title"],
        "sentences": extraction["sentences"], "excluded": extraction["excluded"],
        "truncated": extraction["truncated"], "verification": {"status": "unverified"},
    }
    try:
        validate_evidence(evidence)
    except NetworkError as error:
        raise ArticleError("article_sensitive_content" if error.code == "sensitive_state" else "article_malformed") from None
    return evidence


def validate_evidence(evidence):
    """Schema + consistency checks. Article Evidence is only ever unverified."""
    try:
        json.dumps(evidence, allow_nan=False)
    except (TypeError, ValueError):
        raise NetworkError("invalid_article_evidence", "Article Evidence must contain finite JSON values.") from None
    reject_secrets(evidence)
    error = next(_validator(str(EVIDENCE_SCHEMA)).iter_errors(evidence), None)
    if error is not None:
        location = ".".join(str(p) for p in error.absolute_path) or "$"
        raise NetworkError("invalid_article_evidence", f"Article Evidence rejected at {location}: schema rule '{error.validator}'.")
    if evidence["article_id"] != article_id_for(evidence["candidate_id"], evidence["content_sha256"]):
        raise NetworkError("invalid_article_evidence", "Article Evidence ID does not match its content.")
    if evidence["text_sha256"] != _sha256_bytes("\n".join(evidence["sentences"]).encode("utf-8")):
        raise NetworkError("invalid_article_evidence", "Article Evidence text hash does not match its sentences.")
    if evidence["published_at"] is not None and _parse(evidence["published_at"]) > _parse(evidence["fetched_at"]):
        raise NetworkError("invalid_article_evidence", "Publication date cannot be after the fetch time.")
    if (evidence["published_at"] is None) != (evidence["published_at_origin"] not in DATE_ORIGINS + ("feed",)):
        raise NetworkError("invalid_article_evidence", "Publication date and its origin are inconsistent.")
    if any(INSTRUCTION_TEXT.search(s) for s in evidence["sentences"]):
        raise NetworkError("invalid_article_evidence", "Article Evidence contains instruction-like text.")
    if evidence["final_url"] != (evidence["redirects"][-1] if evidence["redirects"] else evidence["requested_url"]):
        raise NetworkError("invalid_article_evidence", "Final URL does not match the redirect chain.")


def fetch_for_candidates(candidates, policy, fetcher, *, clock=utc_now):
    """Fetch articles for candidates in order, within the run limit. Returns (evidence list, rows)."""
    limits = policy["limits"]
    results, rows, attempts = [], [], 0
    for candidate in candidates:
        row = {"candidate_id": candidate["candidate_id"], "source_id": candidate["source"]["source_id"],
               "status": "failed", "code": None, "article_id": None}
        rows.append(row)
        if candidate["content_profile"] != policy["profile"]:
            row["code"] = "article_profile_mismatch"
            continue
        rule = source_rule(policy, candidate["source"]["source_id"])
        if rule is None:
            row.update(status="skipped", code="article_source_not_approved")
            continue
        if attempts >= limits["max_articles_per_run"]:
            row.update(status="skipped", code="article_run_limit")
            continue
        attempts += 1  # Every attempt counts, successful or not; there are no retries.
        fetched_at = clock()
        try:
            fetched = fetcher.fetch(candidate["url"], rule, limits)
            evidence = build_evidence(candidate, fetched, fetched_at=fetched_at, fetch_mode=fetcher.mode, limits=limits)
        except ArticleError as error:
            row["code"] = error.code
            continue
        except Exception:
            row["code"] = "article_error"  # Never expose unexpected exception text.
            continue
        row.update(status="fetched", article_id=evidence["article_id"])
        results.append(evidence)
    return results, rows
