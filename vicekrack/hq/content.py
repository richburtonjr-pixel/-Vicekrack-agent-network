"""Living HQ content results desk (Step 35): read-only views of saved content productions.

Two documents (contract `hq_content` 1.0, schemas/hq-content.schema.json) for one selected
content timeline (`prod-...` reconstructed, a recorded `tl-...` production or quality
timeline, or `demo`), plus a media route:

  at_position  what was ESTABLISHED to exist after the first N events of the timeline:
               stage states folded from those events, and only the artifacts whose
               completion is proven at or before that event. Nothing later is sent.
  latest       the latest saved production: every artifact re-verified now, stage
               attempts and failures, the recorded attempts of this production, and every
               saved Step 22 quality report for it with its binding status.
  media        one validated preview file (video or scene poster) of the production,
               addressed by an opaque `med-` ID; bounded byte ranges.

Integrity (IDs and hashes only; never filenames or timestamps alone):
- Production state is read with ProductionStore.read (validated; no lock, no writes).
- Each artifact must sit inside its production folder (no absolute paths, `..`, or any
  symbolic link along the way), match the SHA-256 saved in the state, pass its own
  contract validator, and match the chain brief -> script -> validation -> plan ->
  manifest -> video (IDs, embedded copies and hashes). A failing artifact is reported
  (`missing`, `tampered`, `mismatched`) and its content is never shown; artifacts after it
  are `unavailable_upstream_invalid`.
- Scene posters are listed in the hash-checked manifest but the Step 13 manifest stores no
  poster hashes: they are served as `not_hash_bound` (inside the preview folder, PNG
  signature, size limit) and labelled so.
- Timelines: a recorded timeline must name this production (run ID and correlation ID). For
  the historical view each completed stage's single saved completion must be proven by the
  timeline: in a reconstructed timeline by the production's own trace (unique completion,
  no truncation or disagreement); in a recorded attempt by a `stage_completed` whose saved
  finish time lies inside that attempt, or a `stage_reused` for a stage finished before
  the attempt started. Otherwise historical viewing is `unavailable` with reasons.
- Quality reports are matched by their own `production_id` (and file name = report ID).
  The Step 22 report contract stores no artifact hashes, so a report can never be proven
  to describe the current artifacts: it is `stale` when it provably predates them (checked
  before the last stage finished, or it saw a different production status) and
  `unverified` otherwise. It is never shown as current. Quality checks are never run.

Nothing here runs agents, generates scripts, renders media, contacts providers, publishes,
locks or writes production files.
"""

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from ..content_events import correlation_for
from ..errors import NetworkError
from ..events.contract import ROOT, fold

PRODUCTION_ID = re.compile(r"^prod-[0-9a-f]{24}$")
MEDIA_ID = re.compile(r"^med-[0-9a-f]{24}$")
REPORT_ID = re.compile(r"^qr-[0-9a-f]{24}$")
STAGES = ("brief", "creator", "validate", "plan", "preview")
STAGE_OF = {f"content.production.{name}": name for name in STAGES}
PIPELINE = "content.production.pipeline"
QUALITY = "content.production.quality"
MAX_REPORTS, MAX_REPORT_SCAN, MAX_ATTEMPTS = 20, 500, 20
MAX_VIDEO_BYTES, MAX_POSTER_BYTES, MAX_JSON_BYTES = 64 * 1024 * 1024, 8 * 1024 * 1024, 4 * 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n"
NOTICE = ("Read-only display of one saved content production. Preview only: publishable is false, nothing here "
          "approves, exports, uploads or publishes, and opening it runs no agents, renders or checks.")
DEMO_NOTICE = "DEMO DATA: a synthetic production with no media files; not a saved run. " + NOTICE
RESTRICTIONS = [
    "publishable: false. Every preview is preview-only and watermarked; this desk cannot change that.",
    "No approval, export, upload or publishing exists in the HQ (human review and export is deferred work).",
    "Claim status comes from the Verification stage; nothing here re-checks facts or rights.",
]
VERIFICATION_LIMITS = [
    "Claim statuses come from the saved Step 17 verification record; this desk does not re-verify facts.",
    "Image, footage, music and narration rights were never checked.",
    "Evidence is whatever the approved sources said when they were scouted; later corrections are not known.",
]
QUALITY_BINDING = ("The Step 22 report records which production it checked and when, but no hashes of the "
                   "artifacts it saw. It cannot prove it describes the current files, so it is never shown as current. "
                   "Run quality-report PRODUCTION_ID to check the current artifacts.")


def _sha(path_or_bytes):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return hashlib.sha256(path_or_bytes).hexdigest()
    digest = hashlib.sha256()
    with open(path_or_bytes, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def media_id(production_id, relative, binding):
    return "med-" + _digest({"production": production_id, "path": relative, "binding": binding})[:24]


@lru_cache(maxsize=None)
def _validator():
    return Draft202012Validator(json.loads((ROOT / "schemas/hq-content.schema.json").read_text(encoding="utf-8")))


def check(document):
    if next(_validator().iter_errors(document), None) is not None:
        raise NetworkError("invalid_content_results", "The content results document does not match its contract.")
    return document


# ---------------------------------------------------------------- safe files
def safe_file(folder, relative, max_bytes):
    """(path, None) for a regular file inside `folder` reached without any symbolic link, else (None, code)."""
    if not isinstance(relative, str) or not relative or len(relative) > 300 or "\\" in relative or "\x00" in relative:
        return None, "path_invalid"
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or any(p in ("..", ".", "") for p in parts):
        return None, "path_invalid"
    current = Path(folder)
    try:
        if stat.S_ISLNK(os.lstat(current).st_mode):
            return None, "path_invalid"
        for part in parts:
            current = current / part
            mode = os.lstat(current).st_mode
            if stat.S_ISLNK(mode):
                return None, "path_invalid"
        if not stat.S_ISREG(mode):
            return None, "path_invalid"
        if not current.resolve().is_relative_to(Path(folder).resolve()):
            return None, "path_invalid"
        if os.lstat(current).st_size > max_bytes:
            return None, "file_too_large"
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "missing"
    return current, None


def _read_json(path):
    data = path.read_bytes()
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("too large")
    return json.loads(data.decode("utf-8"))


def safe_link(url):
    """True only for a plain http(s) URL with a host and no credentials (shown as text either way)."""
    if not isinstance(url, str) or not 8 <= len(url) <= 2048 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        return False
    try:
        parts = urlsplit(url)
        port_ok = parts.port is None or 0 < parts.port < 65536
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(parts.hostname) and port_ok
            and parts.username is None and parts.password is None and "@" not in parts.netloc)


# ---------------------------------------------------------------- artifact verification
class Artifacts:
    """Re-verifies every completed stage's artifacts of one saved production (never raises)."""

    def __init__(self, state, folder):
        self.state, self.folder = state, folder
        self.stages = {s["name"]: s for s in state["stages"]}
        self.items, self.media = {}, {}
        self.brief = self.script = self.plan = self.manifest = None
        self.video_path = None
        self._verify()

    def _set(self, name, status, reason=None, **extra):
        self.items[name] = {"status": status, "reason": reason, **extra}

    def _completed(self, name):
        return self.stages[name]["status"] == "completed"

    def _hashed(self, name, key, digest_key, max_bytes=MAX_JSON_BYTES):
        artifacts = self.stages[name]["artifacts"]
        path, problem = safe_file(self.folder, artifacts.get(key), max_bytes)
        if path is None:
            return None, "missing" if problem == "missing" else "tampered", \
                "artifact_missing" if problem == "missing" else "artifact_path_invalid"
        if _sha(path) != artifacts.get(digest_key):
            return None, "tampered", "artifact_hash_mismatch"
        return path, None, None

    def _json(self, artifact, stage, key, digest_key, validate):
        path, status, reason = self._hashed(stage, key, digest_key)
        if path is None:
            self._set(artifact, status, reason)
            return None
        try:
            document = _read_json(path)
            validate(document)
        except (NetworkError, OSError, ValueError, UnicodeError, TypeError, KeyError):
            self._set(artifact, "tampered", "artifact_invalid")
            return None
        return document

    def _verify(self):
        from ..scene_plan import validate_scene_plan
        from ..short_script import validate_short_script
        from ..story_brief import count_unverified, validate_story_brief
        config, upstream = self.state["config"], None
        for name in ("brief", "script", "validation", "plan", "manifest", "video"):
            stage = {"brief": "brief", "script": "creator", "validation": "validate", "plan": "plan",
                     "manifest": "preview", "video": "preview"}[name]
            if not self._completed(stage):
                self._set(name, "not_produced", "stage_not_completed")
                upstream = upstream or name
                continue
            if upstream is not None and self.items[upstream]["status"] != "not_produced":
                self._set(name, "unavailable", "upstream_artifact_invalid")
                continue
            getattr(self, "_check_" + name)(validate_story_brief, validate_short_script, validate_scene_plan,
                                             count_unverified, config)
            if self.items[name]["status"] != "verified":
                upstream = upstream or name

    def _check_brief(self, validate_brief, _s, _p, _c, config):
        brief = self._json("brief", "brief", "brief_path", "brief_sha256", validate_brief)
        if brief is None:
            return
        artifacts = self.stages["brief"]["artifacts"]
        if (brief.get("verification", {}).get("record_ids") != [config["record_id"]]
                or brief.get("editorial", {}).get("selection_run_id") != config["selection_run_id"]
                or brief["brief_id"] != artifacts.get("brief_id")):
            self._set("brief", "mismatched", "brief_not_from_this_production")
            return
        self.brief = brief
        self._set("brief", "verified", sha256=artifacts["brief_sha256"])

    def _check_script(self, _b, validate_script, _p, _c, config):
        script = self._json("script", "creator", "script_path", "script_sha256", validate_script)
        if script is None:
            return
        if (script["claims"] != self.brief["claims"] or script["sources"] != self.brief["sources"]
                or script["script_id"] != self.stages["creator"]["artifacts"].get("script_id")):
            self._set("script", "mismatched", "script_not_matching_brief")
            return
        self.script = script
        self._set("script", "verified", sha256=self.stages["creator"]["artifacts"]["script_sha256"])

    def _check_validation(self, _b, _s, _p, count_unverified, config):
        artifacts = self.stages["validate"]["artifacts"]
        unverified = count_unverified(self.script)
        if (artifacts.get("script_sha256") != self.stages["creator"]["artifacts"].get("script_sha256")
                or artifacts.get("draft") != (unverified > 0) or artifacts.get("claims_unverified") != unverified
                or artifacts.get("claims_total") != len(self.script["claims"])):
            self._set("validation", "mismatched", "validation_not_matching_script")
            return
        self._set("validation", "verified", draft=artifacts["draft"], claims_unverified=unverified,
                  claims_total=len(self.script["claims"]))

    def _check_plan(self, _b, _s, validate_plan, _c, config):
        plan = self._json("plan", "plan", "plan_path", "plan_sha256", validate_plan)
        if plan is None:
            return
        artifacts = self.stages["plan"]["artifacts"]
        if (plan["script"] != self.script or plan["plan_id"] != artifacts.get("plan_id")
                or plan["blocked_for_production"] != self.items["validation"]["draft"]):
            self._set("plan", "mismatched", "plan_not_matching_script")
            return
        self.plan = plan
        self._set("plan", "verified", sha256=artifacts["plan_sha256"])

    def _check_manifest(self, *_):
        artifacts = self.stages["preview"]["artifacts"]
        path, status, reason = self._hashed("preview", "manifest_file", "manifest_sha256")
        if path is None:
            self._set("manifest", status, reason)
            return
        try:
            manifest = _read_json(path)
            ok = (isinstance(manifest, dict) and manifest.get("contract") == "preview_render"
                  and isinstance(manifest.get("scenes"), list) and isinstance(manifest.get("limitations"), list))
        except (OSError, ValueError, UnicodeError):
            ok = False
        if not ok:
            self._set("manifest", "tampered", "artifact_invalid")
            return
        draft = self.items["validation"]["draft"]
        if (manifest.get("plan_id") != self.plan["plan_id"] or manifest.get("input_sha256") != self.plan["input_sha256"]
                or manifest.get("publishable") is not False or manifest.get("preview_only") is not True
                or manifest.get("source_blocked_for_production") != draft
                or manifest.get("video_sha256") != artifacts.get("video_sha256")
                or bool(manifest.get("audio_present")) != bool(artifacts.get("audio_present"))
                or bool(manifest.get("audio_present")) != (self.state["config"]["narration"] is not None)):
            self._set("manifest", "mismatched", "manifest_not_matching_plan")
            return
        self.manifest, self.manifest_dir = manifest, str(PurePosixPath(artifacts["manifest_file"]).parent)
        self._set("manifest", "verified", sha256=artifacts["manifest_sha256"])

    def _check_video(self, *_):
        artifacts = self.stages["preview"]["artifacts"]
        if PurePosixPath(artifacts["preview_file"]).parent != PurePosixPath(self.manifest_dir):
            self._set("video", "mismatched", "video_not_beside_manifest")
            return
        path, status, reason = self._hashed("preview", "preview_file", "video_sha256", MAX_VIDEO_BYTES)
        if path is None:
            self._set("video", status, reason)
            return
        self.video_path = path
        identifier = media_id(self.state["production_id"], artifacts["preview_file"], artifacts["video_sha256"])
        self.media[identifier] = {"path": artifacts["preview_file"], "kind": "video", "sha256": artifacts["video_sha256"]}
        self._set("video", "verified", sha256=artifacts["video_sha256"], media_id=identifier, bytes=path.stat().st_size)
        self.posters = []
        for scene in self.manifest["scenes"][:8]:
            poster = scene.get("poster") if isinstance(scene, dict) else None
            row = {"index": scene.get("index") if isinstance(scene, dict) and isinstance(scene.get("index"), int) else None,
                   "status": "missing", "reason": "poster_missing", "media_id": None}
            if isinstance(poster, str) and "/" not in poster:
                relative = f"{self.manifest_dir}/{poster}"
                found, problem = safe_file(self.folder, relative, MAX_POSTER_BYTES)
                if found is not None:
                    with open(found, "rb") as stream:
                        png = stream.read(8) == PNG
                    if png:
                        identifier = media_id(self.state["production_id"], relative, self.items["manifest"]["sha256"])
                        self.media[identifier] = {"path": relative, "kind": "poster", "sha256": None}
                        row.update(status="not_hash_bound", reason="manifest_has_no_poster_hashes", media_id=identifier)
                    else:
                        row.update(status="tampered", reason="poster_not_png")
                elif problem != "missing":
                    row.update(status="tampered", reason="poster_path_invalid")
            else:
                row.update(status="tampered", reason="poster_path_invalid")
            self.posters.append(row)


# ---------------------------------------------------------------- shaping
def _source(source):
    url = source.get("url")
    return {"source_id": source["source_id"], "title": source.get("title"), "publisher": source.get("publisher"),
            "kind": source.get("kind"), "url": url if isinstance(url, str) else None,
            "link_allowed": safe_link(url), "accessed_at": source.get("accessed_at")}


def brief_section(brief):
    links = {c["claim_id"]: c for c in (brief.get("verification") or {}).get("claims", [])}
    return {"brief_id": brief["brief_id"], "topic": brief["topic"], "angle": brief["angle"], "format": brief["format"],
            "language": brief["language"], "content_profile": brief["content_profile"],
            "claims": [{"claim_id": c["claim_id"], "text": c["text"], "status": c["status"], "source_ids": c["source_ids"],
                        "record_claim_id": links.get(c["claim_id"], {}).get("record_claim_id"),
                        "verification_status": links.get(c["claim_id"], {}).get("verification_status")}
                       for c in brief["claims"]],
            "sources": [_source(s) for s in brief["sources"]],
            "constraints": {"avoid": list(brief["constraints"].get("avoid", [])),
                            "disclosures": list(brief["constraints"].get("disclosures", [])),
                            "tone": brief["constraints"].get("tone")},
            "provenance": {k: brief["provenance"].get(k) for k in ("created_by", "provider", "model", "created_at")},
            "editorial": {k: (brief.get("editorial") or {}).get(k) for k in ("selection_id", "score", "topics", "novelty",
                                                                               "reasons")}}


def script_section(script):
    return {"script_id": script["script_id"], "title": script["title"], "angle": script["angle"],
            "format": script["format"], "duration_seconds": script["duration_seconds"],
            "beats": [{"beat": b["beat"], "start_seconds": b["start_seconds"], "end_seconds": b["end_seconds"],
                       "narration": b["narration"], "on_screen_text": b["on_screen_text"], "claim_ids": b["claim_ids"],
                       "visual": {"description": b["visual"]["description"],
                                  "preferred_method": b["visual"]["preferred_method"],
                                  "fallback_methods": b["visual"]["fallback_methods"],
                                  "source_ids": b["visual"]["source_ids"], "avoid": b["visual"].get("avoid", [])},
                       "sound_cue": b["sound_cue"]} for b in script["beats"]],
            "captions": script["captions"], "audio": script["audio"], "disclosures": script.get("disclosures", []),
            "sources": [_source(s) for s in script["sources"]],
            "provenance": {k: script["provenance"].get(k) for k in ("created_by", "provider", "model", "created_at")}}


def plan_section(plan, posters):
    by_index = {p["index"]: p for p in posters or []}
    return {"plan_id": plan["plan_id"], "mode": plan["mode"], "blocked_for_production": plan["blocked_for_production"],
            "unverified_claim_ids": plan["unverified_claim_ids"], "assets_produced": plan["assets_produced"],
            "available_methods": plan["capabilities"]["available_methods"], "duration_seconds": plan["duration_seconds"],
            "scenes": [{"index": s["index"], "beat": s["beat"]["beat"], "start_seconds": s["beat"]["start_seconds"],
                        "end_seconds": s["beat"]["end_seconds"], "selected_method": s["selected_method"],
                        "considered_methods": s["considered_methods"], "narration": s["beat"]["narration"],
                        "on_screen_text": s["beat"]["on_screen_text"],
                        "poster": by_index.get(s["index"], {"index": s["index"], "status": "not_produced",
                                                            "reason": "preview_not_rendered", "media_id": None})}
                       for s in plan["scenes"]]}


def preview_section(manifest, video, narration):
    audio = manifest.get("audio") if isinstance(manifest.get("audio"), dict) else {}
    return {"video": video,
            "format": {k: manifest.get(k) for k in ("width", "height", "fps", "duration_seconds")},
            "audio_present": bool(manifest.get("audio_present")),
            "narration": {"configured": narration is not None, "sha256": narration["sha256"] if narration else None,
                          "source_duration_seconds": audio.get("source_duration_seconds"),
                          "note": ("Local narration WAV mixed into the preview's audio track. Voice consent, rights and "
                                   "content were not verified.") if narration else "Silent storyboard: no narration."},
            "publishable": manifest.get("publishable"), "preview_only": manifest.get("preview_only"),
            "watermark": "DRAFT - NOT FOR PRODUCTION" if manifest.get("source_blocked_for_production")
            else "LOCAL PREVIEW - NOT FOR PUBLISHING",
            "limitations": [str(x)[:200] for x in manifest.get("limitations", [])[:12]]}


def verification_section(state, root, demo_record=None):
    """The saved Step 17 record behind the brief, re-validated with the production's own policy."""
    out = {"record_id": state["config"]["record_id"], "status": "unavailable", "reason": None, "verified_at": None,
           "claims": [], "summary": None, "flags": [], "limitations": list(VERIFICATION_LIMITS)}
    record = demo_record
    if record is None:
        from ..production import PROJECT
        from ..verification import load_policy
        from ..verification_cli import load_record
        policy = state["config"]["policy"]
        try:
            if _sha(PROJECT / policy["path"]) != policy["sha256"]:
                out["reason"] = "verification_policy_changed"
                return out
            loaded, sha = load_policy(policy["path"])
            record = load_record(state["config"]["record_id"], loaded, sha, root)
        except NetworkError as error:
            out["reason"] = {"record_not_found": "evidence_record_unavailable"}.get(error.code, "evidence_record_invalid")
            return out
        except OSError:
            out["reason"] = "verification_policy_unavailable"
            return out
    out.update(status="verified", verified_at=record["verified_at"], summary=record["summary"],
               flags=list(record.get("flags", [])),
               claims=[{"claim_id": c["claim_id"], "text": c["text"], "status": c["status"],
                        "rationale_codes": list(c.get("rationale_codes", []))[:10],
                        "rationale": c.get("rationale"), "primary_support": c.get("primary_support"),
                        "independent_origins": c.get("independent_origins")} for c in record["claims"][:20]])
    return out


def restrictions(state, artifacts):
    validation = artifacts.items.get("validation", {})
    draft = validation.get("draft") if validation.get("status") == "verified" else None
    statements = list(RESTRICTIONS)
    if draft:
        statements.insert(0, "DRAFT: the script has unverified claims. The plan is blocked for production and any "
                             "preview is watermarked 'DRAFT - NOT FOR PRODUCTION'.")
    return {"publishable": False, "preview_only": True, "draft": draft,
            "claims_unverified": validation.get("claims_unverified") if draft is not None else None,
            "allow_draft_preview": state["config"]["allow_draft_preview"], "statements": statements}


def production_section(state):
    config = state["config"]
    return {"production_id": state["production_id"], "status": state["status"], "created_at": state["created_at"],
            "updated_at": state["updated_at"], "profile": config["profile"], "record_id": config["record_id"],
            "selection_run_id": config["selection_run_id"],
            "creator": {k: config["creator"][k] for k in ("adapter", "model", "paid")},
            "narration": {"configured": config["narration"] is not None,
                          "sha256": config["narration"]["sha256"] if config["narration"] else None},
            "allow_draft_preview": config["allow_draft_preview"]}


# ---------------------------------------------------------------- quality reports
def quality_reports(state, root=None, demo_reports=None):
    """(rows, rejected, skipped) for every saved Step 22 report about this production (newest first)."""
    if demo_reports is not None:
        reports, rejected, skipped = demo_reports, [], 0
    else:
        from ..quality import validate_report
        folder = Path(root if root is not None else ROOT) / "runtime/quality"
        reports, rejected, skipped = [], [], 0
        paths = sorted(folder.glob("qr-*.json")) if folder.is_dir() else []
        for path in paths[:MAX_REPORT_SCAN]:
            found, problem = safe_file(folder, path.name, MAX_JSON_BYTES)
            try:
                report = _read_json(found) if found is not None else None
                claimed = report["production_id"] if isinstance(report, dict) else None
            except (OSError, ValueError, UnicodeError, KeyError, TypeError):
                report, claimed = None, None
            if report is None or claimed is None:
                skipped += 1
                continue
            if claimed != state["production_id"]:
                continue
            try:
                validate_report(report)
                if report["report_id"] != path.stem or not REPORT_ID.match(path.stem):
                    raise NetworkError("quality_report_id_mismatch", "x")
            except NetworkError as error:
                rejected.append({"report_id": path.stem[:40] if REPORT_ID.match(path.stem) else "qr-unreadable",
                                 "code": "quality_report_id_mismatch" if error.code == "quality_report_id_mismatch"
                                 else "quality_report_invalid"})
                continue
            reports.append(report)
    last_finish = max((s["finished_at"] for s in state["stages"] if s["finished_at"]), default=None)
    rows = []
    for report in sorted(reports, key=lambda r: (r["checked_at"], r["report_id"]), reverse=True)[:MAX_REPORTS]:
        rows.append(report_row(report, state, last_finish))
    return rows, rejected[:20], skipped


def report_row(report, state, last_finish):
    seen = next((c["details"].get("status") for c in report["checks"] if c["check_id"] == "state"), None)
    reasons = []
    if last_finish is not None and report["checked_at"] < last_finish:
        reasons.append("checked_before_artifacts_finished")
    if seen is not None and seen != state["status"]:
        reasons.append("production_status_changed_since_check")
    binding = "stale" if reasons else "unverified"
    reasons.append("report_records_no_artifact_hashes")
    counts = {}
    for item in report["checks"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"report_id": report["report_id"], "checked_at": report["checked_at"], "result": report["result"],
            "binding": binding, "binding_reasons": reasons, "reasons": report["reasons"][:40], "counts": counts,
            "checks": [{"check_id": c["check_id"], "status": c["status"], "reasons": c["reasons"][:20],
                        "details": {k: v for k, v in list(c["details"].items())[:12]}} for c in report["checks"]],
            "scope": report["scope"], "notes": report["notes"][:8]}


# ---------------------------------------------------------------- timelines and history
def _seconds(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def establish(view, state, demo=False):
    """(established, reasons): event index (1-based) at which each completed stage's saved artifact is
    proven to exist in this timeline, and the quality reports the timeline proves were saved."""
    reasons, at, reports = [], {}, {}
    events = view["events"]
    completed = {s["name"]: s for s in state["stages"] if s["status"] == "completed"}
    kind = view.get("kind")
    if not demo:
        if view.get("run_id") != state["production_id"] or view.get("correlation_id") != correlation_for(state["production_id"]):
            raise NetworkError("timeline_production_mismatch", "The timeline does not belong to this production.")
    if view["issues"]:
        reasons.append("timeline_has_issues")
    if kind in ("content_quality",):
        start = view.get("started_at")
        if start is None or _seconds(state["created_at"]) > _seconds(start):
            reasons.append("timeline_predates_production")
        else:
            for name, stage in completed.items():
                if stage["finished_at"] and _seconds(stage["finished_at"]) <= _seconds(start):
                    at[name] = 0
    else:
        count = {}
        recorded = view["origin"] == "recorded"
        start = view.get("started_at")
        if recorded and (start is None or _seconds(state["created_at"]) > _seconds(start)):
            reasons.append("timeline_predates_production")
        for index, event in enumerate(events, start=1):
            name = STAGE_OF.get(event["component"])
            if name is None or event["event_type"] not in ("stage_completed", "stage_reused"):
                continue
            count[name] = count.get(name, 0) + 1
            stage = completed.get(name)
            if stage is None:
                reasons.append("timeline_completion_not_in_saved_state")
                continue
            if recorded and not demo and start is not None and stage["finished_at"]:
                finish = _seconds(stage["finished_at"])
                if event["event_type"] == "stage_completed":
                    ok = _seconds(start) <= finish <= _seconds(event["recorded_at"])
                else:
                    ok = finish <= _seconds(start)
                if not ok:
                    reasons.append("artifact_completion_time_not_in_attempt")
                    continue
            at[name] = index
        if any(n > 1 for n in count.values()):
            reasons.append("stage_completed_more_than_once")
    for index, event in enumerate(events, start=1):
        if event["component"] == QUALITY and event["event_type"] == "stage_completed":
            for ref in event["refs"]:
                if ref["kind"] == "quality_report":
                    reports[ref["id"]] = index
    reasons = list(dict.fromkeys(reasons))
    return ({} if reasons else at), reports, reasons


def stages_at(view, position):
    """Per production stage, what the first `position` events show (attempts, failures, reuse)."""
    events = view["events"][:position]
    rows = []
    for name in ("pipeline",) + STAGES + ("quality",):
        component = f"content.production.{name}"
        mine = [e for e in events if e["component"] == component]
        last = mine[-1] if mine else None
        attempts = [e["details"].get("attempt") for e in mine if e["event_type"] == "stage_started"]
        rows.append({"stage": name, "events": len(mine),
                     "state": _event_state(last) if last else "no_event_yet",
                     "last_event": last["event_type"] if last else None,
                     "reason_codes": list(last["reason_codes"]) if last else [],
                     "attempt": max([a for a in attempts if isinstance(a, int)], default=None) if attempts else None,
                     "reused": any(e["event_type"] == "stage_reused" for e in mine),
                     "failures": sum(1 for e in mine if e["event_type"] in ("stage_failed", "stage_interrupted")),
                     "recorded_at": last["recorded_at"] if last else None})
    return rows


def _event_state(event):
    return {"stage_started": "working", "stage_completed": "completed", "stage_failed": "failed",
            "stage_blocked": "blocked", "stage_interrupted": "unknown", "stage_reused": "reused"}.get(event["event_type"],
                                                                                                       "unknown")


def attempts(production_id, root=None):
    """Recorded production and quality timelines of this production, by correlation ID (bounded)."""
    from ..events.store import EventStore
    rows = []
    store = EventStore(root)
    correlation = correlation_for(production_id)
    for row in store.list("content"):
        if not row.get("readable") or row.get("correlation_id") != correlation or row.get("run_id") != production_id:
            continue
        if row.get("kind") not in ("content_production", "content_quality"):
            continue
        try:
            view = store.load(row["timeline_id"])
        except NetworkError:
            continue
        events = view["events"]
        rows.append({"timeline_id": view["timeline_id"], "kind": view["kind"], "started_at": view["started_at"],
                     "completeness": view["completeness"], "outcome": view["outcome"],
                     "stages_run": sorted({STAGE_OF[e["component"]] for e in events if e["component"] in STAGE_OF
                                           and e["event_type"] == "stage_started"}, key=STAGES.index),
                     "stages_reused": sorted({STAGE_OF[e["component"]] for e in events if e["component"] in STAGE_OF
                                              and e["event_type"] == "stage_reused"}, key=STAGES.index),
                     "failures": [{"stage": e["component"].split(".")[-1], "codes": list(e["reason_codes"])}
                                  for e in events if e["event_type"] in ("stage_failed", "stage_interrupted")][:10]})
    rows.sort(key=lambda r: (r["started_at"] or "", r["timeline_id"]))
    return rows[-MAX_ATTEMPTS:]


def state_stages(state):
    return [{"stage": s["name"], "status": s["status"], "attempts": s["attempts"], "started_at": s["started_at"],
             "finished_at": s["finished_at"], "error_code": s["error_code"]} for s in state["stages"]]


# ---------------------------------------------------------------- documents
def _timeline(view, demo):
    return {"timeline_id": "demo" if demo else view["timeline_id"], "origin": "demo" if demo else view["origin"],
            "kind": view["kind"], "completeness": view["completeness"], "issues": list(view["issues"])[:20],
            "event_count": len(view["events"]), "time_basis": view.get("time_basis"),
            "started_at": view.get("started_at")}


def _artifact_view(name, item, section):
    return {"status": item["status"], "reason": item.get("reason"), "data": section}


def sections(artifacts, state, root, visible=None, demo=None):
    """Artifact sections; with `visible` (set of stage names) everything else is withheld."""
    items, out = artifacts.items, {}

    def show(stage):
        return visible is None or stage in visible
    pairs = (("brief", "brief"), ("script", "creator"), ("validation", "validate"), ("plan", "plan"),
             ("preview", "preview"))
    for name, stage in pairs:
        key = "manifest" if name == "preview" else name
        item = items[key]
        if not show(stage):
            out[name] = {"status": "not_established_at_position", "reason": "not_established_at_position", "data": None}
            continue
        if item["status"] != "verified" or (name == "preview" and items["video"]["status"] != "verified"):
            bad = item if item["status"] != "verified" else items["video"]
            out[name] = {"status": bad["status"], "reason": bad.get("reason"), "data": None}
            continue
        if name == "brief":
            data = brief_section(artifacts.brief)
        elif name == "script":
            data = script_section(artifacts.script)
        elif name == "validation":
            data = {k: item[k] for k in ("draft", "claims_unverified", "claims_total")}
        elif name == "plan":
            data = plan_section(artifacts.plan, getattr(artifacts, "posters", None) if show("preview") else None)
        else:
            data = preview_section(artifacts.manifest, {k: items["video"].get(k) for k in ("sha256", "media_id", "bytes")},
                                   state["config"]["narration"])
        out[name] = {"status": "verified", "reason": None, "data": data}
    out["verification"] = verification_section(state, root, demo["record"] if demo else None) if show("brief") else \
        {"record_id": state["config"]["record_id"], "status": "not_established_at_position",
         "reason": "brief_not_established_at_position", "verified_at": None, "claims": [], "summary": None, "flags": [],
         "limitations": list(VERIFICATION_LIMITS)}
    return out


def _load(timeline_id, root):
    if timeline_id == "demo":
        from .content_demo import demo_inputs
        return demo_inputs()
    from ..events.cli import load_timeline
    from ..production import ProductionStore
    if timeline_id.startswith("prod-"):
        view = load_timeline(timeline_id, root)
        production_id = timeline_id
    elif timeline_id.startswith("tl-"):
        view = load_timeline(timeline_id, root)
        if view["department"] != "content" or view["kind"] not in ("content_production", "content_quality"):
            raise NetworkError("content_not_production", "This timeline is not a content production.")
        production_id = view["run_id"]
        if not isinstance(production_id, str) or not PRODUCTION_ID.match(production_id):
            raise NetworkError("content_production_missing", "This timeline does not name a saved production.")
    else:
        raise NetworkError("content_not_production", "This timeline is not a content production.")
    store = ProductionStore(root)
    state = store.read(production_id)
    artifacts = Artifacts(state, store.folder(production_id))
    return view, state, artifacts, None


def content_latest(timeline_id, root=None):
    view, state, artifacts, demo = _load(timeline_id, root)
    rows, rejected, skipped = quality_reports(state, root, demo["reports"] if demo else None)
    doc = {"contract": "hq_content", "version": "1.0", "view": "latest", "demo": demo is not None, "read_only": True,
           "label": "Latest saved production: every artifact re-verified now. Not tied to the replay position.",
           "timeline": _timeline(view, demo is not None), "production": production_section(state),
           "restrictions": restrictions(state, artifacts), "stages": state_stages(state),
           "trace_entries": len(state["trace"]),
           "attempts": demo["attempts"] if demo else attempts(state["production_id"], root),
           "artifacts": sections(artifacts, state, root, demo=demo),
           "quality": {"reports": rows, "rejected_reports": rejected, "unreadable_reports_skipped": skipped,
                       "binding_note": QUALITY_BINDING,
                       "status": "available" if rows else ("rejected" if rejected else "none_saved")},
           "notice": DEMO_NOTICE if demo else NOTICE}
    return check(doc)


def content_at(timeline_id, position, root=None):
    view, state, artifacts, demo = _load(timeline_id, root)
    events = view["events"]
    if not isinstance(position, int) or not 0 <= position <= len(events):
        raise NetworkError("content_position_out_of_range", "The replay position is outside this timeline.")
    established, report_at, reasons = establish(view, state, demo=demo is not None)
    event = events[position - 1] if position else None
    doc = {"contract": "hq_content", "version": "1.0", "view": "at_position", "demo": demo is not None, "read_only": True,
           "timeline": _timeline(view, demo is not None), "production": production_section(state),
           "position": position, "positions": len(events),
           "event": {k: event[k] for k in ("sequence", "component", "event_type", "status", "recorded_at")} if event else None,
           "historical": {"status": "unavailable" if reasons else "available", "reasons": reasons},
           "stages": stages_at(view, position), "artifacts": None, "quality": None,
           "restrictions": {"publishable": False, "preview_only": True, "draft": None, "claims_unverified": None,
                            "allow_draft_preview": state["config"]["allow_draft_preview"],
                            "statements": list(RESTRICTIONS)},
           "notice": DEMO_NOTICE if demo else NOTICE}
    if reasons:
        return check(doc)
    visible = {name for name, index in established.items() if index <= position}
    doc["artifacts"] = sections(artifacts, state, root, visible=visible, demo=demo)
    if "validate" in visible:
        doc["restrictions"] = restrictions(state, artifacts)
    shown = [rid for rid, index in report_at.items() if index <= position]
    if not report_at:
        doc["quality"] = {"status": "not_in_timeline", "reports": [], "binding_note": QUALITY_BINDING}
    elif not shown:
        doc["quality"] = {"status": "not_yet", "reports": [], "binding_note": QUALITY_BINDING}
    else:
        rows, _, _ = quality_reports(state, root, demo["reports"] if demo else None)
        by_id = {r["report_id"]: r for r in rows}
        doc["quality"] = {"status": "available" if all(r in by_id for r in shown) else "referenced_report_unavailable",
                          "reports": [by_id[r] for r in shown if r in by_id], "binding_note": QUALITY_BINDING}
    return check(doc)


# ---------------------------------------------------------------- media
def media_file(production_id, identifier, root=None):
    """(bytes, content_type) of one validated preview file of this production."""
    if not isinstance(production_id, str) or not PRODUCTION_ID.match(production_id) or not MEDIA_ID.match(str(identifier)):
        raise NetworkError("invalid_media_request", "Use production=prod-... and id=med-....")
    from ..production import ProductionStore
    store = ProductionStore(root)
    state = store.read(production_id)
    folder = store.folder(production_id)
    artifacts = Artifacts(state, folder)
    entry = artifacts.media.get(identifier)
    if entry is None:
        raise NetworkError("media_not_found", "No validated preview file with this ID in this production.")
    limit = MAX_VIDEO_BYTES if entry["kind"] == "video" else MAX_POSTER_BYTES
    path, problem = safe_file(folder, entry["path"], limit)
    if path is None:
        raise NetworkError("media_not_found", "No validated preview file with this ID in this production.")
    data = path.read_bytes()                       # one read: what is checked is exactly what is served
    if entry["kind"] == "video" and _sha(data) != entry["sha256"]:
        raise NetworkError("media_changed", "The preview file changed; reload the desk.")
    if entry["kind"] == "poster" and not data.startswith(PNG):
        raise NetworkError("media_changed", "The poster changed; reload the desk.")
    return data, "video/mp4" if entry["kind"] == "video" else "image/png"
