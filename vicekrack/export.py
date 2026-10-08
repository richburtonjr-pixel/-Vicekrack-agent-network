"""Step 38: portable, locally viewable content-preview packages.

`export-preview` copies an allowlisted set of files from one completed production into a
self-contained directory under runtime/exports/ (ignored by Git):

  package.json                  package manifest (`content_preview_package` 1.0): every payload
                                file with relative path, role, size and SHA-256. It does not list
                                itself, and no payload file contains its hash (no circularity).
  index.html                    static, offline review page: escaped text, relative links only,
                                no JavaScript, fonts, trackers or remote resources.
  media/preview.mp4             the preview exactly as rendered (narration already mixed in).
  media/scene-N.png             scene posters.
  content/script.json           the script, original bytes.
  content/provenance.json       DERIVED readable summary of the brief, sources and verification
                                record (source IDs and hashes kept).
  quality/quality-report.json   the Step 36 bound quality report, original bytes.
  review/review-summary.json    DERIVED summary of the Step 37 decisions on this report, a
                                snapshot at export time. Reviewer labels and notes are left out
                                unless explicitly included.

Purposes: `review_copy` (visibly "For review") and `approved_preview` (needs a currently
applicable Step 37 approval of this exact report and binding). Neither grants permission to
publish: `publishable` stays false, draft markings are kept, and technical checks, factual
verification and rights clearance stay separate.

Gates: completed production; a 1.1 report whose binding matches the current files; no
technical `fail`; no corrupted review history; every required artifact present and equal
to its bound hash. The export holds the production lock (no resume, quality check or
review can write meanwhile), reads every source once into a snapshot, writes the payload
from those bytes into a staging directory, re-reads and re-hashes every staged file,
re-hashes every source, and only then renames the staging directory into place under a
new package ID (never overwriting).

Never exported: production state, configuration, credentials, environment data, raw
exceptions, original narration files, original review records, evidence/selection files.

`export-verify PATH` checks a package anywhere (another directory or computer) without the
production: exact file inventory, no links, sizes, hashes, schemas and internal references.
Hashes are consistency checks, not signatures: anyone who can edit the package can also
rewrite its manifest consistently.
"""

import html
import json
import os
import re
import secrets
import shutil
import stat
from datetime import datetime, timezone
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator

from .artifact_binding import (MAX_JSON_BYTES, MAX_POSTER_BYTES, MAX_VIDEO_BYTES, PNG, norm, safe_file,
                               sha256_bytes, validate_manifest, verify_binding)
from .errors import NetworkError
from .orchestrator import ROOT

CONTRACT, VERSION = "content_preview_package", "1.0"
PURPOSES = ("review_copy", "approved_preview")
PACKAGE_ID = re.compile(r"^pkg-[0-9a-f]{24}$")
MANIFEST = "package.json"
MAX_PACKAGE_FILES = 40
ROLE_LIMITS = {"video": MAX_VIDEO_BYTES, "poster": MAX_POSTER_BYTES}
INTEGRITY_NOTE = ("SHA-256 hashes let anyone check that these files are consistent with this manifest. They are not "
                  "signatures: whoever can edit the package can rewrite the manifest too, and nothing here proves who "
                  "made it.")
STATEMENTS = [
    "publishable: false. This package is a local preview copy; it grants no permission to publish or upload.",
    "Technical checks (the quality report) are not factual verification and not rights clearance.",
    "Factual status comes from the saved verification record at production time; nothing here re-checks facts.",
    "Image, footage, music, narration and voice rights were never checked.",
    "Review status and evidence freshness are snapshots taken when this package was exported; they are not "
    "promises that remain current.",
]
PURPOSE_TEXT = {"review_copy": "FOR REVIEW - not approved, not for publishing",
                "approved_preview": "APPROVED FOR PREVIEW ONLY - not approved for publishing"}


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(document):
    return (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


@lru_cache(maxsize=None)
def _validator(name):
    schema = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _valid(name, document):
    return isinstance(document, dict) and next(_validator(name).iter_errors(document), None) is None


def _between_checks():
    """Seam for tests: runs after the payload is staged and before sources are re-checked."""


# ---------------------------------------------------------------- snapshot of the sources
class _Source:
    """One source file, read once."""

    def __init__(self, path, data):
        self.path, self.data, self.sha = path, data, sha256_bytes(data)

    def unchanged(self):
        try:
            info = os.lstat(self.path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return False
            return Path(self.path).read_bytes() == self.data
        except OSError:
            return False


def _read(folder, relative, limit, code):
    path, problem = safe_file(folder, relative, limit)
    if path is None:
        raise NetworkError(code if problem == "missing" else "unsafe_or_invalid_artifact_path",
                           "A required artifact is missing or not a regular file inside the production.")
    try:
        return _Source(path, path.read_bytes())
    except OSError:
        raise NetworkError(code, "A required artifact could not be read.") from None


def _json(source, code):
    try:
        return json.loads(source.data.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise NetworkError(code, "A required artifact is not valid JSON.") from None


class PreviewExporter:
    def __init__(self, root=None, clock=None):
        self.root = root
        self.clock = clock or utc_now

    # -- public
    def export(self, production_id, report_id, *, purpose, include_reviewer_labels=False, include_review_notes=False):
        from .production import ProductionStore
        if purpose not in PURPOSES:
            raise NetworkError("invalid_export_purpose", "Use review_copy or approved_preview.")
        store = ProductionStore(self.root)
        folder = store.folder(production_id)
        base = Path(self.root if self.root is not None else ROOT) / "runtime/exports"
        with store.lock(production_id):
            snap = self._snapshot(store, production_id, report_id, folder)
            self._gates(snap, purpose)
            package_id = "pkg-" + secrets.token_hex(12)
            exported_at = self.clock()
            files = self._payload(snap, purpose, package_id, exported_at, include_reviewer_labels, include_review_notes)
            manifest = self._manifest(snap, purpose, package_id, exported_at, files,
                                      include_reviewer_labels, include_review_notes)
            from .persistence import reject_secrets
            reject_secrets({"manifest": manifest, "summaries": [json.loads(files[p][1]) for p in files
                                                                 if p.endswith(".json") and files[p][2]]})
            if not _valid("content-preview-package.schema.json", manifest):
                raise NetworkError("invalid_package_manifest", "The package manifest did not match its contract.")
            target = self._publish(base, package_id, files, manifest, snap)
        return manifest, target

    # -- snapshot and gates
    def _snapshot(self, store, production_id, report_id, folder):
        from .quality import load_report
        from .review import _read_history, history
        state_source = _read(folder, "state.json", MAX_JSON_BYTES, "production_not_found")
        state = store.read(production_id)
        if _json(state_source, "invalid_production_state") != state:
            raise NetworkError("sources_changed_during_export", "The production changed while it was being read.")
        if state["status"] != "completed":
            raise NetworkError("production_not_completed", "Only a completed production can be exported.")
        report = load_report(report_id, self.root)
        report_source = _read(Path(self.root if self.root is not None else ROOT) / "runtime/quality",
                              report_id + ".json", MAX_JSON_BYTES, "quality_report_not_found")
        if _json(report_source, "invalid_quality_report") != report:
            raise NetworkError("sources_changed_during_export", "The quality report changed while it was being read.")
        if report["production_id"] != production_id:
            raise NetworkError("report_for_another_production", "The quality report is for another production.")
        stages = {s["name"]: s["artifacts"] for s in state["stages"]}
        sources = {"state": state_source, "report": report_source}
        for key, (stage, field, limit) in {"brief": ("brief", "brief_path", MAX_JSON_BYTES),
                                           "script": ("creator", "script_path", MAX_JSON_BYTES),
                                           "plan": ("plan", "plan_path", MAX_JSON_BYTES),
                                           "manifest": ("preview", "manifest_file", MAX_JSON_BYTES),
                                           "video": ("preview", "preview_file", MAX_VIDEO_BYTES)}.items():
            sources[key] = _read(folder, norm(stages[stage][field]), limit, "required_artifact_missing")
        preview_manifest = _json(sources["manifest"], "invalid_preview_manifest")
        validate_manifest(preview_manifest)
        package_dir = PurePosixPath(norm(stages["preview"]["manifest_file"])).parent
        posters = []
        for scene in preview_manifest["scenes"]:
            ref = str(package_dir / scene["poster"])
            source = _read(folder, ref, MAX_POSTER_BYTES, "required_artifact_missing")
            if not source.data.startswith(PNG):
                raise NetworkError("required_artifact_invalid", "A scene poster is not a PNG file.")
            posters.append((scene["index"], ref, source))
            sources[f"poster_{scene['index']}"] = source
        records, corrupted = _read_history(production_id, self.root)
        review_doc = history(production_id, self.root, state=state, folder=folder)
        review_files = {}
        reviews_dir = Path(self.root if self.root is not None else ROOT) / "runtime/reviews" / production_id
        for record in records:
            name = f"{record['sequence']:06d}-{record['review_id']}.json"
            review_files[record["review_id"]] = _read(reviews_dir, name, MAX_JSON_BYTES, "review_history_corrupted")
            sources["review_" + record["review_id"]] = review_files[record["review_id"]]
        return {"state": state, "report": report, "folder": folder, "stages": stages, "sources": sources,
                "preview_manifest": preview_manifest, "posters": posters, "records": records,
                "corrupted": corrupted, "review": review_doc, "review_files": review_files,
                "brief": _json(sources["brief"], "invalid_brief"), "script": _json(sources["script"], "invalid_script"),
                "plan": _json(sources["plan"], "invalid_plan")}

    def _gates(self, snap, purpose):
        from .review import binding_digest
        report, state = snap["report"], snap["state"]
        binding = verify_binding(report, state, snap["folder"], self.root)
        if binding["status"] != "matching":
            raise NetworkError("binding_not_matching" if binding["status"] != "legacy_unverified" else "report_not_bound",
                               "Export needs a Step 36 quality report that still matches the current files.")
        if report["result"] == "fail":
            raise NetworkError("technical_result_fail", "A production whose quality report failed cannot be exported.")
        if snap["corrupted"]:
            raise NetworkError("review_history_corrupted", "The review history is corrupted; nothing was exported.")
        rows = {(r["role"], r["index"]): r["sha256"] for r in report["binding"]["artifacts"]}
        expected = {"state": ("production_state", None), "brief": ("brief", None), "script": ("script", None),
                    "plan": ("scene_plan", None), "manifest": ("preview_manifest", None), "video": ("video", None)}
        expected.update({f"poster_{i}": ("poster", i) for i, _, _ in snap["posters"]})
        for key, row in expected.items():
            if rows.get(row) != snap["sources"][key].sha:
                raise NetworkError("required_artifact_changed", "A required artifact differs from the bound report.")
        snap["digest"] = binding_digest(report)
        summary = snap["review"]["summary"]
        latest = next((r for r in snap["review"]["reviews"] if r["review_id"] == summary["latest_review_id"]), None)
        snap["approval"] = latest if (latest and summary["current_preview_approval"]
                                      and latest["report_id"] == report["report_id"]
                                      and latest["binding_digest"] == snap["digest"]) else None
        if purpose == "approved_preview" and snap["approval"] is None:
            raise NetworkError("no_current_preview_approval",
                               "approved_preview needs a current Step 37 approval of this exact report and binding.")

    # -- payload
    def _payload(self, snap, purpose, package_id, exported_at, labels, notes):
        """{relative path: (role, bytes, derived, source)} for every payload file except the manifest."""
        from .hq.content import brief_section, script_section, verification_section
        files = {"media/preview.mp4": ("video", snap["sources"]["video"].data, False,
                                       {"sha256": snap["sources"]["video"].sha})}
        for index, _, source in snap["posters"]:
            files[f"media/scene-{index}.png"] = ("poster", source.data, False, {"sha256": source.sha, "index": index})
        files["content/script.json"] = ("script", snap["sources"]["script"].data, False,
                                        {"sha256": snap["sources"]["script"].sha, "id": snap["script"]["script_id"]})
        files["quality/quality-report.json"] = ("quality_report", snap["sources"]["report"].data, False,
                                                {"sha256": snap["sources"]["report"].sha, "id": snap["report"]["report_id"]})
        record_row = next((r for r in snap["report"]["binding"]["artifacts"] if r["role"] == "verification_record"), {})
        verification = verification_section(snap["state"], self.root)
        provenance = {"contract": "content_provenance_summary", "version": "1.0", "derived": True,
                      "statement": "Derived, readable summary made at export time. It is not the original brief or "
                                   "verification record; their IDs and hashes are listed so the originals can be matched.",
                      "sources_of_truth": {
                          "brief": {"id": snap["brief"]["brief_id"], "sha256": snap["sources"]["brief"].sha},
                          "verification_record": {"id": snap["state"]["config"]["record_id"],
                                                  "sha256": record_row.get("sha256")}},
                      "brief": {k: v for k, v in brief_section(snap["brief"]).items() if k != "editorial"},
                      "verification": {k: verification[k] for k in ("record_id", "status", "verified_at", "summary",
                                                                     "flags", "claims", "limitations")},
                      "evidence_at_export": snap["review"]["evidence_now"]}
        for source in provenance["brief"]["sources"]:
            source.pop("link_allowed", None)
        files["content/provenance.json"] = ("provenance_summary", _canonical(provenance), True,
                                            {"sha256": snap["sources"]["brief"].sha, "id": snap["brief"]["brief_id"]})
        review = self._review_summary(snap, exported_at, labels, notes)
        files["review/review-summary.json"] = ("review_summary", _canonical(review), True,
                                               {"sha256": None, "id": snap["approval"]["review_id"]
                                                if snap["approval"] else None})
        script = script_section(snap["script"])
        page = render_page(snap, purpose, package_id, exported_at, provenance, review, script, sorted(files))
        files["index.html"] = ("review_page", page.encode("utf-8"), True, {"sha256": None, "id": None})
        return files

    def _review_summary(self, snap, exported_at, labels, notes):
        decisions = []
        for row in snap["review"]["reviews"]:
            if row["report_id"] != snap["report"]["report_id"]:
                continue
            item = {k: row[k] for k in ("review_id", "sequence", "decision", "recorded_at", "report_id", "binding_digest",
                                        "acknowledgments", "supersedes", "superseded_by")}
            item.update(record_sha256=snap["review_files"][row["review_id"]].sha,
                        conditions_at_review=row["conditions"],
                        applicability_at_export=row["applicability"],
                        artifact_binding_at_export=row["artifact_binding_now"],
                        current_preview_approval_at_export=row["current_preview_approval"],
                        reasons_at_export=row["reasons"])
            if labels:
                item["reviewer_label"] = row["reviewer_label"]
                item["reviewer_authenticated"] = False
            if notes:
                item["notes"] = row["notes"]
            decisions.append(item)
        approval = snap["approval"]
        return {"contract": "content_review_summary", "version": "1.0", "derived": True,
                "statement": "Derived summary of the Step 37 review records about this quality report, made at export "
                             "time. It is not an original record; each decision keeps its review ID and the SHA-256 "
                             "of its original file. Applicability and evidence freshness are a snapshot at export "
                             "time, not a promise that they remain current.",
                "snapshot_at": exported_at, "report_id": snap["report"]["report_id"], "binding_digest": snap["digest"],
                "history_status": snap["review"]["status"],
                "other_decisions_not_about_this_report": sum(1 for r in snap["review"]["reviews"]
                                                             if r["report_id"] != snap["report"]["report_id"]),
                "current_preview_approval_at_export": approval is not None,
                "approval": {"review_id": approval["review_id"], "acknowledgments": approval["acknowledgments"],
                             "conditions_at_review": approval["conditions"],
                             "meaning": "Accepts this preview only. Not permission to publish, not rights clearance, "
                                        "not fact verification."} if approval else None,
                "evidence_at_export": snap["review"]["evidence_now"],
                "reviewer_labels_included": bool(labels), "review_notes_included": bool(notes),
                "decisions": decisions[:50]}

    def _manifest(self, snap, purpose, package_id, exported_at, files, labels, notes):
        report, state = snap["report"], snap["state"]
        draft = next(s for s in state["stages"] if s["name"] == "validate")["artifacts"].get("draft") is True
        rows = [{"path": path, "role": role, "bytes": len(data), "sha256": sha256_bytes(data), "derived": derived,
                 "source_id": source.get("id"), "source_sha256": source.get("sha256"), "index": source.get("index")}
                for path, (role, data, derived, source) in sorted(files.items())]
        return {"contract": CONTRACT, "version": VERSION, "package_id": package_id, "purpose": purpose,
                "purpose_label": PURPOSE_TEXT[purpose], "exported_at": exported_at,
                "production_id": state["production_id"],
                "source": {"quality_report_id": report["report_id"], "quality_report_sha256": snap["sources"]["report"].sha,
                           "binding_digest": snap["digest"],
                           "preview_manifest_version": snap["preview_manifest"]["version"],
                           "preview_manifest_sha256": snap["sources"]["manifest"].sha,
                           "approval_review_id": snap["approval"]["review_id"] if snap["approval"] else None},
                "snapshot_at_export": {"technical_result": report["result"], "artifact_binding": "matching",
                                       "review_history": snap["review"]["status"],
                                       "current_preview_approval": snap["approval"] is not None,
                                       "evidence": snap["review"]["evidence_now"]},
                "restrictions": {"publishable": False, "preview_only": True, "draft": draft,
                                 "for_review": purpose == "review_copy",
                                 "watermark": "DRAFT - NOT FOR PRODUCTION" if draft else "LOCAL PREVIEW - NOT FOR PUBLISHING"},
                "statements": list(STATEMENTS),
                "privacy": {"reviewer_labels_included": bool(labels), "review_notes_included": bool(notes),
                            "narration_source_included": False, "production_state_included": False,
                            "configuration_included": False},
                "integrity": {"hash": "sha256", "manifest_lists_itself": False, "note": INTEGRITY_NOTE},
                "files": rows}

    # -- publication
    def _publish(self, base, package_id, files, manifest, snap):
        if base.is_symlink() or (base.exists() and not base.is_dir()):
            raise NetworkError("invalid_export_storage", "Export storage must be a real directory.")
        target = base / package_id
        staging = base / f".staging-{package_id}"
        try:
            base.mkdir(parents=True, exist_ok=True)
            staging.mkdir()
        except OSError:
            raise NetworkError("export_write_failed", "Could not create the package directory.") from None
        try:
            payload = dict(files)
            for relative, (_, data, _, _) in list(payload.items()) + [(MANIFEST, (None, _canonical(manifest), None, None))]:
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            for row in manifest["files"]:                       # validate the copied bytes
                written = (staging / row["path"]).read_bytes()
                if len(written) != row["bytes"] or sha256_bytes(written) != row["sha256"]:
                    raise NetworkError("export_copy_mismatch", "A copied file does not match its source bytes.")
            _between_checks()
            if not all(source.unchanged() for source in snap["sources"].values()):
                raise NetworkError("sources_changed_during_export",
                                   "A source file changed while the package was being made; nothing was exported.")
            from .review import _read_history
            records, corrupted = _read_history(snap["state"]["production_id"], self.root)
            if corrupted or [r["review_id"] for r in records] != [r["review_id"] for r in snap["records"]]:
                raise NetworkError("sources_changed_during_export", "The review history changed during export.")
            if target.exists() or target.is_symlink():
                raise NetworkError("package_exists", "A package with this ID already exists; nothing was overwritten.")
            os.rename(staging, target)
        except NetworkError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            raise NetworkError("export_write_failed", "Could not write the package completely; nothing was exported.") from None
        return target


# ---------------------------------------------------------------- static review page
def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def render_page(snap, purpose, package_id, exported_at, provenance, review, script, paths):
    """A static HTML page: escaped text, relative links to files in this package, no script or remote resource."""
    report, manifest = snap["report"], snap["preview_manifest"]
    draft = next(s for s in snap["state"]["stages"] if s["name"] == "validate")["artifacts"].get("draft") is True
    banner = PURPOSE_TEXT[purpose]
    out = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' file:; '
           'media-src \'self\' file:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
           '<meta name="referrer" content="no-referrer">',
           f"<title>{_e(banner)} · {_e(package_id)}</title>",
           "<style>body{margin:0;background:#0b0f14;color:#e6edf3;font:15px/1.5 system-ui,-apple-system,'Segoe UI',"
           "Roboto,sans-serif}main{max-width:1000px;margin:0 auto;padding:16px}h1,h2{line-height:1.25}"
           ".banner{padding:12px 14px;border-radius:8px;font-weight:700;letter-spacing:.02em}"
           ".review{background:#3a2a00;border:2px solid #f0b429;color:#ffe08a}"
           ".approved{background:#0c2a1c;border:2px solid #3fb950;color:#a7f3c0}"
           ".draft{background:#3b0d12;border:2px solid #f85149;color:#ffc1bd;margin-top:8px}"
           "section{margin-top:22px;padding:14px;border:1px solid #1f2a36;border-radius:10px;background:#0f151c}"
           "table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-top:1px solid #1f2a36;"
           "vertical-align:top}.wrap{overflow-x:auto}code{overflow-wrap:anywhere}video{width:100%;max-width:360px;"
           "background:#000}.posters{display:flex;flex-wrap:wrap;gap:8px}.posters img{width:108px;height:192px;"
           "object-fit:cover;border:1px solid #1f2a36}.muted{color:#9aa7b4}.notes{white-space:pre-wrap;"
           "background:#0b1016;padding:8px;border-left:3px solid #2b3a4a}a{color:#7cc4ff}</style></head><body><main>",
           f'<p class="banner {"review" if purpose == "review_copy" else "approved"}">{_e(banner)}</p>']
    if draft:
        out.append('<p class="banner draft">DRAFT - NOT FOR PRODUCTION: the script has unverified claims.</p>')
    out += [f"<h1>{_e(script['title'])}</h1>",
            f'<p class="muted">Package {_e(package_id)} · production {_e(snap["state"]["production_id"])} · exported '
            f"{_e(exported_at)} (UTC). publishable: false.</p>",
            "<section><h2>What this package is (and is not)</h2><ul>"]
    out += [f"<li>{_e(s)}</li>" for s in STATEMENTS]
    out.append(f"<li>{_e(INTEGRITY_NOTE)} Check it with <code>python -m vicekrack export-verify PATH</code>.</li></ul></section>")
    first = f"media/scene-{snap['posters'][0][0]}.png" if snap["posters"] else None
    out += ["<section><h2>Preview</h2>",
            f'<video controls preload="metadata" src="media/preview.mp4"{f" poster={_q(first)}" if first else ""}>'
            '</video>',
            f'<p><a href="media/preview.mp4">Open the preview video</a> · {_e(manifest["width"])}×{_e(manifest["height"])}'
            f' · {_e(manifest["duration_seconds"])} s · audio {"present (narration as rendered)" if manifest["audio_present"] else "none"}'
            "</p><div class=\"posters\">"]
    for index, _, _ in snap["posters"]:
        out.append(f'<a href="media/scene-{index}.png"><img src="media/scene-{index}.png" alt="Scene {index} poster"></a>')
    out.append("</div></section>")
    out += ["<section><h2>Script</h2>", '<div class="wrap"><table><tr><th>Beat</th><th>Time</th><th>Narration</th>'
            "<th>On screen</th><th>Visual</th><th>Claims</th></tr>"]
    for beat in script["beats"]:
        out.append(f"<tr><td>{_e(beat['beat'])}</td><td>{_e(beat['start_seconds'])}–{_e(beat['end_seconds'])} s</td>"
                   f"<td>{_e(beat['narration'])}</td><td>{_e(beat['on_screen_text'])}</td>"
                   f"<td>{_e(beat['visual']['description'])}</td><td>{_e(', '.join(beat['claim_ids']))}</td></tr>")
    out.append('</table></div><p><a href="content/script.json">script.json</a> (original bytes)</p></section>')
    brief, verification = provenance["brief"], provenance["verification"]
    out += ["<section><h2>Sources and provenance (derived summary)</h2>",
            f"<p class=\"muted\">{_e(provenance['statement'])}</p>",
            f"<p>Brief {_e(brief['brief_id'])} · topic {_e(brief['topic'])} · verification record "
            f"{_e(verification['record_id'])} ({_e(verification['status'])}, verified {_e(verification['verified_at'])}) "
            f"· evidence at export: {_e(provenance['evidence_at_export']['status'])}</p>",
            '<div class="wrap"><table><tr><th>Claim</th><th>Text</th><th>Status</th><th>Sources</th></tr>']
    for claim in brief["claims"]:
        out.append(f"<tr><td>{_e(claim['claim_id'])}</td><td>{_e(claim['text'])}</td><td>{_e(claim['status'])}</td>"
                   f"<td>{_e(', '.join(claim['source_ids']))}</td></tr>")
    out.append('</table></div><div class="wrap"><table><tr><th>Source</th><th>Title</th><th>Publisher</th>'
               "<th>Address (text only, not a link)</th></tr>")
    for source in brief["sources"]:
        out.append(f"<tr><td>{_e(source['source_id'])}</td><td>{_e(source['title'])}</td>"
                   f"<td>{_e(source['publisher'])}</td><td><code>{_e(source['url'])}</code></td></tr>")
    out.append("</table></div><ul>" + "".join(f"<li>{_e(x)}</li>" for x in verification["limitations"]) +
               '</ul><p><a href="content/provenance.json">provenance.json</a> (derived)</p></section>')
    out += ["<section><h2>Technical quality report</h2>",
            f"<p>Report {_e(report['report_id'])} · checked {_e(report['checked_at'])} · result "
            f"<strong>{_e(report['result'])}</strong> · artifact binding at export: matching · binding digest "
            f"<code>{_e(snap['digest'])}</code></p>",
            '<div class="wrap"><table><tr><th>Check</th><th>Status</th><th>Reasons</th></tr>']
    for check in report["checks"]:
        out.append(f"<tr><td>{_e(check['check_id'])}</td><td>{_e(check['status'])}</td>"
                   f"<td>{_e(', '.join(check['reasons']) or '-')}</td></tr>")
    out.append('</table></div><p class="muted">Technical checks only: facts not verified, rights not cleared.</p>'
               '<p><a href="quality/quality-report.json">quality-report.json</a> (original bytes)</p></section>')
    out += ["<section><h2>Human review (snapshot at export)</h2>", f"<p class=\"muted\">{_e(review['statement'])}</p>",
            f"<p>Current preview approval at export: <strong>{'yes' if review['current_preview_approval_at_export'] else 'no'}"
            f"</strong> · evidence at export: {_e((review['evidence_at_export'] or {}).get('status'))} · reviewer labels "
            f"{'included' if review['reviewer_labels_included'] else 'not included'} · notes "
            f"{'included' if review['review_notes_included'] else 'not included'}</p>"]
    if review["approval"]:
        acks = ", ".join(review["approval"]["acknowledgments"]) or "none needed"
        out.append(f"<p>Approval {_e(review['approval']['review_id'])} · acknowledged: {_e(acks)} · "
                   f"{_e(review['approval']['meaning'])}</p>")
    if review["decisions"]:
        out.append('<div class="wrap"><table><tr><th>#</th><th>Decision</th><th>Recorded</th><th>At export</th>'
                   "<th>Acknowledged</th><th>Reviewer</th></tr>")
        for item in review["decisions"]:
            reviewer = (_e(item["reviewer_label"]) + " (self-declared)") if "reviewer_label" in item else "not included"
            out.append(f"<tr><td>{_e(item['sequence'])}</td><td>{_e(item['decision'])}</td><td>{_e(item['recorded_at'])}</td>"
                       f"<td>{_e(item['applicability_at_export'])}</td><td>{_e(', '.join(item['acknowledgments']) or '-')}"
                       f"</td><td>{reviewer}</td></tr>")
            if item.get("notes"):
                out.append(f'<tr><td></td><td colspan="5"><div class="notes">{_e(item["notes"])}</div></td></tr>')
        out.append("</table></div>")
    else:
        out.append("<p>No review decision about this report was saved at export time.</p>")
    out.append('<p><a href="review/review-summary.json">review-summary.json</a> (derived)</p></section>')
    out.append("<section><h2>Files</h2><ul>" + "".join(f'<li><a href="{_e(p)}">{_e(p)}</a></li>' for p in paths) +
               f'<li><a href="{MANIFEST}">{MANIFEST}</a> (package manifest)</li></ul></section></main></body></html>')
    return "\n".join(out) + "\n"


def _q(value):
    return '"' + _e(value) + '"'


# ---------------------------------------------------------------- verification (read-only, portable)
def verify_package(path):
    """Check a package directory on its own: inventory, links, sizes, hashes, schemas, references."""
    from .quality import validate_report
    problems = []
    folder = Path(path)
    result = {"status": "inconsistent", "package_id": None, "purpose": None, "exported_at": None, "files_checked": 0,
              "problems": problems, "note": INTEGRITY_NOTE}
    try:
        if folder.is_symlink() or not folder.is_dir():
            problems.append("package_not_a_directory")
            return result
        manifest_path, problem = safe_file(folder, MANIFEST, MAX_JSON_BYTES)
        if manifest_path is None:
            problems.append("package_manifest_" + problem)
            return result
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        problems.append("package_manifest_unreadable")
        return result
    if not _valid("content-preview-package.schema.json", manifest):
        problems.append("package_manifest_invalid")
        return result
    result.update(package_id=manifest["package_id"], purpose=manifest["purpose"], exported_at=manifest["exported_at"],
                  publishable=False, snapshot_at_export=manifest["snapshot_at_export"])
    listed = {row["path"]: row for row in manifest["files"]}
    present = set()
    for current, dirs, names in os.walk(folder, followlinks=False):
        for name in dirs + names:
            full = Path(current) / name
            relative = full.relative_to(folder).as_posix()
            if full.is_symlink():
                problems.append("link_in_package")
            elif name in names:
                present.add(relative)
            elif not full.is_dir():
                problems.append("special_file_in_package")
    present.discard(MANIFEST)
    if present - set(listed):
        problems.append("unlisted_file_in_package")
    if set(listed) - present:
        problems.append("listed_file_missing")
    data = {}
    for relative, row in sorted(listed.items()):
        if relative not in present:
            continue
        limit = ROLE_LIMITS.get(row["role"], MAX_JSON_BYTES)
        file, problem = safe_file(folder, relative, limit)
        if file is None:
            problems.append("file_" + problem)
            continue
        content = file.read_bytes()
        result["files_checked"] += 1
        if len(content) != row["bytes"]:
            problems.append("size_mismatch")
        elif sha256_bytes(content) != row["sha256"]:
            problems.append("hash_mismatch")
        else:
            data[relative] = content
    problems.extend(_references(manifest, data, validate_report))
    result["problems"] = list(dict.fromkeys(problems))
    if not result["problems"]:
        result["status"] = "consistent"
    return result


def _references(manifest, data, validate_report):
    """Internal references between the manifest and the files whose bytes verified."""
    problems = []

    def load(path):
        try:
            return json.loads(data[path].decode("utf-8")) if path in data else None
        except (UnicodeError, ValueError):
            problems.append("payload_json_unreadable")
            return None
    roles = {}
    for row in manifest["files"]:
        roles.setdefault(row["role"], []).append(row)
    for role, count in (("video", 1), ("script", 1), ("quality_report", 1), ("provenance_summary", 1),
                        ("review_summary", 1), ("review_page", 1)):
        if len(roles.get(role, [])) != count:
            problems.append("package_role_count_wrong_" + role)
    report = load("quality/quality-report.json")
    if report is not None:
        from .review import binding_digest
        try:
            validate_report(report)
        except NetworkError:
            problems.append("quality_report_invalid")
            report = None
    if report is not None:
        source = manifest["source"]
        if report["report_id"] != source["quality_report_id"] or report["production_id"] != manifest["production_id"]:
            problems.append("quality_report_reference_mismatch")
        if sha256_bytes(data["quality/quality-report.json"]) != source["quality_report_sha256"]:
            problems.append("quality_report_not_original")
        if report.get("version") != "1.1" or binding_digest(report) != source["binding_digest"]:
            problems.append("binding_digest_mismatch")
        if report["result"] == "fail" or manifest["snapshot_at_export"]["technical_result"] != report["result"]:
            problems.append("technical_result_mismatch")
        bound = {(r["role"], r["index"]): r["sha256"] for r in report.get("binding", {}).get("artifacts", [])}
        for row in manifest["files"]:
            key = {"video": ("video", None), "script": ("script", None), "poster": ("poster", row["index"])}.get(row["role"])
            if key is not None and bound.get(key) != row["sha256"]:
                problems.append("payload_not_bound_by_report")
        if bound.get(("preview_manifest", None)) != source["preview_manifest_sha256"]:
            problems.append("preview_manifest_reference_mismatch")
    review = load("review/review-summary.json")
    if review is not None:
        if not _valid("content-review-summary.schema.json", review):
            problems.append("review_summary_invalid")
        else:
            if review["report_id"] != manifest["source"]["quality_report_id"] or \
                    review["binding_digest"] != manifest["source"]["binding_digest"]:
                problems.append("review_summary_reference_mismatch")
            approval = review["approval"]
            approved = manifest["purpose"] == "approved_preview"
            if approved and (approval is None or approval["review_id"] != manifest["source"]["approval_review_id"]):
                problems.append("approved_preview_without_approval")
            if review["current_preview_approval_at_export"] != manifest["snapshot_at_export"]["current_preview_approval"]:
                problems.append("review_snapshot_mismatch")
            privacy = manifest["privacy"]
            if (review["reviewer_labels_included"], review["review_notes_included"]) != \
                    (privacy["reviewer_labels_included"], privacy["review_notes_included"]):
                problems.append("privacy_flags_mismatch")
            for item in review["decisions"]:
                if ("reviewer_label" in item) != privacy["reviewer_labels_included"] or \
                        ("notes" in item) != privacy["review_notes_included"]:
                    problems.append("privacy_flags_mismatch")
    provenance = load("content/provenance.json")
    if provenance is not None and not _valid("content-provenance-summary.schema.json", provenance):
        problems.append("provenance_summary_invalid")
    page = data.get("index.html")
    if page is not None:
        problems.extend(_page_problems(page.decode("utf-8", "replace"), manifest))
    return problems


class _PageScan(HTMLParser):
    """Tags and attributes of the page as a browser would see them (text content is ignored)."""

    ACTIVE = {"script", "iframe", "object", "embed", "link", "form", "base", "frame", "frameset", "applet", "meta-refresh"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.targets, self.handlers, self.styles = [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            value = value or ""
            if name.startswith("on"):
                self.handlers.append(name)
            if name in ("href", "src", "poster", "action", "srcset", "data", "formaction", "xlink:href"):
                self.targets.append(value)
            if name == "style":
                self.styles.append(value)
            if tag == "meta" and name == "http-equiv" and value.lower() == "refresh":
                self.tags.append("meta-refresh")

    def handle_data(self, data):
        if self.tags and self.tags[-1] == "style":
            self.styles.append(data)


def _page_problems(text, manifest):
    problems = []
    scan = _PageScan()
    try:
        scan.feed(text)
        scan.close()
    except Exception:            # noqa: BLE001 - any parser failure means the page is not the generated one
        return ["page_unparseable"]
    if "script" in scan.tags or scan.handlers or any(t.strip().lower().startswith("javascript:") for t in scan.targets):
        problems.append("page_contains_script")
    if (set(scan.tags) - {"script"}) & _PageScan.ACTIVE or any("url(" in s.lower() or "@import" in s.lower()
                                                               for s in scan.styles):
        problems.append("page_contains_external_or_active_element")
    allowed = {row["path"] for row in manifest["files"]} | {MANIFEST}
    if any(target not in allowed for target in scan.targets):
        problems.append("page_reference_not_in_package")
    plain = html.unescape(text)
    if manifest["package_id"] not in plain or manifest["purpose_label"] not in plain or "publishable: false" not in plain:
        problems.append("page_labels_missing")
    return problems
