"""Verification (Step 17): deterministic claim checking between Scout and Story Brief.

Inputs are stored Story Candidates (untrusted feed text) and an auditable policy that says
which sources are primary (first-party official) and which are reputable secondary press.
For each candidate claim the Verifier collects matching and conflicting statements from all
stored candidates and decides a status by fixed rules:

  verified               a primary source states it first-hand, on its official host,
                         and no first-hand primary statement contradicts it
  corroborated           >= N independent origins of reputable secondary support, no
                         primary evidence and no reputable contradiction (draft-only)
  disputed               reputable evidence both supports and contradicts it
  rejected               contradicted by a primary source (or by >= N independent
                         reputable origins) with no primary support
  insufficient_evidence  anything else; the safe default

Never verified because one page says it, because many pages repeat one origin, because a
headline implies it, because the Scout collected it, or because a model says so (no model
is used). Statements that look like instructions to an AI are excluded from evidence and
never executed. Records are re-checked by replaying these rules over their stored evidence.
"""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .errors import NetworkError
from .persistence import reject_secrets
from .scout import validate_candidate


ROOT = Path(__file__).resolve().parent.parent
POLICY_SCHEMA = ROOT / "schemas/verification-policy.schema.json"
RECORD_SCHEMA = ROOT / "schemas/verification-record.schema.json"
STATUSES = ("verified", "corroborated", "disputed", "insufficient_evidence", "rejected")
# Record versions: 1.0 = Step 17 rules; 1.1 adds dated official supersession (Step 19).
RECORD_VERSION = "1.1"
SAME_FACT_MIN_OVERLAP = 0.75             # Each statement must share >= 75% of the other's subject words.
SUPERSESSION_MIN_GAP = timedelta(hours=1)  # Closer publication times are treated as ambiguous.
SUPERSESSION_REASON = "newer_first_hand_statement_same_source"

STATEMENT_CHARS, MIN_STATEMENT_CHARS, MAX_STATEMENTS_PER_CANDIDATE = 300, 20, 12
UNNAMED_ORIGIN = "unnamed sources"

STOPWORDS = frozenset("""a an the and or but if of to in on at by for with from as is are was were be been being
it its this that these those has have had will would can could should may might do does did into about over
after before than then so such also just only says said say says will its their there here which who whom what
when where while new""".split())
# Reporting verbs describe who said something, not what was said; ignore them when matching.
REPORTING_WORDS = frozenset("""confirm confirms confirmed confirming announce announces announced announcing
reveal reveals revealed report reports reported reporting according state states stated statement official
officially today""".split())
MONTHS = frozenset("""january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec spring summer fall autumn winter""".split())
NEGATIONS = frozenset("""not no never deny denies denied false untrue debunked isn aren wasn weren won doesn didn
cannot cant without""".split())

INSTRUCTION_TEXT = re.compile(
    r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier|system)\s+"
    r"(instructions?|prompts?|rules|messages?)"
    r"|\bsystem\s+prompt\b"
    r"|\b(mark|treat|label|classify|set)\s+(this|these|it|the\s+claims?|all\s+claims?|status)\s+(as\s+|to\s+)?"
    r"(verified|true|confirmed|approved)\b"
    r"|\byou\s+are\s+(now\s+)?(an?|the)\s+(ai|assistant|language\s+model|verifier)\b"
    r"|(^|\s)(assistant|system|developer)\s*:",
    re.IGNORECASE)
NAMED_ATTRIBUTION = re.compile(
    # Trigger phrase is case-insensitive; the cited name must be Capitalized words.
    r"\b(?i:according to|reported by|as reported by|first reported by|reports? from|citing|via|per)\s+"
    r"(?:(?i:the)\s+)?([A-Z][A-Za-z0-9&'.-]*(?:\s+[A-Z][A-Za-z0-9&'.-]*){0,3})")
UNNAMED_ATTRIBUTION = re.compile(
    r"\b(?:according to|reported by|citing|per|via|from)\s+(?:an?\s+|the\s+|some\s+|several\s+|multiple\s+)?"
    r"(?:anonymous\s+|unnamed\s+|industry\s+)?(?:insiders?|sources?|leakers?|leaks?|rumou?rs?|tipsters?|reports?)\b"
    r"|\b(?:reportedly|allegedly|rumou?red|purportedly)\b", re.IGNORECASE)

RATIONALE = {
    "verified": "A primary source states this first-hand on its official host; no first-hand primary statement contradicts it.",
    "corroborated": "Independent reputable secondary origins support this, but no primary source confirms it. Draft use only.",
    "disputed": "Reputable evidence both supports and contradicts this claim.",
    "rejected": "Contradicted by stronger evidence and not confirmed by any primary source.",
    "insufficient_evidence": "Not enough independent, reputable evidence to decide; treated as unconfirmed.",
}


@lru_cache(maxsize=None)
def _validator(path):
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- policy

def _policy_fail(location, reason):
    raise NetworkError("invalid_verification_policy", f"Verification policy rejected at {location}: {reason}.")


def validate_policy(policy):
    reject_secrets(policy)
    error = next(_validator(str(POLICY_SCHEMA)).iter_errors(policy), None)
    if error is not None:
        _policy_fail(".".join(str(p) for p in error.absolute_path) or "$", f"schema rule '{error.validator}'")
    seen = set()
    for group in ("primary_sources", "secondary_sources"):
        for index, rule in enumerate(policy[group]):
            if rule["source_id"] in seen:
                _policy_fail(f"{group}[{index}].source_id", "a source may appear in only one tier, once")
            seen.add(rule["source_id"])


def load_policy(path, root=ROOT):
    """Load a policy file inside the project. Returns (policy, sha256)."""
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        _policy_fail("$", "the file must remain inside the project")
    try:
        policy = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        _policy_fail("$", "cannot read valid JSON")
    validate_policy(policy)
    return policy, _sha256(_canonical(policy))


def tier_for(source_id, url, policy):
    """primary / secondary / unrated, decided only by the policy (source ID + exact host).

    The candidate's own declared kind is ignored, so a mislabeled or edited candidate
    cannot promote itself; replaying a stored record yields the same tier.
    """
    host = (urlsplit(url).hostname or "").lower()
    for rule in policy["primary_sources"]:
        if rule["source_id"] == source_id and host in rule["hosts"]:
            return "primary"
    for rule in policy["secondary_sources"]:
        if rule["source_id"] == source_id and host in rule["hosts"]:
            return "secondary"
    return "unrated"


# ---------------------------------------------------------------- text analysis

def normalize_name(name, aliases=None):
    text = re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()
    text = re.sub(r"^the ", "", text)
    return (aliases or {}).get(text, text)


def _tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def analyze(text):
    """Deterministic features of a sentence used for matching."""
    tokens = _tokens(text)
    content = {t for t in tokens if (len(t) > 1 or t.isdigit()) and t not in STOPWORDS
               and t not in NEGATIONS and t not in REPORTING_WORDS}
    facts = {t for t in content if t.isdigit() or t in MONTHS}
    return {"content": content, "facts": facts, "topic": content - facts,
            "negated": bool(NEGATIONS.intersection(tokens)),
            "instructions": bool(INSTRUCTION_TEXT.search(text))}


def attribution(text, aliases):
    """(origin name or None, unnamed?) for a statement's explicit attribution."""
    named = NAMED_ATTRIBUTION.search(text)
    if named:
        return normalize_name(named.group(1), aliases), False
    if UNNAMED_ATTRIBUTION.search(text):
        return UNNAMED_ORIGIN, True
    return None, False


def is_first_hand(statement, publisher, aliases):
    origin, unnamed = attribution(statement, aliases)
    return not unnamed and (origin is None or origin == normalize_name(publisher, aliases))


def relation(claim, statement, threshold):
    """'supports', 'contradicts' or None between two analyzed sentences."""
    topic = claim["topic"]
    if not topic:
        return None  # Bare numbers/dates say nothing about a subject.
    other = statement["topic"] or statement["content"]
    overlap = len(topic & other) / len(topic)
    if overlap < threshold:
        return None
    if claim["negated"] != statement["negated"]:
        return "contradicts"
    if claim["facts"] - statement["facts"] and statement["facts"] - claim["facts"]:
        return "contradicts"  # Same subject, different dates/numbers.
    if claim["facts"] <= statement["facts"]:
        return "supports"
    return None  # Statement lacks the claim's key facts: not evidence either way.


def _jaccard(left, right):
    return len(left & right) / len(left | right) if left | right else 0.0


def statements_for(candidate):
    """Excerpt sentences and excerpt-based claims; headlines are never evidence."""
    texts = [c["text"] for c in candidate["candidate_claims"] if c["basis"] == "excerpt"]
    texts += [s.strip() for s in re.split(r"(?<=[.!?])\s+", candidate["excerpt"])]
    unique, seen = [], set()
    for text in texts:
        key = " ".join(_tokens(text))
        if MIN_STATEMENT_CHARS <= len(text) <= STATEMENT_CHARS and key not in seen:
            seen.add(key)
            unique.append(text)
        if len(unique) == MAX_STATEMENTS_PER_CANDIDATE:
            break
    return unique


# ---------------------------------------------------------------- origins

class _Origins:
    """Union-find over organization names: citations and copied text merge origins."""

    def __init__(self):
        self.parent = {}

    def find(self, name):
        self.parent.setdefault(name, name)
        while self.parent[name] != name:
            self.parent[name] = self.parent[self.parent[name]]
            name = self.parent[name]
        return name

    def union(self, child, root):
        """Merge child's cluster into root's; root's representative names the origin."""
        a, b = self.find(child), self.find(root)
        if a != b:
            self.parent[a] = b

    def copy(self):
        clone = _Origins()
        clone.parent = dict(self.parent)
        return clone


def _cycle_nodes(edges):
    """Organizations that take part in a citation cycle (A cites B ... cites A)."""
    graph = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
    cyclic = set()
    for start in sorted(graph):
        stack, seen = [start], set()
        while stack:
            node = stack.pop()
            for nxt in graph.get(node, ()):
                if nxt == start:
                    cyclic.add(start)
                elif nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
    return cyclic


# ---------------------------------------------------------------- supersession (Step 19)

def _parse_time(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def same_fact_conflict(left_text, right_text):
    """True when two statements are about the same fact and give clearly different values.

    Same fact: each shares >= SAME_FACT_MIN_OVERLAP of the other's subject words. Clear
    conflict: both state dates/numbers, each has values the other lacks, and neither is
    negated (negation-only conflicts are too ambiguous to order).
    """
    left, right = analyze(left_text), analyze(right_text)
    if left["negated"] or right["negated"] or left["instructions"] or right["instructions"]:
        return False
    if not (left["facts"] - right["facts"] and right["facts"] - left["facts"]):
        return False
    shared = left["topic"] & right["topic"]
    if not left["topic"] or not right["topic"]:
        return False
    return (len(shared) / len(left["topic"]) >= SAME_FACT_MIN_OVERLAP
            and len(shared) / len(right["topic"]) >= SAME_FACT_MIN_OVERLAP)


def _trusted_published(item):
    """Publication time only (never retrieval time); None when missing or implausible."""
    if not item.get("published_at"):
        return None
    published = _parse_time(item["published_at"])
    if published > _parse_time(item["retrieved_at"]):
        return None  # Claims to be published after we collected it: not trustworthy.
    return published


def supersessions(evidence):
    """Map evidence index -> superseded_by entry, for dated same-source official updates.

    Only first-hand primary statements from the same source take part. Within one source,
    if any same-fact conflicting pair lacks a trustworthy publication date or is closer
    than SUPERSESSION_MIN_GAP, nothing from that source is superseded (ambiguous order).
    Otherwise each statement is superseded by the newest conflicting newer statement.
    Different official sources never supersede each other.
    """
    official = [i for i, e in enumerate(evidence) if e["tier"] == "primary" and e["first_hand"]]
    groups = {}
    for index in official:
        item = evidence[index]
        groups.setdefault((item["source_id"], item["publisher"]), []).append(index)
    result = {}
    for members in groups.values():
        pairs = [(a, b) for n, a in enumerate(members) for b in members[n + 1:]
                 if same_fact_conflict(evidence[a]["statement"], evidence[b]["statement"])]
        dates = {i: _trusted_published(evidence[i]) for i in members}
        if any(dates[a] is None or dates[b] is None or abs(dates[a] - dates[b]) < SUPERSESSION_MIN_GAP
               for a, b in pairs):
            continue  # Ambiguous ordering: keep the existing disputed behavior for this source.
        for older in members:
            newer = [n for a, b in pairs for n, o in ((a, b), (b, a)) if o == older and dates[n] > dates[older]]
            if newer:
                winner = min(newer, key=lambda n: (-dates[n].timestamp(), n))
                result[older] = {"evidence_index": winner, "candidate_id": evidence[winner]["candidate_id"],
                                 "published_at": evidence[winner]["published_at"], "reason": SUPERSESSION_REASON}
    return result


# ---------------------------------------------------------------- decision

def decide(evidence, min_origins, claim_has_instructions=False):
    """Pure decision over evidence items. Returns (status, codes, primary_support, origins).

    Evidence marked `superseded_by` (record 1.1) is excluded from contradiction counting
    only; superseded support is still counted and flagged.
    """
    if claim_has_instructions:
        return "insufficient_evidence", ["claim_contains_instructions"], 0, 0
    reputable = ("primary", "secondary")
    support = [e for e in evidence if e["relation"] == "supports"]
    contra = [e for e in evidence if e["relation"] == "contradicts" and "superseded_by" not in e]
    superseded_contra = [e for e in evidence if e["relation"] == "contradicts" and "superseded_by" in e]
    primary_support = [e for e in support if e["tier"] == "primary" and e["first_hand"]]
    primary_contra = [e for e in contra if e["tier"] == "primary" and e["first_hand"]]
    support_origins = {e["origin"] for e in support if e["tier"] in reputable}
    secondary_origins = {e["origin"] for e in support if e["tier"] in reputable and e not in primary_support}
    contra_origins = {e["origin"] for e in contra if e["tier"] in reputable}
    codes = []
    if primary_support and not primary_contra:
        status = "verified"
        codes.append("primary_first_hand_support")
        if contra_origins:
            codes.append("secondary_contradiction")
    elif primary_contra and not primary_support:
        status = "rejected"
        codes.append("primary_contradiction")
    elif primary_support and primary_contra:
        status = "disputed"
        codes += ["primary_first_hand_support", "primary_contradiction"]
    elif contra_origins and secondary_origins:
        status = "disputed"
        codes.append("conflicting_secondary_reports")
    elif len(contra_origins) >= min_origins:
        status = "rejected"
        codes.append("secondary_contradiction")
    elif contra_origins:
        status = "insufficient_evidence"
        codes.append("secondary_contradiction")
    elif len(secondary_origins) >= min_origins:
        status = "corroborated"
        codes.append("independent_secondary_origins")
    elif secondary_origins:
        status = "insufficient_evidence"
        codes.append("single_origin_only")
        if len({e["publisher"] for e in support if e["tier"] in reputable}) > 1:
            codes.append("repeated_single_origin")
    elif support:
        status = "insufficient_evidence"
        codes.append("unrated_sources_only")
    else:
        status = "insufficient_evidence"
        codes.append("no_matching_evidence")
    if status != "verified" and any(e["tier"] == "primary" and not e["first_hand"] for e in support):
        codes.append("secondhand_primary_report")
    if superseded_contra:
        codes.append("older_official_contradiction_superseded")
    if any("superseded_by" in e for e in primary_support):
        codes.append("official_support_superseded")
    return status, codes, len(primary_support), len(support_origins)


# ---------------------------------------------------------------- verification

def _pool_digest(pool):
    return _sha256(_canonical(sorted((c["candidate_id"], _sha256(_canonical(c))) for c in pool)))


def verify_candidate(candidate, pool, policy, policy_sha256, *, clock=utc_now):
    """Return a validated Verification Record for candidate, using pool as evidence.

    pool: stored Story Candidates (the target is added if missing). Invalid candidates
    raise invalid_candidate; nothing in candidate text is ever executed or obeyed.
    """
    validate_candidate(candidate)
    if candidate["content_profile"] != policy["profile"]:
        raise NetworkError("profile_mismatch", "Candidate and verification policy use different profiles.")
    rules, aliases = policy["rules"], policy["attribution_aliases"]
    for item in pool:
        validate_candidate(item)  # Every pool entry, before any deduplication.
    by_id = {c["candidate_id"]: c for c in pool if c["content_profile"] == policy["profile"]}
    if by_id.get(candidate["candidate_id"], candidate) != candidate:
        raise NetworkError("invalid_candidate", "Two different candidates share one candidate_id.")
    by_id[candidate["candidate_id"]] = candidate
    ordered = sorted(by_id)
    flags = set()
    if len(ordered) > rules["max_candidates"]:
        flags.add("pool_truncated")
        kept = [cid for cid in ordered if cid != candidate["candidate_id"]][:rules["max_candidates"] - 1]
        ordered = sorted(kept + [candidate["candidate_id"]])
    members = [by_id[cid] for cid in ordered]

    # Citation graph over the whole pool: who attributes reporting to whom.
    origins, edges, analyzed = _Origins(), set(), {}
    for member in members:
        publisher = normalize_name(member["source"]["publisher"], aliases)
        origins.find(publisher)
        rows = []
        for text in statements_for(member):
            features = analyze(text)
            source, _ = attribution(text, aliases)
            if features["instructions"]:
                flags.add("instruction_like_text_excluded")
                continue  # Untrusted text that addresses an AI is never evidence.
            if source and source != publisher:
                edges.add((publisher, source))
                origins.union(publisher, source)
            rows.append((text, features, source))
        analyzed[member["candidate_id"]] = (publisher, rows)
    cyclic = _cycle_nodes(edges)

    claims, summary = [], dict.fromkeys(STATUSES, 0)
    for claim in candidate["candidate_claims"]:
        target = analyze(claim["text"])
        evidence, seen = [], {}
        for member in members:
            publisher, rows = analyzed[member["candidate_id"]]
            tier = tier_for(member["source"]["source_id"], member["url"], policy)
            for text, features, source in rows:
                found = relation(target, features, rules["match_threshold"])
                if not found:
                    continue
                key = (publisher, found, " ".join(sorted(features["content"])))
                if key in seen:
                    flags.add("duplicate_evidence_collapsed")
                    kept = evidence[seen[key]]
                    # Keep the newest dated copy so a repeated statement keeps its latest date.
                    if member["published_at"] and (kept["published_at"] is None
                                                   or member["published_at"] > kept["published_at"]):
                        evidence[seen[key]] = None
                        seen[key] = len(evidence)
                    else:
                        continue
                else:
                    seen[key] = len(evidence)
                evidence.append({
                    "candidate_id": member["candidate_id"], "source_id": member["source"]["source_id"],
                    "publisher": member["source"]["publisher"], "url": member["url"],
                    "title": member["title"], "tier": tier,
                    "relation": found, "statement": text,
                    "first_hand": is_first_hand(text, member["source"]["publisher"], aliases),
                    "origin": source or publisher, "published_at": member["published_at"],
                    "retrieved_at": member["retrieved_at"], "_features": features})
        evidence = [item for item in evidence if item is not None]
        # Near-identical wording from different publishers is one origin (copied/syndicated).
        local = origins.copy()
        for i, left in enumerate(evidence):
            for right in evidence[i + 1:]:
                if (left["publisher"] != right["publisher"] and _jaccard(
                        left["_features"]["content"], right["_features"]["content"]) >= rules["near_duplicate_threshold"]):
                    names = sorted([normalize_name(left["publisher"], aliases), normalize_name(right["publisher"], aliases)])
                    local.union(names[1], names[0])  # Deterministic: copied text joins the alphabetically first.
        circular = False
        for item in evidence:
            node = normalize_name(item["origin"], aliases)
            circular |= node in cyclic or local.find(node) in cyclic
            item["origin"] = local.find(node)
            del item["_features"]
        rank = {"primary": 0, "secondary": 1, "unrated": 2}
        evidence.sort(key=lambda e: (rank[e["tier"]], not e["first_hand"], e["relation"], e["candidate_id"], e["statement"]))
        if len(evidence) > rules["max_evidence_per_claim"]:
            flags.add("evidence_truncated")
            evidence = evidence[:rules["max_evidence_per_claim"]]
        # Computed on the final stored list so replay reaches the same result.
        for index, superseded_by in supersessions(evidence).items():
            evidence[index]["superseded_by"] = superseded_by
            flags.add("official_statement_superseded")
        status, codes, primary, independent = decide(
            evidence, rules["min_independent_secondary_origins"], target["instructions"])
        if circular:
            codes.append("circular_citation")
            flags.add("circular_citation")
        summary[status] += 1
        claims.append({"claim_id": claim["claim_id"], "text": claim["text"], "basis": claim["basis"],
                       "status": status, "rationale_codes": codes, "rationale": RATIONALE[status],
                       "primary_support": primary, "independent_origins": independent, "evidence": evidence})

    pool_sha = _pool_digest(members)
    record = {
        "contract": "verification_record", "version": RECORD_VERSION,
        "record_id": record_id_for(candidate["candidate_id"], policy_sha256, pool_sha, RECORD_VERSION),
        "content_profile": candidate["content_profile"],
        "candidate": {"candidate_id": candidate["candidate_id"], "source_id": candidate["source"]["source_id"],
                      "publisher": candidate["source"]["publisher"], "url": candidate["url"],
                      "title": candidate["title"], "published_at": candidate["published_at"],
                      "retrieved_at": candidate["retrieved_at"],
                      "url_sha256": candidate["fingerprints"]["url_sha256"]},
        "policy": {"profile": policy["profile"], "policy_sha256": policy_sha256},
        "evidence_pool": {"candidates_considered": len(members), "pool_sha256": pool_sha},
        "verified_at": clock(),
        "claims": claims, "summary": summary, "flags": sorted(flags),
    }
    validate_record(record, policy, policy_sha256)
    return record


def record_id_for(candidate_id, policy_sha256, pool_sha256, version):
    """1.0 keeps its original formula; newer rule versions get distinct IDs so an existing
    1.0 file never blocks re-verification under the new rules."""
    seed = f"{candidate_id}\n{policy_sha256}\n{pool_sha256}"
    if version != "1.0":
        seed += f"\nrules={version}"
    return "ver-" + _sha256(seed)[:24]


def validate_record(record, policy, policy_sha256=None):
    """Schema check plus replay of every decision from the stored evidence.

    Version 1.0 records replay with Step 17 rules (no supersession allowed); 1.1 records
    also recompute every supersession from the stored statements and dates.
    """
    def fail(reason):
        raise NetworkError("invalid_verification_record", f"Verification Record rejected: {reason}.")

    try:
        json.dumps(record, allow_nan=False)
    except (TypeError, ValueError):
        fail("content must be finite JSON values")
    reject_secrets(record)
    error = next(_validator(str(RECORD_SCHEMA)).iter_errors(record), None)
    if error is not None:
        fail(f"schema rule '{error.validator}' at {'.'.join(str(p) for p in error.absolute_path) or '$'}")
    if record["policy"]["profile"] != policy["profile"] or record["content_profile"] != policy["profile"]:
        fail("profile does not match the policy")
    if policy_sha256 is not None and record["policy"]["policy_sha256"] != policy_sha256:
        raise NetworkError("policy_mismatch", "Verification Record was made under a different policy; verify again.")
    if [c["claim_id"] for c in record["claims"]] != [f"k{i}" for i in range(1, len(record["claims"]) + 1)]:
        fail("claim identifiers are out of order")
    aliases, rules = policy["attribution_aliases"], policy["rules"]
    summary = dict.fromkeys(STATUSES, 0)
    any_superseded = False
    for claim in record["claims"]:
        if len(claim["evidence"]) > rules["max_evidence_per_claim"]:
            fail("too much evidence")
        expected = supersessions(claim["evidence"]) if record["version"] != "1.0" else {}
        stored_marks = {i: e["superseded_by"] for i, e in enumerate(claim["evidence"]) if "superseded_by" in e}
        if stored_marks != expected:
            fail("superseded evidence does not follow from the stored statements and dates")
        any_superseded |= bool(expected)
        for item in claim["evidence"]:
            if item["tier"] != tier_for(item["source_id"], item["url"], policy):
                fail("evidence tier does not match the policy")
            if item["first_hand"] != is_first_hand(item["statement"], item["publisher"], aliases):
                fail("evidence first-hand flag does not match its statement")
            if INSTRUCTION_TEXT.search(item["statement"]):
                fail("evidence contains instruction-like text")
        status, codes, primary, independent = decide(
            claim["evidence"], rules["min_independent_secondary_origins"], bool(INSTRUCTION_TEXT.search(claim["text"])))
        stored = [c for c in claim["rationale_codes"] if c != "circular_citation"]
        if (status != claim["status"] or codes != stored or primary != claim["primary_support"]
                or independent != claim["independent_origins"] or claim["rationale"] != RATIONALE[status]):
            fail("a claim status does not follow from its evidence")
        summary[status] += 1
    if summary != record["summary"]:
        fail("summary counts do not match the claims")
    if any_superseded != ("official_statement_superseded" in record["flags"]):
        fail("supersession flag does not match the evidence")
    expected_id = record_id_for(record["candidate"]["candidate_id"], record["policy"]["policy_sha256"],
                                record["evidence_pool"]["pool_sha256"], record["version"])
    if record["record_id"] != expected_id:
        fail("record_id does not match its inputs")
