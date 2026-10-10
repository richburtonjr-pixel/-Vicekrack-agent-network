"""Step 44: the controlled video-production workflow.

One finite, resumable workflow per production and generation setting:

  production       an approved story (selection run + record) goes through the existing Step 21
                   pipeline, or an already completed production is linked
  scene_plan       the production's saved, hash-checked scene plan
  jobs             four Step 43 Grok jobs are PREPARED (no request, no cost)
  generation       each job is submitted only by `video-production-submit` with that job's own
                   consent phrase; resume only checks status (bounded, explicit --allow-network)
  downloads        controlled Step 43 downloads into the workflow's media folder
  media_manifest   the four downloads become a Step 42 media manifest
  preview_revision the existing production gets a revised local preview (Step 43 revision;
                   older reports and approvals stop matching). Step 45: with optional local
                   narration, the narration is mixed into this preview; the clips stay muted
  quality          a fresh Step 36 bound quality report (`fail` stops the workflow)
  review           waits for an explicit Step 37 human decision; the workflow never records one
  export           only `video-production-export` exports, through the unchanged Step 38 gates

Nothing here spends money on its own: start and resume never submit, an uncertain submission
pauses the workflow until a person explicitly retries with `--retry-uncertain
--acknowledge-duplicate-billing`, and resume makes at most a fixed number of status and
download requests, only with --allow-network. There is no background worker or polling loop.

State: runtime/video-production/<workflow_id>/workflow.json (`video_production_workflow` 1.0),
schema-validated, self-hashed and replaced atomically under an OS lock. Errors are stored as
fixed codes only. Provider keys, headers, raw exceptions and signed download URLs are never
written here (signed URLs stay inside the Step 43 job records and are never printed).

Step 45, optional narration: `start(narration=PATH)` validates a local 16-bit PCM WAV with the
Step 14 rules, refuses an all-silent file, copies the exact bytes to
`<workflow>/narration/narration.wav` and records their SHA-256 (never the original path).
Duration policy: the preview is always 15 seconds; shorter narration is padded with silence,
longer narration is refused at start and is never cut off. Every resume, submit, retry and
export re-hashes the managed copy (`narration_tampered`), and the narration is part of the
revision identity, so a narrated preview is a different video that needs its own quality
report and its own human approval. Without narration the workflow is exactly Step 44.
"""

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from . import video_jobs as jobs
from .errors import NetworkError
from .events.store import _Lock
from .orchestrator import ROOT
from .persistence import reject_secrets

CONTRACT, VERSION = "video_production_workflow", "1.0"
STAGES = ("production", "scene_plan", "jobs", "generation", "downloads", "media_manifest", "preview_revision",
          "quality", "review", "export")
SCENES = (1, 2, 3, 4)
CONFIG_PATH = "config/video-production.json"
BLOCKING = {"workflow_config_changed", "production_changed", "media_tampered", "media_manifest_tampered",
            "scene_plan_tampered", "production_preview_changed", "narration_tampered"}
NARRATION_FILE = "narration/narration.wav"
DURATION_POLICY = "pad_shorter_with_silence_reject_longer"
TAMPERED = {"media-manifest.json": "media_manifest_tampered", NARRATION_FILE: "narration_tampered"}
NOTICE = ("Draft, illustrative preview workflow. Generated footage is not evidence and its rights are not verified. "
          "publishable stays false; nothing is uploaded or published. Paid generation happens only through "
          "video-production-submit with a job-specific consent phrase.")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@lru_cache(maxsize=None)
def _validator():
    schema = json.loads((ROOT / "schemas/video-production-workflow.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def load_config(root=ROOT):
    path = Path(root) / CONFIG_PATH
    try:
        data = path.read_bytes()
        config = json.loads(data.decode("utf-8"))
        limits, generation = config["limits"], config["generation"]
        if (config.get("config_version") != "1.0" or not set(generation["models"]) <= set(jobs.ALLOWED_MODELS)
                or not all(isinstance(limits[k], int) and 1 <= limits[k] <= 500 for k in
                           ("max_steps", "max_stage_attempts", "max_status_checks_per_resume",
                            "max_downloads_per_resume", "max_scene_replacements", "max_trace"))):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise NetworkError("invalid_workflow_config", "config/video-production.json is invalid.") from None
    return config, sha256_bytes(data)


def validate_state(state):
    try:
        reject_secrets(state)
    except NetworkError:
        raise NetworkError("workflow_corrupt", "The workflow state contains credential-like data.") from None
    if not isinstance(state, dict) or next(_validator().iter_errors(state), None) is not None:
        raise NetworkError("workflow_corrupt", "The workflow state does not match its contract.")
    body = {k: v for k, v in state.items() if k != "state_sha256"}
    if sha256_bytes(canonical(body)) != state["state_sha256"]:
        raise NetworkError("workflow_corrupt", "The workflow state failed its integrity check.")
    if [s["name"] for s in state["stages"]] != list(STAGES) or [s["scene_index"] for s in state["scenes"]] != list(SCENES):
        raise NetworkError("workflow_corrupt", "The workflow state does not list the fixed stages and scenes.")
    if "download_url" in json.dumps(state) or "://vidgen" in json.dumps(state):
        raise NetworkError("workflow_corrupt", "The workflow state must never hold signed download URLs.")
    return state


def workflow_id_for(origin, generation, narration_sha256=None):
    key = {"production_id": origin["production_id"], "selection_run_id": origin["selection_run_id"],
           "record_id": origin["record_id"], "generation": generation}
    if narration_sha256 is not None:            # Step 45; silent workflows keep their Step 44 IDs
        key["narration_sha256"] = narration_sha256
    return "vpw-" + sha256_bytes(canonical(key))[:24]


def read_narration(path):
    """Step 45: read and validate a local narration ONCE; returns (bytes, safe metadata).
    The same bytes are validated, hashed and stored, so a file edited mid-start cannot slip through."""
    from .narration import has_sound, normalize_narration, read_bounded
    if path is None or str(path).strip() == "":
        raise NetworkError("narration_not_found", "Narration file was not found.")
    data = read_bounded(path)
    metadata, _ = normalize_narration(data)              # Step 14 rules: format, size, <= 15 s (never truncated)
    if not has_sound(data):
        raise NetworkError("narration_silent", "Narration contains only silence; supply a recording or omit --narration.")
    return data, {"managed_file": NARRATION_FILE, "source_sha256": sha256_bytes(data), "source_bytes": len(data),
                  "normalized_sha256": metadata["normalized_sha256"],
                  "source_duration_seconds": metadata["source_duration_seconds"],
                  "padded_duration_seconds": metadata["padded_duration_seconds"], "channels": metadata["channels"],
                  "sample_rate": metadata["sample_rate"], "duration_policy": DURATION_POLICY}


class WorkflowStore:
    def __init__(self, root=None):
        self.base = Path(root if root is not None else ROOT) / "runtime" / "video-production"

    def folder(self, workflow_id):
        if not isinstance(workflow_id, str) or len(workflow_id) != 28 or not workflow_id.startswith("vpw-") \
                or any(c not in "0123456789abcdef" for c in workflow_id[4:]):
            raise NetworkError("invalid_workflow_id", "Workflow IDs look like vpw- followed by 24 hex characters.")
        return self.base / workflow_id

    @contextmanager
    def lock(self, workflow_id):
        folder = self.folder(workflow_id)
        folder.mkdir(parents=True, exist_ok=True)
        lock = _Lock(folder / "workflow.lock")
        if not lock.acquire(create=True):
            raise NetworkError("workflow_busy", "Another process is running this workflow; nothing was done.")
        try:
            yield
        finally:
            lock.release()

    def exists(self, workflow_id):
        return (self.folder(workflow_id) / "workflow.json").is_file()

    def read(self, workflow_id):
        path = self.folder(workflow_id) / "workflow.json"
        if not path.is_file():
            raise NetworkError("workflow_not_found", "No video-production workflow with this ID.")
        try:
            if path.is_symlink() or path.stat().st_size > 512 * 1024:
                raise ValueError
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise NetworkError("workflow_corrupt", "The workflow state is unreadable.") from None
        validate_state(state)
        if state["workflow_id"] != workflow_id:
            raise NetworkError("workflow_corrupt", "The workflow state belongs to another workflow.")
        return state

    def write(self, state, create=False):
        body = {k: v for k, v in state.items() if k != "state_sha256"}
        state["state_sha256"] = sha256_bytes(canonical(body))
        validate_state(state)
        folder = self.folder(state["workflow_id"])
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "workflow.json"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, indent=1, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if create:
                os.link(temporary, target)
            else:
                os.replace(temporary, target)
                temporary = None
        except FileExistsError:
            raise NetworkError("workflow_exists", "This workflow already exists; inspect or resume it.") from None
        except OSError:
            raise NetworkError("workflow_storage_failed", "Could not save the workflow state atomically.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return state

    def publish_once(self, workflow_id, name, data):
        """Write a workflow file exactly once; an identical existing file is accepted (crash recovery)."""
        target = self.folder(workflow_id) / name
        if target.exists():
            if target.is_symlink() or target.read_bytes() != data:
                raise NetworkError(TAMPERED.get(name, "scene_plan_tampered"),
                                   "A saved workflow file differs from what this workflow produced.")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        except OSError:
            raise NetworkError("workflow_storage_failed", "Could not save a workflow file.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target

    def ids(self):
        if not self.base.is_dir():
            return []
        return sorted(p.name for p in self.base.iterdir() if p.is_dir() and p.name.startswith("vpw-"))


class Wait(Exception):
    """A stage cannot finish yet; the workflow pauses in `status` until an explicit action."""

    def __init__(self, status, code=None):
        super().__init__(status)
        self.status, self.code = status, code


class VideoProduction:
    """Start, resume, submit, retry, export and inspect workflows. Injection points are for tests and the demo."""

    def __init__(self, root=None, clock=None, transport=None, renderer=None, prober=None, poster_reader=None,
                 drafter=None, config_root=None):
        self.root = root
        self.clock = clock or utc_now
        self.transport = transport                    # None: Step 43's bounded HTTPS transport
        self.renderer = renderer                      # None: the real local renderer
        self.prober, self.poster_reader, self.drafter = prober, poster_reader, drafter
        self.store = WorkflowStore(root)
        self.config, self.config_sha = load_config(config_root or ROOT)

    # ------------------------------------------------------------------ helpers
    @property
    def limits(self):
        return self.config["limits"]

    def _jobs(self):
        return jobs.use_root(self.root)

    def _kwargs(self):
        return {"transport": self.transport} if self.transport is not None else {}

    def media_root(self, workflow_id):
        return self.store.folder(workflow_id) / "media"

    def _trace(self, state, stage, event, code=None):
        state["trace"] = (state["trace"] + [{"at": self.clock(), "stage": stage, "event": event, "code": code}]
                          )[-self.limits["max_trace"]:]

    def _save(self, state):
        state["updated_at"] = self.clock()
        return self.store.write(state)

    def _stage(self, state, name):
        return next(s for s in state["stages"] if s["name"] == name)

    def _scene(self, state, index):
        if index not in SCENES:
            raise NetworkError("invalid_scene", "Scenes are numbered 1 to 4.")
        return state["scenes"][index - 1]

    def _production_pipeline(self):
        from .production import Pipeline
        kwargs = {}
        if self.renderer is not None:
            kwargs["renderer"] = self.renderer
        if self.drafter is not None:
            kwargs["drafter"] = self.drafter
        return Pipeline(root=self.root, clock=self.clock, **kwargs)

    # ------------------------------------------------------------------ start
    def start(self, *, production_id=None, selection_run_id=None, record_id=None, model=jobs.DEFAULT_MODEL,
              resolution="720p", audio_mode="none", allow_draft_preview=False, narration=None):
        if (production_id is None) == (selection_run_id is None or record_id is None):
            raise NetworkError("invalid_workflow_origin",
                               "Start from --production PRODUCTION_ID, or from --selection RUN_ID --record RECORD_ID.")
        generation = self._generation(model, resolution, audio_mode)
        # Step 45: invalid, missing, silent or overlong narration is refused before anything is created.
        narration_data, narration_meta = read_narration(narration) if narration is not None else (None, None)
        origin = {"kind": "production" if production_id else "selection", "production_id": production_id,
                  "selection_run_id": selection_run_id, "record_id": record_id,
                  "allow_draft_preview": bool(allow_draft_preview)}
        workflow_id = workflow_id_for(origin, generation, narration_meta and narration_meta["source_sha256"])
        if self.store.exists(workflow_id):
            raise NetworkError("workflow_exists", "This workflow already exists; inspect or resume it.")
        now = self.clock()
        state = {"contract": CONTRACT, "version": VERSION, "workflow_id": workflow_id, "status": "running",
                 "created_at": now, "updated_at": now, "origin": origin, "generation": generation,
                 "config_sha256": self.config_sha, "notice": NOTICE, "publishable": False, "steps": 0,
                 "inputs": {"production_id": production_id, "plan_id": None, "plan_sha256": None},
                 "scenes": [{"scene_index": i, "job_id": None, "replaced_job_ids": [], "job_status": None,
                             "request_id": None, "media_sha256": None, "media_bytes": None} for i in SCENES],
                 "outputs": {"media_manifest_sha256": None, "media_digest": None, "preview_video_sha256": None,
                             "preview_manifest_sha256": None, "quality_report_id": None, "quality_result": None,
                             "review_id": None, "export_package_id": None, "export_purpose": None},
                 "stages": [{"name": name, "status": "pending", "attempts": 0, "started_at": None, "finished_at": None,
                             "error_code": None} for name in STAGES],
                 "trace": [], "state_sha256": "0" * 64}
        if narration_meta is not None:
            state["narration"] = narration_meta
        with self.store.lock(workflow_id):
            if self.store.exists(workflow_id):
                raise NetworkError("workflow_exists", "This workflow already exists; inspect or resume it.")
            if narration_data is not None:          # the managed copy exists before the state that names it
                self.store.publish_once(workflow_id, NARRATION_FILE, narration_data)
            self.store.write(state, create=True)
            return self._advance(state, allow_network=False)

    def _generation(self, model, resolution, audio_mode):
        allowed = self.config["generation"]
        if model not in allowed["models"] or resolution not in allowed["resolutions"] or audio_mode not in allowed["audio_modes"]:
            raise NetworkError("invalid_generation_settings", "Model, resolution or audio mode is not allowed by the config.")
        return {"model": model, "resolution": resolution, "audio_mode": audio_mode}

    # ------------------------------------------------------------------ resume / inspect
    def resume(self, workflow_id, *, allow_network=False):
        with self.store.lock(workflow_id):
            state = self.store.read(workflow_id)
            self._verify(state)
            self._recheck_review(state)
            return self._advance(state, allow_network=allow_network)

    def _recheck_review(self, state):
        """A completed review stage is re-read on every resume or export until the export is done: a later
        human decision (superseding, rejecting) or a changed binding must never leave a stale approval."""
        review = self._stage(state, "review")
        if review["status"] == "completed" and self._stage(state, "export")["status"] != "completed":
            review.update(status="pending", error_code=None)

    def inspect(self, workflow_id):
        state = self.store.read(workflow_id)
        problems = []
        try:
            self._verify(state)
        except NetworkError as error:
            problems.append(error.code)
        return self.view(state, problems)

    def list(self):
        rows = []
        for workflow_id in self.store.ids()[:200]:
            try:
                state = self.store.read(workflow_id)
                rows.append({"workflow_id": workflow_id, "status": state["status"],
                             "production_id": state["inputs"]["production_id"], "updated_at": state["updated_at"]})
            except NetworkError as error:
                rows.append({"workflow_id": workflow_id, "error": error.code})
        return rows

    # ------------------------------------------------------------------ integrity of completed work
    def _verify(self, state):
        """Every completed output must still be what the workflow recorded; nothing is repaired silently."""
        if state["config_sha256"] != self.config_sha:
            raise NetworkError("workflow_config_changed",
                               "config/video-production.json changed since this workflow started; start a new workflow.")
        done = {s["name"] for s in state["stages"] if s["status"] == "completed"}
        folder = self.store.folder(state["workflow_id"])
        self._narration(state)                       # Step 45: the managed copy must be byte-identical
        if "scene_plan" in done:
            plan = self._plan(state["inputs"]["production_id"])
            if sha256_bytes(canonical(plan)) != state["inputs"]["plan_sha256"]:
                raise NetworkError("production_changed", "The production's scene plan changed since the workflow used it.")
            saved = folder / "scene-plan.json"
            if not saved.is_file() or sha256_bytes(saved.read_bytes()) != state["inputs"]["plan_sha256"]:
                raise NetworkError("scene_plan_tampered", "The workflow's saved scene plan changed.")
        media = self.media_root(state["workflow_id"])
        for scene in state["scenes"]:
            if scene["media_sha256"]:
                path = media / f"{scene['job_id']}.mp4"
                if ("downloads" not in done and not path.exists()
                        and self._adoptable(state, scene["job_id"], scene["media_sha256"], scene["media_bytes"])):
                    continue                          # shared clip not copied yet (interrupted); downloads adopts it
                try:
                    data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
                except OSError:
                    data = None
                if data is None or sha256_bytes(data) != scene["media_sha256"]:
                    raise NetworkError("media_tampered", f"Downloaded media for scene {scene['scene_index']} changed or is missing.")
        if "media_manifest" in done:
            path = folder / "media-manifest.json"
            if not path.is_file() or sha256_bytes(path.read_bytes()) != state["outputs"]["media_manifest_sha256"]:
                raise NetworkError("media_manifest_tampered", "The workflow's media manifest changed.")
        if "preview_revision" in done:
            artifacts = self._preview_artifacts(state["inputs"]["production_id"])
            if (artifacts.get("media_sha256") != state["outputs"]["media_digest"]
                    or artifacts.get("narration_sha256") != self._narration_sha(state)
                    or artifacts.get("video_sha256") != state["outputs"]["preview_video_sha256"]):
                raise NetworkError("production_preview_changed",
                                   "The production's current preview is not the one this workflow rendered.")

    @staticmethod
    def _narration_sha(state):
        return (state.get("narration") or {}).get("source_sha256")

    def _narration(self, state):
        """The managed narration bytes ({"data", "sha256"}), or None for a silent workflow. Never repaired."""
        meta = state.get("narration")
        if not meta:
            return None
        path = self.store.folder(state["workflow_id"]) / NARRATION_FILE
        try:
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        except OSError:
            data = None
        if data is None:
            raise NetworkError("narration_tampered", "The workflow's narration copy is missing; start a new workflow.")
        if len(data) != meta["source_bytes"] or sha256_bytes(data) != meta["source_sha256"]:
            raise NetworkError("narration_tampered", "The workflow's narration copy changed; start a new workflow.")
        return {"data": data, "sha256": meta["source_sha256"]}

    def _production_state(self, production_id):
        from .production import ProductionStore
        return ProductionStore(self.root).read(production_id)

    def _preview_artifacts(self, production_id):
        state = self._production_state(production_id)
        return next(s for s in state["stages"] if s["name"] == "preview")["artifacts"]

    def _plan(self, production_id):
        from .production import ProductionStore, _check_saved_config
        from .scene_plan import validate_scene_plan
        pipeline = self._production_pipeline()
        store = ProductionStore(self.root)
        with store.lock(production_id):
            state = store.read(production_id)
            if state["status"] != "completed":
                raise NetworkError("production_incomplete", "The production is not completed.")
            loaded = _check_saved_config(state)
            pipeline._verify_artifacts(state, loaded)
            plan = pipeline._load_json(state, "plan", "plan_path")
        validate_scene_plan(plan)
        return plan

    # ------------------------------------------------------------------ the fixed sequence
    def _advance(self, state, *, allow_network):
        budget = {"status": self.limits["max_status_checks_per_resume"], "download": self.limits["max_downloads_per_resume"]}
        for stage in state["stages"]:
            if stage["status"] == "completed":
                continue
            if state["steps"] >= self.limits["max_steps"]:
                state["status"] = "blocked"
                self._trace(state, stage["name"], "step_limit", "workflow_step_limit")
                self._save(state)
                raise NetworkError("workflow_step_limit", "The workflow reached max_steps; start a new workflow.")
            if stage["status"] == "failed" and stage["attempts"] >= self.limits["max_stage_attempts"]:
                state["status"] = "blocked"
                self._save(state)
                raise NetworkError("workflow_attempt_limit", f"The {stage['name']} stage reached max_stage_attempts.")
            state["steps"] += 1
            if stage["started_at"] is None:
                stage["started_at"] = self.clock()
            try:
                getattr(self, "_stage_" + stage["name"])(state, stage, allow_network, budget)
            except Wait as wait:
                stage.update(status="waiting", error_code=wait.code)
                state["status"] = wait.status
                self._trace(state, stage["name"], "waiting", wait.code)
                return self.view(self._save(state))
            except NetworkError as error:
                stage.update(status="failed", attempts=stage["attempts"] + 1, error_code=error.code,
                             finished_at=self.clock())
                state["status"] = ("quality_failed" if error.code == "quality_failed" else
                                   "blocked" if error.code in BLOCKING else "failed")
                self._trace(state, stage["name"], "failed", error.code)
                return self.view(self._save(state))
            stage.update(status="completed", error_code=None, finished_at=self.clock())
            state["status"] = "running"
            self._trace(state, stage["name"], "completed")
            self._save(state)
        state["status"] = "exported"
        return self.view(self._save(state))

    # -- stage handlers (each either returns = completed, raises Wait, or raises NetworkError)
    def _stage_production(self, state, stage, allow_network, budget):
        from .production import ProductionStore
        pipeline = self._production_pipeline()
        origin, inputs = state["origin"], state["inputs"]
        if inputs["production_id"] is None:
            try:
                result = pipeline.produce(origin["selection_run_id"], origin["record_id"],
                                          allow_draft_preview=origin["allow_draft_preview"])
                inputs["production_id"] = result["production_id"]
            except NetworkError as error:
                if error.code != "production_exists":
                    raise
                inputs["production_id"] = self._find_production(origin)
            self._save(state)                         # the production ID is never lost after a crash
        production = ProductionStore(self.root).read(inputs["production_id"])
        if production["status"] != "completed":
            if origin["kind"] == "production":
                raise NetworkError("production_incomplete", "Link a completed production, or start from a selection.")
            result = pipeline.resume(inputs["production_id"], allow_paid=False)    # never authorizes paid Creator calls
            if result["status"] != "completed":
                code = (result.get("error") or {}).get("code") or "production_not_completed"
                raise NetworkError(code if isinstance(code, str) else "production_not_completed",
                                   "The production did not complete; resume the workflow to try again.")

    def _find_production(self, origin):
        from .production import ProductionStore
        store = ProductionStore(self.root)
        for production_id in store.list_ids():
            try:
                config = store.read(production_id)["config"]
            except NetworkError:
                continue
            if (config["selection_run_id"], config["record_id"]) == (origin["selection_run_id"], origin["record_id"]):
                return production_id
        raise NetworkError("production_exists", "This story already has a production that could not be matched.")

    def _stage_scene_plan(self, state, stage, allow_network, budget):
        plan = self._plan(state["inputs"]["production_id"])
        data = canonical(plan)
        self.store.publish_once(state["workflow_id"], "scene-plan.json", data)
        state["inputs"].update(plan_id=plan["plan_id"], plan_sha256=sha256_bytes(data))

    def _saved_plan(self, state):
        path = self.store.folder(state["workflow_id"]) / "scene-plan.json"
        data = path.read_bytes()
        if sha256_bytes(data) != state["inputs"]["plan_sha256"]:
            raise NetworkError("scene_plan_tampered", "The workflow's saved scene plan changed.")
        return json.loads(data)

    def _stage_jobs(self, state, stage, allow_network, budget):
        plan = self._saved_plan(state)
        generation = state["generation"]
        with self._jobs():
            for scene in state["scenes"]:
                if scene["job_id"] is None:
                    record = jobs.prepare(plan, scene["scene_index"], model=generation["model"],
                                          audio_mode=generation["audio_mode"], resolution=generation["resolution"])
                    scene.update(job_id=record["job_id"], job_status=record["status"])
                    self._save(state)

    def _refresh_scenes(self, state):
        records = {}
        with self._jobs():
            for scene in state["scenes"]:
                record = jobs.inspect(scene["job_id"])
                scene.update(job_status=record["status"], request_id=record["request_id"])
                if record.get("media"):
                    scene.update(media_sha256=record["media"]["sha256"], media_bytes=record["media"]["bytes"])
                records[scene["scene_index"]] = record
        return records

    def _stage_generation(self, state, stage, allow_network, budget):
        records = self._refresh_scenes(state)
        if allow_network:
            with self._jobs():
                for scene in state["scenes"]:
                    record = records[scene["scene_index"]]
                    if record["status"] in ("submitted", "pending") and budget["status"] > 0:
                        budget["status"] -= 1
                        try:
                            jobs.status(scene["job_id"], allow_network=True, **self._kwargs())
                            self._trace(state, "generation", f"status_checked_scene_{scene['scene_index']}")
                        except NetworkError as error:   # transient: the saved provider ID is kept; nothing resubmitted
                            self._trace(state, "generation", f"status_unavailable_scene_{scene['scene_index']}", error.code)
            records = self._refresh_scenes(state)
        statuses = [r["status"] for r in records.values()]
        if any(s in ("failed", "expired") for s in statuses):
            raise NetworkError("provider_job_failed", "A scene job failed or expired at the provider; use video-production-retry-scene.")
        if any(s in ("uncertain", "submitting") for s in statuses):
            raise Wait("uncertain_submission", "submission_outcome_unknown")
        if any(s == "prepared" for s in statuses):
            raise Wait("waiting_for_consent", "paid_consent_required")
        if any(s in ("submitted", "pending") for s in statuses):
            raise Wait("waiting_for_provider", "provider_pending" if allow_network else "status_check_needs_network")

    def _stage_downloads(self, state, stage, allow_network, budget):
        records = self._refresh_scenes(state)
        media = self.media_root(state["workflow_id"])
        self._adopt_clips(state, records, media)
        if allow_network:
            with self._jobs():
                for scene in state["scenes"]:
                    if records[scene["scene_index"]]["status"] == "done" and budget["download"] > 0:
                        budget["download"] -= 1
                        jobs.download(scene["job_id"], allow_network=True, media_root=media, **self._kwargs())
                        self._trace(state, "downloads", f"downloaded_scene_{scene['scene_index']}")
            records = self._refresh_scenes(state)
        if any(r["status"] != "downloaded" for r in records.values()):
            raise Wait("waiting_for_provider", "download_needs_network" if not allow_network else "download_pending")

    def _adopt_clips(self, state, records, media):
        """Step 45: a job is shared by workflows with the same plan and settings (for example a narrated
        workflow started after a silent one). Its clip was downloaded into the other workflow's folder,
        so copy it here, only if it is byte-identical to the job's recorded hash. No request is made."""
        for scene in state["scenes"]:
            record = records[scene["scene_index"]]
            target = media / f"{scene['job_id']}.mp4"
            if record["status"] != "downloaded" or target.exists():
                continue
            data = self._adoptable(state, scene["job_id"], record["media"]["sha256"], record["media"]["bytes"])
            if data is None:
                raise NetworkError("clip_unavailable", f"Scene {scene['scene_index']}'s clip was downloaded for another "
                                   "workflow but is missing or changed there; it is never downloaded or paid for again.")
            media.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("wb", dir=media, suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, target)               # never overwrites; a crash leaves no partial clip
            except OSError:
                raise NetworkError("workflow_storage_failed", "Could not copy a reused clip.") from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self._trace(state, "downloads", f"reused_clip_scene_{scene['scene_index']}")

    def _adoptable(self, state, job_id, sha256, size):
        """Bytes of this job's clip from another workflow's media folder, only if byte-identical, else None."""
        for other in self.store.ids():
            if other == state["workflow_id"]:
                continue
            source = self.media_root(other) / f"{job_id}.mp4"
            try:
                data = source.read_bytes() if source.is_file() and not source.is_symlink() else None
            except OSError:
                data = None
            if data is not None and len(data) == size and sha256_bytes(data) == sha256:
                return data
        return None

    def _stage_media_manifest(self, state, stage, allow_network, budget):
        plan = self._saved_plan(state)
        with self._jobs():
            manifest = jobs.media_manifest(plan, [s["job_id"] for s in state["scenes"]], self.media_root(state["workflow_id"]))
        data = json.dumps(manifest, indent=1, sort_keys=True).encode() + b"\n"
        self.store.publish_once(state["workflow_id"], "media-manifest.json", data)
        state["outputs"].update(media_manifest_sha256=sha256_bytes(data),
                                media_digest=sha256_bytes(json.dumps(manifest, sort_keys=True, allow_nan=False).encode()))

    def _stage_preview_revision(self, state, stage, allow_network, budget):
        from .media_production import render_production
        production_id = state["inputs"]["production_id"]
        manifest = json.loads((self.store.folder(state["workflow_id"]) / "media-manifest.json").read_bytes())
        narration = self._narration(state)
        current = self._preview_artifacts(production_id)
        if (current.get("media_sha256") != state["outputs"]["media_digest"]
                or current.get("narration_sha256") != self._narration_sha(state)):
            kwargs = {"renderer": self.renderer} if self.renderer is not None else {}
            render_production(production_id, manifest, self.media_root(state["workflow_id"]), root=self.root,
                              clock=self.clock, narration=narration, **kwargs)
        else:                                          # rendered before a crash: adopt it, never render twice
            self._trace(state, "preview_revision", "adopted_existing_revision")
        artifacts = self._preview_artifacts(production_id)
        state["outputs"].update(preview_video_sha256=artifacts["video_sha256"],
                                preview_manifest_sha256=artifacts["manifest_sha256"])

    def _matching_report(self, production_id):
        from .review import reviewable
        for row in reviewable(production_id, self.root):        # newest first
            if row.get("binding") == "matching":
                return row
        return None

    def _stage_quality(self, state, stage, allow_network, budget):
        from .quality import QualityChecker
        production_id = state["inputs"]["production_id"]
        row = self._matching_report(production_id)
        if row is None:
            kwargs = {k: v for k, v in (("prober", self.prober), ("poster_reader", self.poster_reader)) if v is not None}
            report, _ = QualityChecker(root=self.root, clock=self.clock, **kwargs).run(production_id)
            report_id, result = report["report_id"], report["result"]
        else:
            report_id, result = row["report_id"], row["technical_result"]
        state["outputs"].update(quality_report_id=report_id, quality_result=result)
        if result == "fail":
            raise NetworkError("quality_failed", "The quality report failed; this preview cannot be approved or exported.")

    def _stage_review(self, state, stage, allow_network, budget):
        from .review import history
        doc = history(state["inputs"]["production_id"], self.root)
        if doc["status"] == "history_corrupted":
            raise NetworkError("review_history_corrupted", "The review history is corrupted.")
        report_id = state["outputs"]["quality_report_id"]
        latest = doc["reviews"][0] if doc["reviews"] else None
        if latest is None or latest["report_id"] != report_id or latest["applicability"] != "current":
            state["outputs"]["review_id"] = None
            raise Wait("waiting_for_review", "human_review_required")
        state["outputs"]["review_id"] = latest["review_id"]
        if latest["decision"] != "approved_for_preview":
            raise Wait("review_rejected", "review_" + latest["decision"])
        if not latest["current_preview_approval"]:
            raise Wait("waiting_for_review", "approval_not_current")

    def _stage_export(self, state, stage, allow_network, budget):
        raise Wait("ready_to_export", "explicit_export_required")

    # ------------------------------------------------------------------ explicit, paid, per-job submission
    def submit(self, workflow_id, scene_index, *, consent, allow_network, retry_uncertain=False,
               acknowledge_duplicate_billing=False):
        with self.store.lock(workflow_id):
            state = self.store.read(workflow_id)
            self._verify(state)
            if self._stage(state, "jobs")["status"] != "completed":
                raise NetworkError("jobs_not_prepared", "Resume the workflow until its four jobs are prepared.")
            if self._stage(state, "generation")["status"] == "completed":
                raise NetworkError("generation_complete", "All scenes are already generated; nothing to submit.")
            if retry_uncertain and not acknowledge_duplicate_billing:
                raise NetworkError("duplicate_billing_ack_required",
                                   "Retrying an uncertain submission may bill twice; add --acknowledge-duplicate-billing.")
            scene = self._scene(state, scene_index)
            if state["steps"] >= self.limits["max_steps"]:
                raise NetworkError("workflow_step_limit", "The workflow reached max_steps; start a new workflow.")
            state["steps"] += 1
            outcome, code = "submitted", None
            with self._jobs():
                try:
                    jobs.submit(scene["job_id"], consent=consent, allow_network=allow_network,
                                retry_uncertain=bool(retry_uncertain), **self._kwargs())
                except NetworkError as error:
                    if error.code != "video_submit_uncertain":
                        raise                          # consent, network, refusal: nothing was sent, nothing changed
                    outcome, code = "uncertain", error.code
            self._trace(state, "generation", f"submit_scene_{scene_index}_{outcome}", code)
            generation = self._stage(state, "generation")
            if generation["status"] in ("waiting", "failed"):
                generation["status"] = "pending"
            self._save(state)
            view = self._advance(state, allow_network=False)
            view["submission"] = {"scene_index": scene_index, "job_id": scene["job_id"], "outcome": outcome}
            return view

    def retry_scene(self, workflow_id, scene_index, *, model=None, resolution=None, audio_mode=None):
        """Replace a scene whose provider job failed or expired with a NEW prepared job (different request).
        Nothing is submitted; the new job needs its own consent."""
        with self.store.lock(workflow_id):
            state = self.store.read(workflow_id)
            self._verify(state)
            scene = self._scene(state, scene_index)
            with self._jobs():
                old = jobs.inspect(scene["job_id"])
                if old["status"] not in ("failed", "expired"):
                    raise NetworkError("scene_not_failed", "Only a failed or expired scene job can be replaced.")
                if len(scene["replaced_job_ids"]) >= self.limits["max_scene_replacements"]:
                    raise NetworkError("scene_replacement_limit", "This scene reached max_scene_replacements.")
                generation = self._generation(model or old["request"]["model"],
                                              resolution or old["request"]["resolution"], audio_mode or old["audio_mode"])
                record = jobs.prepare(self._saved_plan(state), scene_index, model=generation["model"],
                                      resolution=generation["resolution"], audio_mode=generation["audio_mode"])
            if record["job_id"] == scene["job_id"] or record["job_id"] in scene["replaced_job_ids"]:
                raise NetworkError("replacement_must_differ",
                                   "Choose a different --model or --resolution; the same request maps to the same job.")
            scene["replaced_job_ids"].append(scene["job_id"])
            scene.update(job_id=record["job_id"], job_status=record["status"], request_id=None, media_sha256=None,
                         media_bytes=None)
            for name in ("generation", "downloads"):
                self._stage(state, name).update(status="pending", error_code=None, attempts=0)
            self._trace(state, "generation", f"replaced_scene_{scene_index}")
            self._save(state)
            return self._advance(state, allow_network=False)

    # ------------------------------------------------------------------ explicit export (Step 38 gates unchanged)
    def export(self, workflow_id, *, purpose):
        from .export import PreviewExporter
        with self.store.lock(workflow_id):
            state = self.store.read(workflow_id)
            self._verify(state)
            if self._stage(state, "quality")["status"] != "completed":
                raise NetworkError("quality_required", "Export needs this workflow's completed quality report.")
            if purpose == "approved_preview":
                self._recheck_review(state)
                self._advance(state, allow_network=False)       # re-evaluate the human decision (never records one)
                state = self.store.read(workflow_id)
                if self._stage(state, "review")["status"] != "completed":
                    raise NetworkError("no_current_preview_approval",
                                       "approved_preview needs a current human approval of this workflow's report.")
            manifest, target = PreviewExporter(self.root, clock=self.clock).export(
                state["inputs"]["production_id"], state["outputs"]["quality_report_id"], purpose=purpose)
            if manifest["restrictions"]["publishable"] is not False:      # defence in depth; Step 38 guarantees it
                raise NetworkError("export_publishable_refused", "Exports must stay publishable: false.")
            state["outputs"].update(export_package_id=manifest["package_id"], export_purpose=purpose)
            if purpose == "approved_preview":
                self._stage(state, "export").update(status="completed", error_code=None, finished_at=self.clock())
                state["status"] = "exported"
            self._trace(state, "export", f"exported_{purpose}")
            self._save(state)
            view = self.view(state)
            view["export"] = {"package_id": manifest["package_id"], "path": str(target), "purpose": purpose,
                              "publishable": False}
            return view

    # ------------------------------------------------------------------ output
    def view(self, state, problems=()):
        workflow_id = state["workflow_id"]
        scenes = []
        for scene in state["scenes"]:
            row = {k: scene[k] for k in ("scene_index", "job_id", "job_status", "request_id", "media_sha256", "replaced_job_ids")}
            if scene["job_status"] == "prepared":
                row["consent"] = f"paid-generate:{scene['job_id']}"
            scenes.append(row)
        return {"notice": NOTICE, "workflow_id": workflow_id, "status": state["status"], "publishable": False,
                "production_id": state["inputs"]["production_id"], "generation": state["generation"],
                "stages": [{k: s[k] for k in ("name", "status", "attempts", "error_code", "started_at", "finished_at")}
                           for s in state["stages"]],
                "narration": self._narration_view(state),
                "scenes": scenes, "outputs": dict(state["outputs"]), "steps": state["steps"],
                "integrity_problems": list(problems), "next": self.next_actions(state)}

    @staticmethod
    def _narration_view(state):
        meta = state.get("narration")
        if not meta:
            return {"present": False, "output": "silent (generated source audio muted)"}
        return {"present": True, "managed_file": meta["managed_file"], "source_sha256": meta["source_sha256"],
                "source_duration_seconds": meta["source_duration_seconds"],
                "padded_duration_seconds": meta["padded_duration_seconds"], "duration_policy": meta["duration_policy"],
                "output": "local narration mixed in; generated source audio muted"}

    def next_actions(self, state):
        w, out = state["workflow_id"], []
        status = state["status"]
        if status == "waiting_for_consent":
            for scene in state["scenes"]:
                if scene["job_status"] == "prepared":
                    out.append(f"python -m vicekrack video-inspect {scene['job_id']}   (read the prompt and cost first)")
                    out.append(f"python -m vicekrack video-production-submit {w} --scene {scene['scene_index']} "
                               f"--consent paid-generate:{scene['job_id']} --allow-network")
        elif status == "uncertain_submission":
            for scene in state["scenes"]:
                if scene["job_status"] in ("uncertain", "submitting"):
                    out.append("Check your xAI usage first: the earlier request may have been billed.")
                    out.append(f"python -m vicekrack video-production-submit {w} --scene {scene['scene_index']} "
                               f"--consent paid-generate:{scene['job_id']} --allow-network --retry-uncertain "
                               "--acknowledge-duplicate-billing")
        elif status == "waiting_for_provider":
            out.append(f"python -m vicekrack video-production-resume {w} --allow-network")
        elif status in ("failed", "blocked"):
            failed = next((s for s in state["stages"] if s["status"] == "failed"), None)
            if failed and failed["error_code"] == "provider_job_failed":
                for scene in state["scenes"]:
                    if scene["job_status"] in ("failed", "expired"):
                        out.append(f"python -m vicekrack video-production-retry-scene {w} --scene {scene['scene_index']} "
                                   "--model grok-imagine-video-1.5-lite")
            elif status == "failed":
                out.append(f"python -m vicekrack video-production-resume {w}")
        elif status in ("waiting_for_review", "review_rejected"):
            production_id, report_id = state["inputs"]["production_id"], state["outputs"]["quality_report_id"]
            out.append(f"python -m vicekrack review-list {production_id}   (shows the binding digest and required --ack values)")
            out.append(f"python -m vicekrack review-record {production_id} --report {report_id} --binding DIGEST "
                       "--decision approved_for_preview --reviewer \"YOUR NAME\"")
            out.append(f"python -m vicekrack video-production-resume {w}")
        elif status == "ready_to_export":
            out.append(f"python -m vicekrack video-production-export {w} --purpose approved_preview")
        if status not in ("exported",):
            out.append(f"python -m vicekrack video-production-inspect {w}")
        return out
