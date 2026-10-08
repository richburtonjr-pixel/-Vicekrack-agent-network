"""Step 37: explicit human review decisions for content previews.

A review record (`content_review` 1.0, schemas/content-review.schema.json) is one human
decision about one preview, tied to one Step 36 bound quality report:

  approved_for_preview | changes_requested | rejected

Each record holds the production ID, the quality report's ID and file SHA-256, the exact
artifact-binding digest, a self-declared reviewer label (never an authenticated identity),
a UTC timestamp, the decision, the acknowledgments given, the conditions seen at review
time and optional bounded notes.

Rules (checked again immediately before saving, under the production lock):
- the report must still be bound to the current files (`verify_binding` == matching), and
  the reviewer must confirm its binding digest;
- approval is blocked when the technical result is `fail`, and refused when evidence
  freshness cannot be established now;
- approval needs explicit acknowledgments for `needs_review`, unavailable checks, draft
  restrictions and stale evidence, when they apply (acknowledgments that do not apply are
  refused, so a record never claims more than was seen);
- if anything changes while the decision is being recorded, the decision is refused rather
  than bound to different artifacts.

Approval accepts this preview only: technical findings are unchanged, `publishable` stays
false, and nothing is exported, uploaded or published.

History is append-only: records are never edited or deleted. Each record names the record
it supersedes (`supersedes`, the previous latest), so a concurrent decision cannot be
silently lost: the second writer is refused and must re-read the history. Files are
`runtime/reviews/<production_id>/<sequence>-<review_id>.json`, written to a temporary
file, flushed and published with an exclusive hard link (an interrupted write leaves only
an ignored temporary file). Whether an old decision still applies is computed every time
it is read, never stored: changed or missing artifacts or reports invalidate it, and
evidence that became stale is reported separately from artifact matching.

Review notes are free text typed by a person and may contain sensitive content. They are
stored unencrypted in ignored local storage and are always displayed as text.
"""

import json
import re
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .artifact_binding import MAX_JSON_BYTES, safe_file, sha256_bytes, sha256_file, verify_binding
from .errors import NetworkError
from .orchestrator import ROOT

CONTRACT, VERSION = "content_review", "1.0"
DECISIONS = ("approved_for_preview", "changes_requested", "rejected")
ACKNOWLEDGMENTS = ("needs_review_result", "unavailable_checks", "draft_restrictions", "stale_evidence")
REVIEW_ID = re.compile(r"^rev-[0-9a-f]{24}$")
FILE_NAME = re.compile(r"^(\d{6})-(rev-[0-9a-f]{24})\.json$")
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@'()-]{0,63}$")
DIGEST_PREFIX = re.compile(r"^[0-9a-f]{12,64}$")
MAX_NOTES = 2000
MAX_RECORDS = 500
MEANING = ("Human acceptance of this preview only. Technical findings are unchanged; it is not permission to publish, "
           "not rights clearance and not fact verification. publishable stays false.")
NOTE = ("Review decisions are self-declared (reviewer labels are not authenticated). A decision applies only while the "
        "quality report it names is unchanged and still matches the current files; evidence freshness is shown "
        "separately. Notes are user text and may contain sensitive content.")


def utc(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _clock_now():
    """Seconds now (UTC). One seam, so tests can pin the evidence age."""
    return datetime.now(timezone.utc).timestamp()


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def binding_digest(report):
    """SHA-256 of the report's exact binding (canonical JSON)."""
    encoded = json.dumps(report["binding"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(encoded.encode("utf-8"))


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads((ROOT / "schemas/content-review.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_review(record):
    if not isinstance(record, dict) or next(_validator().iter_errors(record), None) is not None:
        raise NetworkError("invalid_review_record", "The review record does not match its contract.")
    return record


def _folder(production_id, root):
    from .production import PRODUCTION_ID
    if not isinstance(production_id, str) or not PRODUCTION_ID.match(production_id):
        raise NetworkError("invalid_production_id", "Production IDs look like prod- followed by 24 hex characters.")
    base = Path(root if root is not None else ROOT) / "runtime/reviews"
    folder = base / production_id
    if base.is_symlink() or folder.is_symlink():
        raise NetworkError("invalid_review_storage", "Review storage cannot be a symbolic link.")
    return folder


# ---------------------------------------------------------------- evidence freshness (now)
def evidence_now(state, root=None, now=None):
    """Freshness of the production's verification record right now, against its own policy."""
    from .production import PROJECT
    from .verification import load_policy
    from .verification_cli import load_record
    out = {"status": "unavailable", "reason": None, "age_hours": None, "max_age_days": None, "verified_at": None}
    policy = state["config"]["policy"]
    try:
        path, _ = safe_file(PROJECT, policy["path"], MAX_JSON_BYTES)
        if path is None or sha256_file(path) != policy["sha256"]:
            out["reason"] = "verification_policy_changed"
            return out
        loaded, sha = load_policy(policy["path"])
        record = load_record(state["config"]["record_id"], loaded, sha, root)
        max_age = loaded["rules"]["max_record_age_days"]
        age = (now if now is not None else _clock_now()) - utc(record["verified_at"])
    except NetworkError as error:
        out["reason"] = "evidence_record_unavailable" if error.code == "record_not_found" else "evidence_record_invalid"
        return out
    except (OSError, ValueError, KeyError, TypeError):
        out["reason"] = "evidence_record_unavailable"
        return out
    if age < 0:
        out["reason"] = "evidence_verified_in_the_future"
        return out
    out.update(status="stale" if age > max_age * 86400 else "fresh", age_hours=round(age / 3600, 1),
               max_age_days=max_age, verified_at=record["verified_at"])
    return out


# ---------------------------------------------------------------- gates
def _report(report_id, root):
    """(report, sha256 of its file bytes) for a saved, schema-valid quality report."""
    from .quality import load_report
    report = load_report(report_id, root)
    path, problem = safe_file(Path(root if root is not None else ROOT) / "runtime/quality", report_id + ".json",
                              MAX_JSON_BYTES)
    if path is None:
        raise NetworkError("quality_report_not_found", "No saved quality report with this ID.")
    return report, sha256_file(path)


def assess(report, state, folder, root=None, now=None):
    """What a reviewer must know before deciding on this report (read-only)."""
    binding = verify_binding(report, state, folder, root)
    checks = {c["check_id"]: c for c in report["checks"]}
    unavailable = sorted(c for c, item in checks.items() if item["status"] == "unavailable")
    draft = next((s for s in state["stages"] if s["name"] == "validate"), {}).get("artifacts", {}).get("draft") is True
    evidence = evidence_now(state, root, now)
    freshness = checks.get("evidence_freshness", {}).get("status", "unavailable")
    conditions = {"technical_result": report["result"], "needs_review": report["result"] == "needs_review",
                  "unavailable_checks": unavailable[:20], "draft": draft, "evidence_freshness_at_check": freshness,
                  "evidence_now": {k: evidence[k] for k in ("status", "age_hours", "max_age_days")}}
    applicable = []
    if conditions["needs_review"]:
        applicable.append("needs_review_result")
    if unavailable:
        applicable.append("unavailable_checks")
    if draft:
        applicable.append("draft_restrictions")
    if evidence["status"] == "stale":
        applicable.append("stale_evidence")
    blockers, approval_blockers = [], []
    if binding["status"] != "matching":
        blockers.append("binding_not_matching")
    if report["result"] == "fail":
        approval_blockers.append("technical_result_fail")
    if evidence["status"] == "unavailable":
        approval_blockers.append("evidence_freshness_not_established")
    digest = binding_digest(report) if "binding" in report else None
    return {"report_id": report["report_id"], "production_id": report["production_id"], "binding": binding,
            "binding_digest": digest, "conditions": conditions, "evidence": evidence,
            "applicable_acknowledgments": applicable, "blockers": blockers,
            "approval_blockers": approval_blockers + blockers,
            "decisions_allowed": [] if blockers else
            [d for d in DECISIONS if d != "approved_for_preview" or not approval_blockers]}


def _check_inputs(decision, reviewer, notes, acknowledgments):
    if decision not in DECISIONS:
        raise NetworkError("invalid_decision", "Use approved_for_preview, changes_requested or rejected.")
    if not isinstance(reviewer, str) or not LABEL.match(reviewer):
        raise NetworkError("invalid_reviewer_label",
                           "Reviewer labels are 1-64 letters, digits, spaces or . _ @ ' ( ) - (self-declared, not verified).")
    if notes is not None:
        if not isinstance(notes, str):
            raise NetworkError("invalid_notes", "Notes must be text.")
        notes = notes.replace("\r\n", "\n").strip()
        if len(notes) > MAX_NOTES or any(ord(ch) < 32 and ch not in "\n\t" for ch in notes) or "\x7f" in notes:
            raise NetworkError("invalid_notes", f"Notes are plain text of at most {MAX_NOTES} characters.")
        notes = notes or None
    acks = list(dict.fromkeys(acknowledgments or []))
    if any(a not in ACKNOWLEDGMENTS for a in acks):
        raise NetworkError("invalid_acknowledgment", "Unknown acknowledgment.")
    return notes, sorted(acks, key=ACKNOWLEDGMENTS.index)


# ---------------------------------------------------------------- history (read side)
def _read_history(production_id, root):
    """(records in sequence order, corrupted entries). Never follows links; never changes anything."""
    folder = _folder(production_id, root)
    records, corrupted = [], []
    if not folder.exists():
        return records, corrupted
    if not folder.is_dir():
        return records, [{"name": "unexpected_entry", "code": "review_storage_invalid"}]
    names = sorted(p.name for p in folder.iterdir())
    for name in names:
        if name.endswith(".tmp"):
            continue                                   # an interrupted write's temporary file; never promoted, never read
        match = FILE_NAME.match(name)
        if match is None:
            corrupted.append({"name": "unexpected_entry", "code": "unexpected_file_in_review_history"})
            continue
        if len(records) + len(corrupted) >= MAX_RECORDS:
            corrupted.append({"name": name, "code": "review_history_too_long"})
            break
        path, problem = safe_file(folder, name, MAX_JSON_BYTES)
        try:
            if path is None:
                raise ValueError(problem)
            record = json.loads(path.read_bytes().decode("utf-8"))
            validate_review(record)
        except (OSError, ValueError, UnicodeError, NetworkError):
            corrupted.append({"name": name, "code": "review_record_unreadable_or_invalid"})
            continue
        if (record["review_id"] != match.group(2) or record["sequence"] != int(match.group(1))
                or record["production_id"] != production_id):
            corrupted.append({"name": name, "code": "review_record_does_not_match_its_file"})
            continue
        records.append(record)
    records.sort(key=lambda r: r["sequence"])
    previous = None
    for index, record in enumerate(records, start=1):
        if record["sequence"] != index or record["supersedes"] != previous:
            corrupted.append({"name": f"{record['sequence']:06d}-{record['review_id']}.json",
                              "code": "review_history_chain_broken"})
        previous = record["review_id"]
    return records, corrupted


def _row(record, later, live):
    conditions = record["conditions"]
    return {"review_id": record["review_id"], "sequence": record["sequence"], "decision": record["decision"],
            "reviewer_label": record["reviewer"]["label"], "reviewer_authenticated": False,
            "recorded_at": record["recorded_at"], "report_id": record["quality_report"]["report_id"],
            "report_sha256": record["quality_report"]["sha256"], "binding_digest": record["binding_digest"],
            "acknowledgments": list(record["acknowledgments"]), "notes": record["notes"],
            "conditions": {"technical_result": conditions["technical_result"],
                           "unavailable_checks": list(conditions["unavailable_checks"]), "draft": conditions["draft"],
                           "evidence_freshness_at_check": conditions["evidence_freshness_at_check"],
                           "evidence_at_review": conditions["evidence_now"]["status"]},
            "supersedes": record["supersedes"], "superseded_by": later.get(record["review_id"]), **live}


def _live(record, report_cache, state, folder, root, evidence, latest_id, corrupted):
    """Whether one saved decision applies now. Computed on every read; never stored."""
    report_id = record["quality_report"]["report_id"]
    if report_id not in report_cache:
        try:
            report_cache[report_id] = _report(report_id, root)
        except NetworkError as error:
            report_cache[report_id] = error.code
    loaded = report_cache[report_id]
    reasons = []
    if isinstance(loaded, str):
        artifacts, reasons = "unavailable", ["quality_report_missing_or_invalid"]
    else:
        report, sha = loaded
        if sha != record["quality_report"]["sha256"] or binding_digest(report) != record["binding_digest"]:
            artifacts, reasons = "changed", ["quality_report_changed_since_review"]
        elif folder is None:
            artifacts, reasons = "unavailable", ["production_folder_unavailable"]
        else:
            result = verify_binding(report, state, folder, root)
            artifacts = result["status"] if result["status"] in ("matching", "changed") else "unavailable"
            reasons = ["artifacts_changed_since_review"] + ["changed_" + c for c in result["changed"][:20]] \
                if result["status"] == "changed" else list(result["reasons"])
    if corrupted:
        applicability = "unavailable"
        reasons = ["review_history_corrupted"] + reasons
    elif record["review_id"] != latest_id:
        applicability = "superseded"
    elif artifacts == "matching":
        applicability = "current"
    else:
        applicability = "invalidated"
    approval = record["decision"] == "approved_for_preview" and applicability == "current"
    if approval and evidence["status"] == "unavailable":
        approval, reasons = False, reasons + ["evidence_freshness_not_established_now"]
    elif approval and evidence["status"] == "stale" and "stale_evidence" not in record["acknowledgments"]:
        approval, reasons = False, reasons + ["evidence_became_stale_since_review"]
    return {"applicability": applicability, "artifact_binding_now": artifacts, "current_preview_approval": approval,
            "reasons": list(dict.fromkeys(reasons))[:30]}


def history(production_id, root=None, now=None, state=None, folder=None):
    """Every saved decision with its applicability now, newest first, plus a summary."""
    from .production import ProductionStore
    store = ProductionStore(root)
    if state is None:
        state = store.read(production_id)
        folder = store.folder(production_id)
    records, corrupted = _read_history(production_id, root)
    evidence = evidence_now(state, root, now)
    later = {r["supersedes"]: r["review_id"] for r in records if r["supersedes"]}
    latest = records[-1] if records else None
    cache, rows = {}, []
    for record in records:
        live = _live(record, cache, state, folder, root, evidence, latest["review_id"], corrupted)
        rows.append(_row(record, later, live))
    rows.reverse()
    current = rows[0] if rows else None
    if corrupted:
        status = "history_corrupted"
    elif not rows:
        status = "none_saved"
    else:
        status = "available"
    summary = {"latest_review_id": current["review_id"] if current else None,
               "latest_decision": current["decision"] if current else None,
               "latest_applicability": current["applicability"] if current else None,
               "current_preview_approval": bool(current and current["current_preview_approval"]),
               "reasons": (["review_history_corrupted"] if corrupted else []) + (current["reasons"] if current else [])}
    return {"production_id": production_id, "status": status, "summary": summary,
            "evidence_now": {k: evidence[k] for k in ("status", "age_hours", "max_age_days")},
            "reviews": rows, "corrupted": corrupted[:40], "note": NOTE}


def inspect(production_id, review_id, root=None, now=None):
    if not isinstance(review_id, str) or not REVIEW_ID.match(review_id):
        raise NetworkError("invalid_review_id", "Review IDs look like rev- followed by 24 hex characters.")
    doc = history(production_id, root, now)
    row = next((r for r in doc["reviews"] if r["review_id"] == review_id), None)
    if row is None:
        raise NetworkError("review_not_found", "No valid review with this ID for this production.")
    records, _ = _read_history(production_id, root)
    record = next(r for r in records if r["review_id"] == review_id)
    return {"record": record, "now": {k: row[k] for k in ("applicability", "artifact_binding_now",
                                                          "current_preview_approval", "reasons", "superseded_by")},
            "evidence_now": doc["evidence_now"], "history_status": doc["status"], "note": NOTE}


# ---------------------------------------------------------------- recording (write side)
def _between_checks():
    """Seam for tests: runs between the first validation and the re-validation before saving."""


def _publish(document, folder, name):
    from .scout_cli import _publish as publish
    try:
        return publish(document, folder, name)
    except NetworkError:
        raise NetworkError("review_write_failed", "Could not save the review record completely.") from None


def _stable(conditions):
    """Conditions without the evidence age, which moves with the clock (its status is compared)."""
    return dict(conditions, evidence_now=conditions["evidence_now"]["status"])


class ReviewRecorder:
    def __init__(self, root=None, clock=None, now=None):
        self.root = root
        self.clock = clock or now_utc
        self.now = now                                 # seconds, for evidence age (tests); None = real time

    def record(self, production_id, report_id, *, decision, reviewer, binding, acknowledgments=(), notes=None,
               supersedes=None):
        from .persistence import reject_secrets
        from .production import ProductionStore
        notes, acks = _check_inputs(decision, reviewer, notes, acknowledgments)
        if not isinstance(binding, str) or not DIGEST_PREFIX.match(binding):
            raise NetworkError("binding_digest_required", "Confirm the report's binding digest (at least 12 hex characters).")
        if supersedes is not None and (not isinstance(supersedes, str) or not REVIEW_ID.match(supersedes)):
            raise NetworkError("invalid_review_id", "Review IDs look like rev- followed by 24 hex characters.")
        store = ProductionStore(self.root)
        folder = store.folder(production_id)
        reviews = _folder(production_id, self.root)
        with store.lock(production_id):                # no production resume or quality check meanwhile
            first = self._evaluate(store, production_id, report_id, folder)
            records, corrupted = _read_history(production_id, self.root)
            if corrupted:
                raise NetworkError("review_history_corrupted",
                                   "The review history has unreadable or inconsistent records; nothing was saved.")
            latest = records[-1]["review_id"] if records else None
            if supersedes != latest:
                raise NetworkError("supersedes_required" if supersedes is None else "review_conflict",
                                   "Name the latest review this decision supersedes (see review-list).")
            assessment, report, report_sha = first
            if report["production_id"] != production_id:
                raise NetworkError("report_for_another_production", "The quality report is for another production.")
            if assessment["blockers"]:
                raise NetworkError("binding_not_matching", "The quality report no longer matches the current files.")
            if not assessment["binding_digest"].startswith(binding):
                raise NetworkError("binding_digest_mismatch", "The confirmed digest is not this report's binding.")
            applicable = assessment["applicable_acknowledgments"]
            extra = [a for a in acks if a not in applicable]
            if extra:
                raise NetworkError("acknowledgment_not_applicable", "Acknowledge only conditions that apply: "
                                   + ", ".join(extra))
            if decision == "approved_for_preview":
                if "technical_result_fail" in assessment["approval_blockers"]:
                    raise NetworkError("approval_blocked_technical_fail", "A failed quality report cannot be approved.")
                if "evidence_freshness_not_established" in assessment["approval_blockers"]:
                    raise NetworkError("approval_blocked_evidence_unknown",
                                       "Evidence freshness cannot be established, so the preview cannot be approved.")
                missing = [a for a in applicable if a not in acks]
                if missing:
                    raise NetworkError("acknowledgment_required", "Approval needs these acknowledgments: "
                                       + ", ".join(missing))
            sequence = len(records) + 1
            record = {"contract": CONTRACT, "version": VERSION, "review_id": "rev-" + secrets.token_hex(12),
                      "production_id": production_id, "sequence": sequence, "supersedes": latest,
                      "decision": decision, "reviewer": {"label": reviewer, "authenticated": False},
                      "recorded_at": self.clock(),
                      "quality_report": {"report_id": report["report_id"], "sha256": report_sha,
                                         "result": report["result"], "checked_at": report["checked_at"]},
                      "binding_digest": assessment["binding_digest"], "conditions": assessment["conditions"],
                      "acknowledgments": acks, "notes": notes,
                      "scope": {"preview_only": True, "publishable": False, "technical_findings_unchanged": True,
                                "reviewer_authenticated": False, "meaning": MEANING}}
            reject_secrets(record)
            validate_review(record)
            _between_checks()
            again, report2, sha2 = self._evaluate(store, production_id, report_id, folder)
            records2, corrupted2 = _read_history(production_id, self.root)
            if (sha2 != report_sha or again["binding"]["status"] != "matching"
                    or again["binding_digest"] != assessment["binding_digest"]
                    or _stable(again["conditions"]) != _stable(assessment["conditions"])):
                raise NetworkError("artifacts_changed_during_review",
                                   "The files, report or evidence changed while the decision was being recorded; "
                                   "nothing was saved. Review again.")
            if corrupted2 or [r["review_id"] for r in records2] != [r["review_id"] for r in records]:
                raise NetworkError("review_conflict", "Another decision was saved meanwhile; nothing was saved.")
            name = f"{sequence:06d}-{record['review_id']}.json"
            try:
                reviews.mkdir(parents=True, exist_ok=True)
                taken = [p.name for p in reviews.iterdir() if p.name.startswith(f"{sequence:06d}-")]
            except OSError:
                raise NetworkError("review_write_failed", "Could not save the review record completely.") from None
            if taken or not _publish(record, reviews, name):
                raise NetworkError("review_conflict", "Another decision was saved meanwhile; nothing was saved.")
        return record, reviews / name

    def _evaluate(self, store, production_id, report_id, folder):
        state = store.read(production_id)
        report, sha = _report(report_id, self.root)
        if report["production_id"] != production_id:
            raise NetworkError("report_for_another_production", "The quality report is for another production.")
        return assess(report, state, folder, self.root, self.now), report, sha


def reviewable(production_id, root=None, now=None):
    """Read-only: each saved quality report of this production and what a decision on it requires."""
    from .production import ProductionStore
    from .quality import list_reports
    store = ProductionStore(root)
    state = store.read(production_id)
    folder = store.folder(production_id)
    rows = []
    for item in list_reports(root):
        if item.get("production_id") != production_id:
            continue
        try:
            report, sha = _report(item["report_id"], root)
        except NetworkError as error:
            rows.append({"report_id": item["report_id"], "error": error.code})
            continue
        found = assess(report, state, folder, root, now)
        rows.append({"report_id": report["report_id"], "checked_at": report["checked_at"], "report_sha256": sha,
                     "binding": found["binding"]["status"], "binding_digest": found["binding_digest"],
                     "technical_result": report["result"], "conditions": found["conditions"],
                     "applicable_acknowledgments": found["applicable_acknowledgments"],
                     "decisions_allowed": found["decisions_allowed"],
                     "approval_blockers": found["approval_blockers"]})
    return rows[:40]

