"""Controlled production pipeline (Step 21).

One explicit command turns one selected story into a local, watermarked preview through a
fixed sequence of existing stages:

    brief -> creator -> validate -> plan -> preview

Every stage reuses the earlier implementation (Step 18 select_brief, Step 15 Creator,
Step 11 validator, Step 12 planner, Steps 13/14 renderer). Nothing is published.

Safety model
- One production per story: the ID is derived from profile + candidate and created
  exclusively; an OS lock prevents concurrent execution.
- State is checkpointed before and after every stage. Any failure stops the run; resume
  restarts at the first incomplete stage after re-validating configuration hashes,
  evidence freshness and every completed artifact (hash, content and chain).
- Paid Creator calls need explicit consent on every invocation that may make one. A paid
  request that may have completed without its result being saved becomes `uncertain`
  and needs explicit retry consent. Local stages simply rerun.
- Story history: the brief stage adds a `reserved` entry owned by this production (it
  blocks duplicate productions but not this production's own resume); the entry becomes
  `produced` only after the preview is saved.
- State and traces contain stage names, timestamps, hashes, paths and fixed error codes:
  no prompts, credentials, environment values or raw exceptions.
- Step 33: optional execution events (ContentEvents). The pipeline controller and each
  stage record start, completion and failure; stages finished in an earlier attempt are
  `stage_reused`. "started" is recorded before the intent checkpoint, results after they
  are saved, and a recording failure stops before the next stage (so before any paid
  request); the production stays resumable.
"""

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator

from .content_events import DISABLED, PIPELINE, REUSED, STAGE_COMPONENTS, EventFailure, retry_reason
from .creator import draft_short_script, drafter_for, load_creator_config
from .errors import NetworkError
from .orchestrator import ROOT, read_json
from .persistence import reject_secrets
from .scene_plan import build_scene_plan, capabilities as normalize_capabilities, validate_scene_plan
from .scout_cli import _publish
from .selection import load_profile, prune_history, select_brief, story_features, utc_now
from .short_script import validate_short_script
from .story_brief import count_unverified, validate_story_brief
from .verification import load_policy

STATE_SCHEMA = ROOT / "schemas/production-state.schema.json"
STAGES = ("brief", "creator", "validate", "plan", "preview")
MAX_ATTEMPTS = 3
MAX_TRACE = 100
PRODUCTION_ID = re.compile(r"^prod-[0-9a-f]{24}$")
RUN_ID = re.compile(r"^sel-[0-9a-f]{24}$")
RECORD_ID = re.compile(r"^ver-[0-9a-f]{24}$")
# Failures known to happen before any provider request: safe to retry without consent.
PRE_REQUEST_CODES = {"missing_credentials", "missing_model", "invalid_provider_configuration", "unsupported_model",
                     "adapter_unavailable", "invalid_story_brief", "sensitive_state", "invalid_configuration"}
PROJECT = ROOT  # Configuration always loads from the project; storage root may differ.
DEFAULTS = {"policy": "config/verification.mock.json", "editorial_profile": "config/editorial.mock.json",
            "creator": "config/creator.json", "capabilities": "config/visual-capabilities.json"}


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads(STATE_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def production_id_for(profile, candidate_id):
    """One production per story: same profile and candidate always map to the same ID."""
    return "prod-" + hashlib.sha256(f"{profile}\n{candidate_id}".encode()).hexdigest()[:24]


# ---------------------------------------------------------------- storage

class ProductionStore:
    def __init__(self, root=None):
        self.base = (Path(root if root is not None else ROOT) / "runtime/productions").resolve()

    def folder(self, production_id):
        if not isinstance(production_id, str) or not PRODUCTION_ID.match(production_id):
            raise NetworkError("invalid_production_id", "Production IDs look like prod- followed by 24 hex characters.")
        folder = self.base / production_id
        if folder.is_symlink():
            raise NetworkError("invalid_production_state", "Production folders cannot be symbolic links.")
        return folder

    def state_path(self, production_id):
        return self.folder(production_id) / "state.json"

    def create(self, state):
        """Exclusively create the production folder and its first state (duplicate guard)."""
        validate_state(state)
        self.base.mkdir(parents=True, exist_ok=True)
        try:
            self.folder(state["production_id"]).mkdir()
        except FileExistsError:
            raise NetworkError("production_exists", "This story already has a production; inspect or resume it.") from None
        except OSError:
            raise NetworkError("production_storage_error", "Could not create local production storage.") from None
        self.write(state)

    def write(self, state):
        validate_state(state)
        folder = self.folder(state["production_id"])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, prefix="state.", suffix=".tmp",
                                             delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, folder / "state.json")
            temporary = None
        except (OSError, ValueError, TypeError):
            raise NetworkError("production_storage_error", "Could not save production state; inspect before resuming.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def read(self, production_id):
        path = self.state_path(production_id)
        if path.is_symlink():
            raise NetworkError("invalid_production_state", "Production state cannot be a symbolic link.")
        try:
            state = read_json(path)
        except FileNotFoundError:
            raise NetworkError("production_not_found", "Production does not exist.") from None
        except (OSError, ValueError, UnicodeError):
            raise NetworkError("invalid_production_state", "Production state is unreadable.") from None
        validate_state(state)
        if state["production_id"] != production_id:
            raise NetworkError("invalid_production_state", "Production state does not match its folder.")
        return state

    @contextmanager
    def lock(self, production_id):
        """Nonblocking OS lock; released automatically if the process exits."""
        self.folder(production_id)
        self.base.mkdir(parents=True, exist_ok=True)
        path = self.base / f"{production_id}.lock"
        if path.is_symlink():
            raise NetworkError("invalid_production_state", "Production locks cannot be symbolic links.")
        stream = path.open("a+b")
        locked = False
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                raise NetworkError("production_locked", "Another process is running this production.") from None
            yield
        finally:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def list_ids(self):
        if not self.base.is_dir():
            return []
        return sorted(p.name for p in self.base.iterdir() if p.is_dir() and PRODUCTION_ID.match(p.name))


def validate_state(state):
    try:
        json.dumps(state, allow_nan=False)
    except (TypeError, ValueError):
        raise NetworkError("invalid_production_state", "Production state must contain finite JSON values.") from None
    reject_secrets(state)
    error = next(_validator().iter_errors(state), None)
    if error is not None:
        location = ".".join(str(p) for p in error.absolute_path) or "$"
        raise NetworkError("invalid_production_state", f"Production state rejected at {location}: schema rule '{error.validator}'.")
    if [stage["name"] for stage in state["stages"]] != list(STAGES):
        raise NetworkError("invalid_production_state", "Production stages are out of order.")
    seen_incomplete = False
    for stage in state["stages"]:
        if stage["status"] != "completed":
            seen_incomplete = True
        elif seen_incomplete:
            raise NetworkError("invalid_production_state", "A stage completed after an incomplete stage.")
    finished = all(stage["status"] == "completed" for stage in state["stages"])
    if (state["status"] == "completed") != (finished and state["result"] is not None):
        raise NetworkError("invalid_production_state", "Completed status does not match the stages.")
    if state["production_id"] != production_id_for(state["config"]["profile"], state["config"]["candidate_id"]):
        raise NetworkError("invalid_production_state", "Production ID does not match its story.")


# ---------------------------------------------------------------- configuration

def _config_file(path, root=PROJECT):
    root = Path(root).resolve()
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", path) or path.startswith("/"):
        raise NetworkError("invalid_configuration", "Configuration paths must be relative project paths.")
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise NetworkError("invalid_configuration", "Configuration files must exist inside the project.")
    return {"path": path, "sha256": _sha256_file(target)}


def load_configs(paths):
    """Load and validate every configuration used by a production. No credentials involved."""
    policy, policy_sha = load_policy(paths["policy"])
    profile, profile_sha = load_profile(paths["editorial_profile"])
    if policy["profile"] != profile["profile"]:
        raise NetworkError("profile_mismatch", "Verification policy and editorial profile use different profiles.")
    creator = load_creator_config(paths["creator"])
    capability_file = _config_file(paths["capabilities"])
    try:
        capabilities = read_json(PROJECT / paths["capabilities"])
        normalize_capabilities(capabilities)
    except (OSError, ValueError, UnicodeError):
        raise NetworkError("invalid_configuration", "Cannot read visual capabilities.") from None
    files = {name: _config_file(paths[name]) for name in ("policy", "editorial_profile", "creator")}
    files["capabilities"] = capability_file
    return {"policy": policy, "policy_sha": policy_sha, "profile": profile, "profile_sha": profile_sha,
            "creator": creator, "capabilities": capabilities, "files": files}


def _narration_config(path):
    if path is None:
        return None
    from .narration import load_narration
    resolved = Path(path).resolve()
    load_narration(resolved)  # Validate before anything is created (Step 14 rules and codes).
    return {"path": str(resolved), "sha256": _sha256_file(resolved)}


def _check_saved_config(state):
    config = state["config"]
    paths = {name: config[name]["path"] for name in ("policy", "editorial_profile", "creator", "capabilities")}
    loaded = load_configs(paths)
    for name in paths:
        if loaded["files"][name]["sha256"] != config[name]["sha256"]:
            raise NetworkError("configuration_mismatch", "A configuration file changed since this production started.")
    creator = loaded["creator"]["execution"]
    if (creator["adapter"], creator["model"]) != (config["creator"]["adapter"], config["creator"]["model"]):
        raise NetworkError("configuration_mismatch", "Creator configuration changed since this production started.")
    if config["narration"] is not None:
        path = Path(config["narration"]["path"])
        try:
            changed = _sha256_file(path) != config["narration"]["sha256"]
        except OSError:
            raise NetworkError("narration_changed", "The narration file is missing; restore it before resuming.") from None
        if changed:
            raise NetworkError("narration_changed", "The narration file changed since this production started.")
    return loaded


# ---------------------------------------------------------------- history reservation

def _history_entry(brief, record, now, production_id, state):
    features = story_features(record)
    return {"selection_id": brief["editorial"]["selection_id"], "record_id": record["record_id"],
            "candidate_id": record["candidate"]["candidate_id"], "url_sha256": record["candidate"]["url_sha256"],
            "brief_id": brief["brief_id"], "topics": list(brief["editorial"]["topics"]), **features,
            "state": state, "production_id": production_id, "selected_at": now}


def _write_reservation(profile, production_id, entry, root, now):
    """Replace this production's own history entry (if any) with `entry`, conflict-checked."""
    from .selection_cli import load_history, save_history
    history, digest = load_history(profile, root)
    current = prune_history(history, profile, now)
    others = [e for e in current["entries"]
              if e.get("production_id") != production_id and e["selection_id"] != entry["selection_id"]]
    save_history(prune_history(dict(current, entries=[entry, *others]), profile, now), digest, profile, root)


# ---------------------------------------------------------------- pipeline

class Pipeline:
    """Executes/resumes one production. Injection points exist only for tests."""

    def __init__(self, root=None, clock=None, drafter=None, renderer=None, providers=None, events=None):
        self.root = root
        self.events = events or DISABLED
        self.store = ProductionStore(root)
        # Late-bound default so the module clock can be patched (keeps CLI tests date-independent).
        self.clock = clock if clock is not None else (lambda: utc_now())
        self.drafter = drafter
        self.providers = providers
        if renderer is None:
            from .preview import render_preview
            renderer = render_preview
        self.renderer = renderer

    # -- start

    def produce(self, selection_run_id, record_id, *, paths=None, allow_paid=False, narration=None,
                allow_draft_preview=False):
        from .selection_cli import load_history, load_report
        from .verification_cli import load_record
        if not isinstance(selection_run_id, str) or not RUN_ID.match(selection_run_id):
            raise NetworkError("invalid_selection_id", "Selection run IDs look like sel- followed by 24 hex characters.")
        if not isinstance(record_id, str) or not RECORD_ID.match(record_id):
            raise NetworkError("invalid_record_id", "Record IDs look like ver- followed by 24 hex characters.")
        paths = {**DEFAULTS, **(paths or {})}
        loaded = load_configs(paths)
        execution = loaded["creator"]["execution"]
        paid = execution["adapter"] != "mock"
        if paid and not allow_paid:
            raise NetworkError("paid_consent_required", "This Creator configuration makes a paid request; pass --allow-paid.")
        narration_config = _narration_config(narration)
        now = self.clock()
        # Pre-check everything the brief stage will check, before any state is created.
        report = load_report(selection_run_id, self.root)
        record = load_record(record_id, loaded["policy"], loaded["policy_sha"], self.root)
        production_id = production_id_for(loaded["profile"]["profile"], record["candidate"]["candidate_id"])
        self.events.bind(production_id, production_id)
        if self.store.state_path(production_id).exists():
            raise NetworkError("production_exists", "This story already has a production; inspect or resume it.")
        history, _ = load_history(loaded["profile"], self.root)
        select_brief(record, report, profile=loaded["profile"], profile_sha256=loaded["profile_sha"],
                     policy=loaded["policy"], policy_sha256=loaded["policy_sha"],
                     history=prune_history(history, loaded["profile"], now), now=now)
        files = loaded["files"]
        state = {
            "contract": "production_run", "version": "1.0", "production_id": production_id, "status": "ready",
            "created_at": now, "updated_at": now,
            "config": {"profile": loaded["profile"]["profile"], "selection_run_id": selection_run_id,
                       "record_id": record_id, "candidate_id": record["candidate"]["candidate_id"],
                       "policy": files["policy"], "editorial_profile": files["editorial_profile"],
                       "capabilities": files["capabilities"],
                       "creator": {**files["creator"], "adapter": execution["adapter"], "model": execution["model"],
                                   "paid": paid},
                       "narration": narration_config, "allow_draft_preview": bool(allow_draft_preview)},
            "stages": [{"name": name, "status": "pending", "attempts": 0, "started_at": None, "finished_at": None,
                        "error_code": None, "artifacts": {}} for name in STAGES],
            "trace": [], "result": None,
        }
        self.store.create(state)
        return self._run(production_id, allow_paid=allow_paid, retry_uncertain=False)

    def resume(self, production_id, *, allow_paid=False, retry_uncertain=False):
        self.events.bind(production_id, production_id)
        return self._run(production_id, allow_paid=allow_paid, retry_uncertain=retry_uncertain)

    def _event_stop(self, state, failure, stage=None):
        """Recording failed before new work: nothing else runs; saved state is untouched."""
        self.events.abort(failure.code)
        summary = self._summary(state)
        summary["error"] = {"stage": stage["name"] if stage else None, "code": failure.code}
        summary["events"] = self.events.summary()
        return summary

    # -- execution

    def _trace(self, state, stage, event, code=None):
        state["trace"] = (state["trace"] + [{"stage": stage, "event": event, "at": self.clock(), "error_code": code}])[-MAX_TRACE:]

    def _save(self, state):
        state["updated_at"] = self.clock()
        self.store.write(state)

    def _run(self, production_id, *, allow_paid, retry_uncertain):
        with self.store.lock(production_id):
            state = self.store.read(production_id)
            if state["status"] == "completed":
                raise NetworkError("production_completed", "This production already finished; nothing to resume.")
            loaded = _check_saved_config(state)
            self._verify_artifacts(state, loaded)
            events, refs = self.events, [{"kind": "production", "id": production_id}]
            try:
                events.check()
            except EventFailure as failure:
                return self._event_stop(state, failure)
            events.emit(PIPELINE, "pipeline", "stage_started", "started", refs=refs)
            index = next(i for i, s in enumerate(state["stages"]) if s["status"] != "completed") \
                if any(s["status"] != "completed" for s in state["stages"]) else len(STAGES)
            for done in state["stages"][:index]:
                events.emit(STAGE_COMPONENTS[done["name"]], done["name"], "stage_reused", "completed",
                            reason_codes=REUSED, details={"attempt": max(1, done["attempts"])})
            if index < len(STAGES):
                stage = state["stages"][index]
                paid_stage = stage["name"] == "creator" and state["config"]["creator"]["paid"]
                if stage["status"] == "running":
                    # Found mid-stage with the lock free: the process stopped during this stage.
                    stage["status"] = "uncertain" if paid_stage else "failed"
                    stage["error_code"] = "interrupted"
                    state["status"] = stage["status"]
                    self._trace(state, stage["name"], "interrupted", "interrupted")
                    self._save(state)
                if stage["status"] == "uncertain" and not retry_uncertain:
                    events.emit(PIPELINE, "pipeline", "stage_failed", "failed", reason_codes=["uncertain_stage"], refs=refs)
                    raise NetworkError("uncertain_stage", "A paid request may have completed without its result being saved. "
                                                          "Use --retry-uncertain (and --allow-paid) to authorize another request.")
                if paid_stage and not allow_paid:
                    events.emit(PIPELINE, "pipeline", "stage_failed", "failed", reason_codes=["paid_consent_required"], refs=refs)
                    raise NetworkError("paid_consent_required", "The next stage makes a paid request; pass --allow-paid.")
                if index > 0:
                    try:
                        self._check_evidence(state, loaded)
                    except NetworkError as error:
                        events.emit(PIPELINE, "pipeline", "stage_failed", "failed", reason_codes=[error.code], refs=refs)
                        raise
            for stage in state["stages"][index:]:
                if stage["attempts"] >= MAX_ATTEMPTS:
                    state["status"] = "failed"
                    stage["error_code"] = "retry_exhausted"
                    self._save(state)
                    events.emit(PIPELINE, "pipeline", "stage_failed", "failed", reason_codes=["retry_exhausted"], refs=refs)
                    return self._summary(state)
                component, attempt = STAGE_COMPONENTS[stage["name"]], {"attempt": stage["attempts"] + 1}
                reason = retry_reason(stage["status"], stage["error_code"]) if stage["attempts"] else None
                try:
                    events.check()                      # never start new work (or a paid request) after a failure
                    events.emit(component, stage["name"], "stage_started", "started",
                                reason_codes=[reason] if reason else [], details=attempt)
                    events.check()
                except EventFailure as failure:
                    return self._event_stop(state, failure, stage)
                stage.update(status="running", attempts=stage["attempts"] + 1, started_at=self.clock(),
                             finished_at=None, error_code=None)
                state["status"] = "running"
                self._trace(state, stage["name"], "started")
                self._save(state)  # Intent checkpoint before any work (and before any paid request).
                try:
                    artifacts = getattr(self, "_stage_" + stage["name"])(state, loaded)
                except NetworkError as error:
                    code = error.code
                except OSError:
                    code = "production_storage_error"
                except Exception:
                    code = "stage_error"  # Never store raw exception text.
                else:
                    stage.update(status="completed", finished_at=self.clock(), artifacts=artifacts)
                    self._trace(state, stage["name"], "completed")
                    self._save(state)
                    events.emit(component, stage["name"], "stage_completed", "completed", details=attempt)  # after saving
                    continue
                paid_stage = stage["name"] == "creator" and state["config"]["creator"]["paid"]
                outcome = "uncertain" if paid_stage and code not in PRE_REQUEST_CODES else "failed"
                stage.update(status=outcome, finished_at=self.clock(), error_code=code)
                state["status"] = outcome
                self._trace(state, stage["name"], outcome, code)
                self._save(state)
                if outcome == "uncertain":              # a paid request may have completed: outcome unknown
                    events.emit(component, stage["name"], "stage_interrupted", "uncertain", reason_codes=[code], details=attempt)
                else:
                    events.emit(component, stage["name"], "stage_failed", "failed", reason_codes=[code], details=attempt)
                events.emit(PIPELINE, "pipeline", "stage_failed", "failed", reason_codes=[code], refs=refs)
                return self._summary(state)
            self._finalize(state, loaded)
            if state["status"] == "completed":
                events.emit(PIPELINE, "pipeline", "stage_completed", "completed", refs=refs)
            else:
                events.emit(PIPELINE, "pipeline", "stage_failed", "failed",
                            reason_codes=[state["stages"][-1]["error_code"] or "finalize_failed"], refs=refs)
            return self._summary(state)

    def _finalize(self, state, loaded):
        try:
            brief = self._load_json(state, "brief", "brief_path")
            from .verification_cli import load_record
            record = load_record(state["config"]["record_id"], loaded["policy"], loaded["policy_sha"], self.root)
            now = self.clock()
            _write_reservation(loaded["profile"], state["production_id"],
                               _history_entry(brief, record, now, state["production_id"], "produced"), self.root, now)
        except NetworkError as error:
            state["status"] = "failed"
            state["stages"][-1]["error_code"] = error.code  # Preview is saved; resume only retries this step.
            self._save(state)
            return
        preview = state["stages"][-1]["artifacts"]
        folder = self.store.folder(state["production_id"])
        state["result"] = {"preview_file": str(folder / preview["preview_file"]),
                           "manifest_file": str(folder / preview["manifest_file"]), "publishable": False,
                           "preview_only": True, "blocked_for_production": preview["blocked_for_production"],
                           "audio_present": preview["audio_present"]}
        state["stages"][-1]["error_code"] = None
        state["status"] = "completed"
        self._save(state)

    def _summary(self, state):
        stages = {s["name"]: s["status"] for s in state["stages"]}
        failed = next((s for s in state["stages"] if s["status"] in ("failed", "uncertain")), None)
        if failed is None and state["status"] == "failed":
            failed = state["stages"][-1]
        return {"production_id": state["production_id"], "status": state["status"], "stages": stages,
                "error": {"stage": failed["name"], "code": failed["error_code"]} if failed else None,
                "result": state["result"], "published": False}

    # -- evidence and artifacts

    def _check_evidence(self, state, loaded):
        """Before continuing after the brief: the record must still replay and be fresh."""
        from .verification_cli import load_record
        record = load_record(state["config"]["record_id"], loaded["policy"], loaded["policy_sha"], self.root)
        age = _parse(self.clock()) - _parse(record["verified_at"])
        if age > timedelta(days=loaded["policy"]["rules"]["max_record_age_days"]) or age < timedelta(minutes=-5):
            raise NetworkError("stale_evidence", "The verification record is too old; verify and select again.")
        return record

    def _artifact_path(self, state, relative):
        folder = self.store.folder(state["production_id"]).resolve()
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise NetworkError("artifact_tampered", "A production artifact path is invalid.")
        path = (folder / relative).resolve()
        if not path.is_relative_to(folder) or path.is_symlink():
            raise NetworkError("artifact_tampered", "A production artifact is outside its production folder.")
        return path

    def _hashed(self, state, relative, expected):
        path = self._artifact_path(state, relative)
        try:
            actual = _sha256_file(path)
        except OSError:
            raise NetworkError("artifact_missing", "A completed production artifact is missing.") from None
        if actual != expected:
            raise NetworkError("artifact_tampered", "A completed production artifact changed.")
        return path

    def _load_json(self, state, stage_name, key):
        artifacts = next(s for s in state["stages"] if s["name"] == stage_name)["artifacts"]
        path = self._hashed(state, artifacts[key], artifacts[key.replace("_path", "_sha256")])
        try:
            return read_json(path)
        except (OSError, ValueError, UnicodeError):
            raise NetworkError("artifact_tampered", "A completed production artifact is unreadable.") from None

    def _verify_artifacts(self, state, loaded):
        """Hash, content and chain checks for every completed stage before resuming."""
        done = {s["name"] for s in state["stages"] if s["status"] == "completed"}
        try:
            if "brief" in done:
                brief = self._load_json(state, "brief", "brief_path")
                validate_story_brief(brief)
                if (brief.get("verification", {}).get("record_ids") != [state["config"]["record_id"]]
                        or brief.get("editorial", {}).get("selection_run_id") != state["config"]["selection_run_id"]):
                    raise NetworkError("artifact_tampered", "The saved brief does not belong to this production.")
            if "creator" in done:
                script = self._load_json(state, "creator", "script_path")
                validate_short_script(script)
                if script["claims"] != brief["claims"] or script["sources"] != brief["sources"]:
                    raise NetworkError("artifact_tampered", "The saved script does not match its brief.")
            if "validate" in done:
                artifacts = state["stages"][2]["artifacts"]
                if artifacts.get("draft") != (count_unverified(script) > 0) or artifacts.get("script_sha256") \
                        != state["stages"][1]["artifacts"]["script_sha256"]:
                    raise NetworkError("artifact_tampered", "The saved validation does not match the script.")
            if "plan" in done:
                plan = self._load_json(state, "plan", "plan_path")
                validate_scene_plan(plan)
                if plan["script"] != script or plan["blocked_for_production"] != state["stages"][2]["artifacts"]["draft"]:
                    raise NetworkError("artifact_tampered", "The saved scene plan does not match the script.")
            if "preview" in done:
                artifacts = state["stages"][4]["artifacts"]
                self._hashed(state, artifacts["preview_file"], artifacts["video_sha256"])
                manifest = read_json(self._hashed(state, artifacts["manifest_file"], artifacts["manifest_sha256"]))
                if manifest.get("plan_id") != plan["plan_id"] or manifest.get("publishable") is not False:
                    raise NetworkError("artifact_tampered", "The saved preview does not match the plan.")
        except NetworkError as error:
            if error.code in ("artifact_tampered", "artifact_missing"):
                raise
            raise NetworkError("artifact_tampered", "A completed production artifact is no longer valid.") from None
        except (OSError, ValueError, KeyError, TypeError):
            raise NetworkError("artifact_tampered", "A completed production artifact is no longer valid.") from None

    def _save_artifact(self, state, document, prefix):
        folder = self.store.folder(state["production_id"])
        name = f"{prefix}-{uuid4().hex}.json"
        if not _publish(document, folder, name):
            raise NetworkError("production_storage_error", "Could not save a production artifact without overwriting.")
        return name, _sha256_file(folder / name)

    # -- stages

    def _stage_brief(self, state, loaded):
        from .selection_cli import load_history, load_report
        from .verification_cli import load_record
        config, now = state["config"], self.clock()
        report = load_report(config["selection_run_id"], self.root)
        record = load_record(config["record_id"], loaded["policy"], loaded["policy_sha"], self.root)
        history, _ = load_history(loaded["profile"], self.root)
        current = prune_history(history, loaded["profile"], now)
        # This production's own reservation (from an interrupted earlier attempt) must not block it.
        mine = dict(current, entries=[e for e in current["entries"] if e.get("production_id") != state["production_id"]])
        brief, _ = select_brief(record, report, profile=loaded["profile"], profile_sha256=loaded["profile_sha"],
                                policy=loaded["policy"], policy_sha256=loaded["policy_sha"], history=mine, now=now)
        name, digest = self._save_artifact(state, brief, "brief")
        _write_reservation(loaded["profile"], state["production_id"],
                           _history_entry(brief, record, now, state["production_id"], "reserved"), self.root, now)
        return {"brief_path": name, "brief_sha256": digest, "brief_id": brief["brief_id"],
                "selection_id": brief["editorial"]["selection_id"]}

    def _stage_creator(self, state, loaded):
        brief = self._load_json(state, "brief", "brief_path")
        creator = state["config"]["creator"]
        drafter = self.drafter or drafter_for(creator["adapter"], self.providers)
        script = draft_short_script(brief, adapter=creator["adapter"], model=creator["model"], drafter=drafter,
                                    created_at=self.clock())
        name, digest = self._save_artifact(state, script, "script")
        return {"script_path": name, "script_sha256": digest, "script_id": script["script_id"],
                "provider": creator["adapter"]}

    def _stage_validate(self, state, loaded):
        brief = self._load_json(state, "brief", "brief_path")
        script = self._load_json(state, "creator", "script_path")
        validate_short_script(script)
        if script["claims"] != brief["claims"] or script["sources"] != brief["sources"]:
            raise NetworkError("creator_changed_facts", "The script's claims or sources differ from the brief.")
        unverified = count_unverified(script)
        return {"script_sha256": state["stages"][1]["artifacts"]["script_sha256"], "draft": unverified > 0,
                "claims_unverified": unverified, "claims_total": len(script["claims"])}

    def _stage_plan(self, state, loaded):
        script = self._load_json(state, "creator", "script_path")
        draft = state["stages"][2]["artifacts"]["draft"]
        plan = build_scene_plan(script, loaded["capabilities"], draft=draft)
        name, digest = self._save_artifact(state, plan, "plan")
        return {"plan_path": name, "plan_sha256": digest, "plan_id": plan["plan_id"], "mode": plan["mode"]}

    def _stage_preview(self, state, loaded):
        plan = self._load_json(state, "plan", "plan_path")
        if plan["blocked_for_production"] and not state["config"]["allow_draft_preview"]:
            raise NetworkError("draft_preview_required", "This plan is a draft; start with --allow-draft-preview to render it.")
        narration = state["config"]["narration"]
        folder = self.store.folder(state["production_id"]).resolve()
        result = self.renderer(plan, allow_draft=state["config"]["allow_draft_preview"],
                               directory=folder / "previews", narration=Path(narration["path"]) if narration else None)
        if result.get("publishable") is not False or result.get("preview_only") is not True:
            raise NetworkError("invalid_render_output", "Preview output must be preview-only and not publishable.")
        video, manifest = Path(result["preview_file"]).resolve(), Path(result["manifest_file"]).resolve()
        if not video.is_relative_to(folder) or not manifest.is_relative_to(folder):
            raise NetworkError("invalid_render_output", "Preview output is outside the production folder.")
        return {"preview_file": str(video.relative_to(folder)), "video_sha256": _sha256_file(video),
                "manifest_file": str(manifest.relative_to(folder)), "manifest_sha256": _sha256_file(manifest),
                "blocked_for_production": bool(result["source_blocked_for_production"]),
                "audio_present": bool(result["audio_present"])}


def inspect_production(production_id, root=None):
    """Saved state; a stage left `running` with the lock free is reported as interrupted."""
    store = ProductionStore(root)
    with store.lock(production_id):
        state = store.read(production_id)
    view = deepcopy(state)
    for stage in view["stages"]:
        if stage["status"] == "running":
            stage["status"] = "uncertain" if stage["name"] == "creator" and state["config"]["creator"]["paid"] else "interrupted"
            view["status"] = stage["status"]
    return view


def list_productions(root=None):
    store, rows = ProductionStore(root), []
    for production_id in store.list_ids():
        try:
            view = inspect_production(production_id, root)
        except NetworkError as error:
            rows.append({"production_id": production_id, "error": error.code})
            continue
        current = next((s["name"] for s in view["stages"] if s["status"] != "completed"), None)
        rows.append({"production_id": production_id, "status": view["status"], "next_stage": current,
                     "record_id": view["config"]["record_id"], "creator": view["config"]["creator"]["adapter"],
                     "updated_at": view["updated_at"]})
    return rows
