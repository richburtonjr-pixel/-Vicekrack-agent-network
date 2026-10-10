"""Step 48: Video Studio service. The HQ's only write path, and a thin layer over existing services.

Everything here delegates to the implemented workflow and its safeguards; nothing is re-implemented:

  productions / production   ProductionStore + VideoProduction._plan (verified artifacts) + speech_jobs
  speech_prepare / submit    speech_jobs.prepare / submit (job-specific consent, uncertain = never resent)
  speech_recover             speech_jobs.recover (offline)
  captions_prepare           captions.prepare (offline)
  workflow_start / resume /  VideoProduction.start / resume / submit / retry_scene / export
  submit / retry_scene / export
  review_record              review.ReviewRecorder.record (binding digest, acknowledgments, supersedes)
  export_zip                 export.verify_package, then a zip of exactly the verified files

Every action is explicitly triggered by a person in the browser. There is no scheduler, no polling
loop and no automatic retry; a paid request needs the exact job-specific consent phrase from the
request body. Documents sent to the browser contain IDs, settings, statuses and fixed messages only:
no credentials, environment values, filesystem paths or signed URLs.
"""

import io
import json
import os
import zipfile
from pathlib import Path

from ..errors import NetworkError
from ..orchestrator import ROOT

NARRATION_CHOICES = [
    {"id": "silent", "label": "Silent (no narration)", "available": True,
     "limits": "The generated clips' own sound is always muted, so the preview has no audio."},
    {"id": "speech", "label": "Grok-generated narration (xAI text to speech)", "available": True,
     "limits": "Reads the script's narration beats unchanged in a stock voice. Paid per request; needs XAI_API_KEY on "
               "this computer. Speech longer than 15 seconds is kept but cannot be used (never cut or sped up)."},
    {"id": "local", "label": "Your own WAV recording", "available": False,
     "limits": "Not available in the Studio (it would need a file path). Use the command line: "
               "video-production-start --narration FILE."},
]
CAPTION_CHOICES = [
    {"id": "none", "label": "No captions", "limits": "The video has no burned-in captions."},
    {"id": "provider", "label": "Captions timed by xAI character timestamps",
     "limits": "Needs a speech job prepared WITH timestamps. Phrase captions whose start/end come from the provider's "
               "timings; not word-by-word highlighting. Check them in review."},
    {"id": "estimated", "label": "Captions with ESTIMATED timing",
     "limits": "Spread over the narration by phrase length. NOT synchronized to the voice: a visible label is burned "
               "in, the quality result is needs_review, and approval requires you to acknowledge it after checking "
               "every caption."},
]
ACK_TEXT = {
    "needs_review_result": "The quality result is needs_review (for example estimated caption timing). I checked it.",
    "unavailable_checks": "Some quality checks could not run. I accept that they were not checked.",
    "draft_restrictions": "The script has unverified claims (draft). I accept the draft restrictions.",
    "stale_evidence": "The source evidence is older than the verification policy allows. I accept that.",
}
REASONS = {
    "production_incomplete": "The production did not finish every stage, so it has no complete script and scene plan.",
    "production_not_found": "The production is not saved on this computer.",
    "invalid_production_state": "The production's saved state failed validation.",
    "artifact_changed": "A saved production file changed after it was written, so it is not trusted.",
    "scene_plan_invalid": "The scene plan does not pass validation.",
    "scene_duration_out_of_range": "A scene is shorter than 1 second or longer than 15 seconds, which Grok video cannot generate.",
    "speech_text_has_markup": "The narration contains [ ] or < > characters, so Grok narration and captions are unavailable "
                              "(silent videos still work).",
    "caption_unsupported_character": "The narration has characters the caption font cannot draw, so captions are unavailable.",
}


def _env_key():
    return os.environ.get("XAI_API_KEY", "").strip()


def credentials_configured():
    return bool(_env_key())


class Studio:
    """One studio per HQ server. `demo` uses mock providers and an isolated root (see studio_demo)."""

    def __init__(self, root=None, *, demo=False, clock=None, video_transport=None, speech_transport=None,
                 renderer=None, prober=None, poster_reader=None, notice=None):
        self.root, self.demo = root, demo
        self.clock = clock
        self.video_transport, self.speech_transport = video_transport, speech_transport
        self.renderer, self.prober, self.poster_reader = renderer, prober, poster_reader
        self.notice = notice

    # ------------------------------------------------------------------ shared helpers
    def flow(self):
        from ..video_production import VideoProduction
        return VideoProduction(root=self.root, clock=self.clock, transport=self.video_transport, renderer=self.renderer,
                               prober=self.prober, poster_reader=self.poster_reader)

    def _base(self):
        return Path(self.root if self.root is not None else ROOT)

    def _production(self, production_id):
        from ..production import ProductionStore
        return ProductionStore(self.root).read(production_id)

    def _script_and_plan(self, production_id):
        flow = self.flow()
        plan = flow._plan(production_id)                     # completed + every artifact re-verified
        from ..speech_jobs import production_script
        return production_script(production_id, self.root), plan

    def eligibility(self, production_id):
        reasons, script, plan = [], None, None
        try:
            script, plan = self._script_and_plan(production_id)
        except NetworkError as error:
            code = {"production_incomplete": "production_incomplete", "production_not_found": "production_not_found",
                    "invalid_production_state": "invalid_production_state"}.get(error.code)
            if code is None:
                code = "scene_plan_invalid" if "plan" in error.code else "artifact_changed"
            reasons.append(code)
            return False, reasons, None, None, {"speech": False, "captions": False}
        for scene in plan["scenes"]:
            duration = int(scene["beat"]["end_seconds"] - scene["beat"]["start_seconds"])
            if not 1 <= duration <= 15:
                reasons.append("scene_duration_out_of_range")
                break
        features = {"speech": True, "captions": True}
        from ..speech_jobs import narration_text
        try:
            narration_text(script)
        except NetworkError:
            features.update(speech=False, captions=False)
            reasons.append("speech_text_has_markup")
        if features["captions"]:
            from ..captions import check_characters
            try:
                check_characters(" ".join(b["narration"] for b in script["beats"]))
            except NetworkError:
                features["captions"] = False
                reasons.append("caption_unsupported_character")
        eligible = "scene_duration_out_of_range" not in reasons
        return eligible, reasons, script, plan, features

    # ------------------------------------------------------------------ read documents
    def session_info(self):
        from ..video_jobs import ALLOWED_MODELS
        from ..video_production import load_config
        from ..speech_jobs import load_config as speech_config
        video = load_config()[0]["generation"]
        speech = speech_config()
        return {"contract": "hq_studio_session", "demo": self.demo, "actions_enabled": True,
                "notice": self.notice or ("REAL MODE: paid xAI requests are made only when you approve a specific "
                                          "request. Everything stays publishable: false."),
                "credentials": {"xai_configured": True if self.demo else credentials_configured(),
                                "source": "mock providers (demo)" if self.demo else "XAI_API_KEY environment variable"},
                "options": {"narration": NARRATION_CHOICES, "captions": CAPTION_CHOICES,
                            "models": [m for m in video["models"] if m in ALLOWED_MODELS],
                            "resolutions": video["resolutions"], "voices": speech["voices"],
                            "default_voice": speech["defaults"]["voice"]},
                "acknowledgments": ACK_TEXT}

    def productions(self):
        from ..production import ProductionStore
        rows = []
        workflows = self._workflows_by_production()
        for production_id in ProductionStore(self.root).list_ids()[:100]:
            try:
                state = self._production(production_id)
                title = None
                eligible, reasons, script, _, features = self.eligibility(production_id)
                title = script["title"] if script else None
                rows.append({"production_id": production_id, "status": state["status"], "title": title,
                             "updated_at": state.get("updated_at"), "eligible": eligible, "features": features,
                             "reasons": [{"code": r, "text": REASONS[r]} for r in reasons],
                             "workflows": len(workflows.get(production_id, []))})
            except NetworkError as error:
                rows.append({"production_id": production_id, "status": "unreadable", "title": None, "eligible": False,
                             "features": {"speech": False, "captions": False},
                             "reasons": [{"code": "invalid_production_state",
                                          "text": REASONS["invalid_production_state"]}], "workflows": 0,
                             "error": error.code})
        return {"contract": "hq_studio_productions", "demo": self.demo, "productions": rows}

    def _workflows_by_production(self):
        out = {}
        for row in self.flow().list():
            if row.get("production_id"):
                out.setdefault(row["production_id"], []).append(row)
        return out

    def production(self, production_id):
        from .. import captions, speech_jobs
        eligible, reasons, script, plan, features = self.eligibility(production_id)
        doc = {"contract": "hq_studio_production", "demo": self.demo, "production_id": production_id,
               "eligible": eligible, "features": features,
               "reasons": [{"code": r, "text": REASONS[r]} for r in reasons], "script": None, "scene_plan": None,
               "speech_jobs": [], "caption_tracks": [], "workflows": []}
        if script is None:
            return doc
        from ..story_brief import count_unverified
        doc["script"] = {"title": script["title"], "language": script["language"],
                         "unverified_claims": count_unverified(script), "disclosures": script.get("disclosures", []),
                         "beats": [{"beat": b["beat"], "start": b["start_seconds"], "end": b["end_seconds"],
                                    "narration": b["narration"], "on_screen_text": b["on_screen_text"],
                                    "visual": b["visual"]["description"]} for b in script["beats"]]}
        doc["scene_plan"] = {"plan_id": plan["plan_id"], "draft": plan["blocked_for_production"],
                             "scenes": [{"index": s["index"], "beat": s["beat"]["beat"], "start": s["beat"]["start_seconds"],
                                         "end": s["beat"]["end_seconds"], "method": s["selected_method"]}
                                        for s in plan["scenes"]]}
        digest = speech_jobs.script_digest(script)
        jobs = []
        for row in speech_jobs.list_jobs(self.root):
            try:
                record = speech_jobs.inspect(row["job_id"], self.root)
            except NetworkError:
                continue
            if record["script"]["script_sha256"] == digest:
                jobs.append(self._speech_view(record))
        doc["speech_jobs"] = jobs
        ids = {j["job_id"] for j in jobs}
        doc["caption_tracks"] = [row for row in captions.list_tracks(self.root) if row.get("speech_job_id") in ids]
        doc["workflows"] = self._workflows_by_production().get(production_id, [])
        return doc

    @staticmethod
    def _speech_view(record):
        from ..speech_jobs import view
        shown = view(record)
        keep = ("job_id", "status", "error_code", "text", "settings", "consent_phrase", "attempts", "audio", "notice",
                "publishable", "next")
        out = {k: shown.get(k) for k in keep}
        out["with_timestamps"] = bool(record["request"].get("with_timestamps"))
        out["paid"] = True
        out["actions"] = []
        status = record["status"]
        if status in ("prepared", "rejected"):
            out["actions"].append({"action": "speech_submit", "paid": True, "consent": record["consent_phrase"]})
        elif status in ("uncertain", "invalid_audio"):
            out["actions"].append({"action": "speech_submit", "paid": True, "consent": record["consent_phrase"],
                                   "retry_uncertain": True, "duplicate_billing_risk": True})
        if status in ("submitting", "received"):
            out["actions"].append({"action": "speech_recover", "paid": False})
        return out

    def workflow(self, workflow_id):
        from .. import video_jobs as jobs
        from ..production import ProductionStore
        from .content import Artifacts
        flow = self.flow()
        view = flow.inspect(workflow_id)
        state = flow.store.read(workflow_id)
        scenes = []
        with jobs.use_root(self.root):
            for scene in view["scenes"]:
                row = dict(scene)
                if scene["job_id"]:
                    record = jobs.inspect(scene["job_id"])
                    request = record["request"]
                    row["request"] = {k: request[k] for k in ("model", "prompt", "duration", "aspect_ratio",
                                                              "resolution", "generate_audio")}
                    row["consent"] = f"paid-generate:{scene['job_id']}"
                scenes.append(row)
        doc = {"contract": "hq_studio_workflow", "demo": self.demo, "workflow_id": workflow_id,
               "status": view["status"], "production_id": view["production_id"], "generation": view["generation"],
               "stages": view["stages"], "scenes": scenes, "narration": view["narration"], "captions": view["captions"],
               "outputs": view["outputs"], "integrity_problems": view["integrity_problems"], "notice": view["notice"],
               "publishable": False, "quality": None, "review": None, "video": None, "export": None, "speech": None}
        speech = (state.get("narration") or {}).get("speech")
        if speech:
            from .. import speech_jobs
            try:
                doc["speech"] = self._speech_view(speech_jobs.inspect(speech["job_id"], self.root))
            except NetworkError as error:
                doc["speech"] = {"job_id": speech["job_id"], "error": error.code}
        report_id = view["outputs"]["quality_report_id"]
        production_id = view["production_id"]
        if report_id:
            from ..quality import load_report
            from ..review import history, reviewable
            report = load_report(report_id, self.root)
            doc["quality"] = {"report_id": report_id, "result": report["result"], "reasons": report["reasons"],
                              "checked_at": report["checked_at"],
                              "checks": [{"check_id": c["check_id"], "status": c["status"], "reasons": c["reasons"]}
                                         for c in report["checks"]]}
            row = next((r for r in reviewable(production_id, self.root) if r["report_id"] == report_id), None)
            hist = history(production_id, self.root)
            doc["review"] = {
                "binding": row["binding"] if row else "unavailable",
                "binding_digest": row["binding_digest"] if row else None,
                "applicable_acknowledgments": [{"id": a, "text": ACK_TEXT[a]} for a in
                                               (row["applicable_acknowledgments"] if row else [])],
                "decisions_allowed": row["decisions_allowed"] if row else [],
                "approval_blockers": row["approval_blockers"] if row else [],
                "history_status": hist["status"],
                "latest_review_id": hist["reviews"][0]["review_id"] if hist["reviews"] else None,
                "decisions": [{"review_id": r["review_id"], "decision": r["decision"], "recorded_at": r["recorded_at"],
                               "reviewer": r["reviewer_label"], "report_id": r["report_id"],
                               "applies_to_this_video": r["report_id"] == report_id,
                               "applicability": r["applicability"], "current_preview_approval": r["current_preview_approval"],
                               "acknowledgments": r["acknowledgments"]} for r in hist["reviews"][:20]]}
        if production_id:
            try:
                store = ProductionStore(self.root)
                production = store.read(production_id)
                artifacts = next(s for s in production["stages"] if s["name"] == "preview")["artifacts"]
                current = artifacts.get("video_sha256") == view["outputs"]["preview_video_sha256"] and \
                    view["outputs"]["preview_video_sha256"] is not None
                checked = Artifacts(production, store.folder(production_id)) if current else None   # the desk's own checks
                video = checked.items.get("video", {}) if checked else {}
                if current and video.get("status") == "verified":
                    poster = next((p["media_id"] for p in checked.posters if p.get("media_id")), None)
                    doc["video"] = {"media_id": video["media_id"], "poster_media_id": poster,
                                    "sha256": artifacts["video_sha256"], "audio": artifacts.get("audio_present"),
                                    "captions": "captions_path" in artifacts}
                elif current:
                    doc["video"] = {"media_id": None, "unverified": video.get("reason") or "video_not_verified"}
                elif view["outputs"]["preview_video_sha256"]:
                    doc["video"] = {"media_id": None, "superseded": True}
            except (NetworkError, StopIteration):
                pass
        package_id = view["outputs"]["export_package_id"]
        if package_id:
            doc["export"] = self._package_summary(package_id, view["outputs"]["export_purpose"])
        doc["actions"] = self._actions(doc, view)
        return doc

    def _package_summary(self, package_id, purpose):
        from ..export import verify_package
        path = self._base() / "runtime" / "exports" / package_id
        result = verify_package(path)
        return {"package_id": package_id, "purpose": purpose, "verified": result["status"] == "consistent",
                "problems": result["problems"], "files_checked": result["files_checked"], "publishable": False}

    @staticmethod
    def _actions(doc, view):
        """What a person may do next, from the workflow's own status (never more than the backend allows)."""
        status, actions = view["status"], []
        failed = next((s for s in view["stages"] if s["status"] == "failed"), None)
        if status == "waiting_for_consent":
            for scene in doc["scenes"]:
                if scene["job_status"] == "prepared":
                    actions.append({"action": "workflow_submit", "scene": scene["scene_index"], "paid": True,
                                    "consent": scene["consent"]})
        elif status == "uncertain_submission":
            for scene in doc["scenes"]:
                if scene["job_status"] in ("uncertain", "submitting"):
                    actions.append({"action": "workflow_submit", "scene": scene["scene_index"], "paid": True,
                                    "consent": scene["consent"], "retry_uncertain": True, "duplicate_billing_risk": True})
            for scene in doc["scenes"]:
                if scene["job_status"] == "prepared":
                    actions.append({"action": "workflow_submit", "scene": scene["scene_index"], "paid": True,
                                    "consent": scene["consent"]})
        elif status == "waiting_for_provider":
            actions.append({"action": "workflow_resume", "allow_network": True, "paid": False,
                            "label": "Check the provider and download finished clips (no new generation)"})
        elif status == "failed" and failed and failed["error_code"] == "provider_job_failed":
            for scene in doc["scenes"]:
                if scene["job_status"] in ("failed", "expired"):
                    actions.append({"action": "workflow_retry_scene", "scene": scene["scene_index"], "paid": False})
        elif status == "failed":
            actions.append({"action": "workflow_resume", "allow_network": False, "paid": False,
                            "label": "Try the failed stage again (no paid request)"})
        elif status in ("waiting_for_review", "review_rejected", "ready_to_export"):
            if doc["review"] and doc["review"]["decisions_allowed"]:
                actions.append({"action": "review_record", "paid": False})
            actions.append({"action": "workflow_resume", "allow_network": False, "paid": False,
                            "label": "Re-check the review status"})
            actions.append({"action": "workflow_export", "purpose": "review_copy", "paid": False})
            if status == "ready_to_export":
                actions.append({"action": "workflow_export", "purpose": "approved_preview", "paid": False})
        if doc.get("export") and doc["export"]["verified"]:
            actions.append({"action": "export_download", "package_id": doc["export"]["package_id"], "paid": False})
        return actions

    # ------------------------------------------------------------------ actions (each explicitly user-triggered)
    def speech_prepare(self, production_id, voice=None, with_timestamps=False):
        from .. import speech_jobs
        eligible, reasons, script, _, features = self.eligibility(production_id)
        if script is None or not features["speech"]:
            raise NetworkError("speech_unavailable", "Grok narration is not available for this production.")
        record = speech_jobs.prepare(script, source="production", production_id=production_id, voice=voice,
                                     with_timestamps=with_timestamps is True, root=self.root, clock=self.clock)
        return self._speech_view(record)

    def speech_submit(self, job_id, consent, retry_uncertain=False, acknowledge_duplicate_billing=False):
        from .. import speech_jobs
        try:
            record = speech_jobs.submit(job_id, consent=consent, allow_network=True, retry_uncertain=retry_uncertain,
                                        acknowledge_duplicate_billing=acknowledge_duplicate_billing,
                                        transport=self.speech_transport, root=self.root, clock=self.clock)
        except NetworkError as error:
            if error.code == "speech_submit_uncertain":
                raise NetworkError(error.code, "The provider did not answer clearly. The request may have been billed "
                                   "and was NOT retried. Check your xAI usage before deciding to retry.") from None
            raise
        return self._speech_view(record)

    def speech_recover(self, job_id):
        from .. import speech_jobs
        return self._speech_view(speech_jobs.recover(job_id, root=self.root, clock=self.clock))

    def captions_prepare(self, speech_job_id, timing):
        from .. import captions
        return captions.view(captions.prepare(speech_job_id, timing=timing, root=self.root))

    def workflow_start(self, production_id, narration="silent", speech_job_id=None, caption_id=None, model=None,
                       resolution="720p"):
        from ..video_jobs import DEFAULT_MODEL
        eligible, reasons, _, _, _ = self.eligibility(production_id)
        if not eligible:
            raise NetworkError("production_not_eligible", "This production cannot be used: " +
                               "; ".join(REASONS[r] for r in reasons if r in REASONS))
        if narration not in ("silent", "speech"):
            raise NetworkError("invalid_narration_choice", "Choose silent or Grok-generated narration.")
        if narration == "silent" and (speech_job_id or caption_id):
            raise NetworkError("invalid_narration_choice", "Silent videos have no narration job or captions.")
        if narration == "speech" and not speech_job_id:
            raise NetworkError("speech_job_required", "Choose a completed speech job first.")
        view = self.flow().start(production_id=production_id, model=model or DEFAULT_MODEL, resolution=resolution,
                                 speech_job=speech_job_id if narration == "speech" else None,
                                 captions=caption_id if narration == "speech" else None)
        return self.workflow(view["workflow_id"])

    def workflow_resume(self, workflow_id, allow_network=False):
        self.flow().resume(workflow_id, allow_network=allow_network is True)
        return self.workflow(workflow_id)

    def workflow_submit(self, workflow_id, scene, consent, retry_uncertain=False, acknowledge_duplicate_billing=False):
        self.flow().submit(workflow_id, scene, consent=consent, allow_network=True, retry_uncertain=retry_uncertain,
                           acknowledge_duplicate_billing=acknowledge_duplicate_billing)
        return self.workflow(workflow_id)

    def workflow_retry_scene(self, workflow_id, scene, model=None, resolution=None):
        self.flow().retry_scene(workflow_id, scene, model=model, resolution=resolution)
        return self.workflow(workflow_id)

    def workflow_export(self, workflow_id, purpose):
        self.flow().export(workflow_id, purpose=purpose)
        return self.workflow(workflow_id)

    def review_record(self, workflow_id, decision, reviewer, binding, acknowledgments=(), notes=None, supersedes=None):
        from ..review import ReviewRecorder
        view = self.flow().inspect(workflow_id)
        if view["integrity_problems"]:
            raise NetworkError(view["integrity_problems"][0], "The workflow's files failed their integrity check.")
        report_id = view["outputs"]["quality_report_id"]
        if not report_id:
            raise NetworkError("quality_required", "There is no quality report to review yet.")
        ReviewRecorder(self.root, clock=self.clock).record(
            view["production_id"], report_id, decision=decision, reviewer=reviewer, binding=binding,
            acknowledgments=list(acknowledgments or []), notes=notes or None, supersedes=supersedes)
        self.flow().resume(workflow_id)                       # re-evaluate (records nothing itself)
        return self.workflow(workflow_id)

    def export_zip(self, workflow_id, package_id):
        """Bytes of a zip of exactly the files of a verified package that this workflow exported."""
        from ..export import verify_package
        view = self.flow().inspect(workflow_id)
        if view["outputs"]["export_package_id"] != package_id:
            raise NetworkError("export_not_found", "This workflow has no such export package.")
        path = self._base() / "runtime" / "exports" / package_id
        result = verify_package(path)
        if result["status"] != "consistent":
            raise NetworkError("export_not_verified", "The export package failed verification and is not offered.")
        manifest = json.loads((path / "package.json").read_text(encoding="utf-8"))
        names = ["package.json"] + [row["path"] for row in manifest["files"]]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in names:
                data = (path / name).read_bytes()
                if len(buffer.getbuffer()) + len(data) > 96 * 1024 * 1024:
                    raise NetworkError("export_too_large", "The package is too large to download here.")
                archive.writestr(f"{package_id}/{name}", data)
        again = verify_package(path)                          # nothing changed while it was zipped
        if again["status"] != "consistent":
            raise NetworkError("export_not_verified", "The export package changed while it was being prepared.")
        return buffer.getvalue()
