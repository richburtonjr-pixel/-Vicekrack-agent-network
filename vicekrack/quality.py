"""Production quality report (Step 22): read-only technical checks of one production run.

The report answers "are this production's local artifacts complete, consistent and
technically as specified?" with `pass`, `needs_review` or `fail` plus specific reason
codes. It never answers whether facts are true, rights are cleared or publishing is
allowed: every report states factual_accuracy_verified=false, rights_cleared=false,
publishable=false and permission_to_publish=false.

Checks reuse the existing validators (Story Brief, Short Script, scene plan, verification
record replay, narration normalization). Media is measured with the local ffmpeg from
requirements-render.txt in a restricted subprocess; raw tool output is parsed in memory
and never stored. Nothing is repaired, retried, re-rendered or published.

Step 36 (report version 1.1): the checks run against one consistent snapshot. Every file
they rely on (production state, brief, script, plan, manifest, video, posters, verification
record, narration input, configuration files) is read once; JSON is parsed from those bytes
and media tools are re-checked by hash afterwards. The report's `binding` records each
file's role, safe reference, SHA-256 and size (never credentials or the narration path). If
any file changed while the checks ran, the binding is `changed_during_inspection`, a pass is
downgraded to needs_review, and nothing is reported as bound. A 1.1 manifest's poster
hashes are checked too. The binding is byte identity only; `evidence_freshness` and the
technical checks stay separate, and none of them grants permission to publish.
"""

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from uuid import uuid4

from jsonschema import Draft202012Validator

from .artifact_binding import (BINDING_NOTE, BINDING_VERSION, MAX_JSON_BYTES, MAX_POSTER_BYTES, MAX_VIDEO_BYTES,
                               Snapshot, norm, posters_bound, safe_file, validate_manifest)
from .errors import NetworkError
from .orchestrator import ROOT, read_json
from .persistence import reject_secrets
from .production import PROJECT, STAGES, ProductionStore, validate_state
from .scene_plan import validate_scene_plan
from .scout_cli import _publish
from .short_script import FORMATS, validate_short_script
from .story_brief import count_unverified, validate_story_brief

REPORT_SCHEMA = ROOT / "schemas/quality-report.schema.json"
CHECK_ORDER = ("state", "artifacts", "provenance", "evidence_freshness", "draft_restrictions", "scene_timing",
               "video", "audio", "manifest_consistency", "history")
EXPECTED = {"width": 1080, "height": 1920, "fps": 24, "duration": 15.0, "frames": 360}
DURATION_TOLERANCE = 0.05        # Video container/decode duration.
AUDIO_MAX_EXTRA = 0.1            # AAC encoder padding may add a few milliseconds; never shorter.
PROBE_TIMEOUT = 60
WATERMARK = {True: (255, 190, 85), False: (89, 214, 193)}   # Draft vs normal band colour (preview.py).
WATERMARK_POINTS = ((70, 114), (1012, 114))
NOTES = [
    "Technical checks only: a pass means local artifacts are complete and internally consistent.",
    "Facts were not independently fact-checked by this report; claim status comes from the Verification stage.",
    "Image, footage, music and narration rights were not checked.",
    "This report never grants permission to publish; previews remain publishable: false.",
    BINDING_NOTE,
]
REF = re.compile(r"^(?!\.\.?(/|$))[A-Za-z0-9._-]+(/(?!\.\.?(/|$))[A-Za-z0-9._-]+){0,3}$")  # no "." or ".." parts
ROLE_OF = {"brief_path": "brief", "script_path": "script", "plan_path": "scene_plan", "preview_file": "video",
           "manifest_file": "preview_manifest", "media_path": "media_manifest"}
CONFIGS = (("verification_policy", "policy"), ("editorial_profile", "editorial_profile"), ("creator", "creator"),
           ("capabilities", "capabilities"))


def _json_bytes(data):
    def reject_constant(value):
        raise ValueError("Non-finite numbers are not JSON values")
    return json.loads(data.decode("utf-8-sig"), parse_constant=reject_constant)


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- media probing

class ProbeUnavailable(Exception):
    pass


class ProbeFailed(Exception):
    pass


def _ffmpeg(exe, arguments):
    """Run ffmpeg with an allowlisted environment and a timeout; return stderr text (in memory only)."""
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG")
           if key in os.environ}
    try:
        completed = subprocess.run([exe, "-hide_banner", "-nostdin", *arguments], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=PROBE_TIMEOUT,
                                   shell=False, env=env, check=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise ProbeFailed("probe_timeout") from None
    except OSError:
        raise ProbeUnavailable() from None
    return completed.returncode, completed.stderr.decode("utf-8", errors="replace")[-20000:]


def _seconds(match):
    hours, minutes, seconds = match
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe_media(path):
    """Measure a local MP4: dimensions, fps, duration, decoded frames, audio presence/length."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        raise ProbeUnavailable() from None
    _, header = _ffmpeg(exe, ["-i", str(path)])
    video = re.search(r"Stream #0:\d+[^\n]*Video: (\w+)[^\n]*?(\d{2,5})x(\d{2,5})[^\n]*?(\d+(?:\.\d+)?) fps", header)
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", header)
    if video is None or duration is None:
        raise ProbeFailed("video_unreadable")
    audio = re.search(r"Stream #0:\d+[^\n]*Audio: (\w+)", header)
    code, decoded = _ffmpeg(exe, ["-xerror", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"])
    frames = re.findall(r"frame=\s*(\d+)", decoded)
    if code != 0 or not frames:
        raise ProbeFailed("video_decode_failed")
    result = {"video_codec": video.group(1), "width": int(video.group(2)), "height": int(video.group(3)),
              "fps": float(video.group(4)), "duration": round(_seconds(duration.groups()), 3),
              "frames": int(frames[-1]), "audio_present": audio is not None, "audio_codec": None, "audio_duration": None}
    if audio is not None:
        code, decoded = _ffmpeg(exe, ["-xerror", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"])
        times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", decoded)
        if code != 0 or not times:
            raise ProbeFailed("audio_decode_failed")
        result.update(audio_codec=audio.group(1), audio_duration=round(_seconds(times[-1]), 3))
    return result


def poster_info(path):
    """(width, height, band colours at the watermark sample points) or raises ProbeUnavailable."""
    try:
        from PIL import Image
    except ImportError:
        raise ProbeUnavailable() from None
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            return image.size, [image.getpixel(point) for point in WATERMARK_POINTS]
    except (OSError, ValueError):
        raise ProbeFailed("poster_unreadable") from None


# ---------------------------------------------------------------- checks

class Check:
    def __init__(self, check_id):
        self.check_id, self.failures, self.reviews, self.details, self.unavailable = check_id, [], [], {}, []

    def fail(self, code):
        self.failures.append(code)

    def review(self, code):
        self.reviews.append(code)

    def skip(self, code):
        self.unavailable.append(code)

    def result(self):
        reasons = list(dict.fromkeys(self.failures + self.reviews + self.unavailable))
        if self.failures:
            status = "fail"
        elif self.unavailable and not self.details:
            status = "unavailable"
        elif self.reviews or self.unavailable:
            status = "needs_review"
        else:
            status = "pass"
        details = {k: v for k, v in self.details.items() if v is None or isinstance(v, (str, int, float, bool))}
        return {"check_id": self.check_id, "status": status, "reasons": reasons, "details": details}


class QualityChecker:
    def __init__(self, root=None, clock=None, prober=None, poster_reader=None, events=None):
        # Defaults are looked up at call time (simple to replace in tests).
        from .content_events import DISABLED
        self.events = events or DISABLED            # Step 33: optional execution events
        self.root = root
        self.clock = clock or (lambda: utc_now())
        self.prober = prober or (lambda path: probe_media(path))
        self.poster_reader = poster_reader or (lambda path: poster_info(path))
        self.store = ProductionStore(root)

    def run(self, production_id):
        """Check one production under its lock and save the report. Returns (report, path)."""
        from .content_events import QUALITY
        events, refs = self.events, [{"kind": "production", "id": production_id}]
        events.bind(production_id, production_id)       # same correlation as the production's attempts
        with self.store.lock(production_id):
            events.check()                              # a recording failure stops before any checks run
            events.emit(QUALITY, "quality", "stage_started", "started", refs=refs)
            self.snapshot = Snapshot()
            try:
                checks = self._checks(production_id)
            except NetworkError as error:
                events.emit(QUALITY, "quality", "stage_failed", "failed", reason_codes=[error.code], refs=refs)
                raise
            changed = self.snapshot.recheck()                # still under the production lock
        report = self._report(production_id, checks, self._binding(changed))
        folder = Path(self.root if self.root is not None else ROOT) / "runtime/quality"
        if not _publish(report, folder, report["report_id"] + ".json"):
            events.emit(QUALITY, "quality", "stage_failed", "failed", reason_codes=["quality_report_exists"], refs=refs)
            raise NetworkError("quality_report_exists", "An identical report already exists.")
        events.emit(QUALITY, "quality", "stage_completed", "completed", reason_codes=["result_" + report["result"]],
                    refs=refs + [{"kind": "quality_report", "id": report["report_id"]}])   # after the report is saved
        return report, (folder / (report["report_id"] + ".json")).resolve()

    # -- helpers

    def _path(self, relative, limit=MAX_POSTER_BYTES):
        """A regular file inside the production folder, reached without symbolic links (or None)."""
        path, problem = safe_file(self.folder, relative, limit)
        if path is None and problem == "missing":
            relative = norm(relative)
            return self.folder / relative if isinstance(relative, str) else None    # reported as missing by callers
        return path

    def _poster_ref(self, poster):
        base = PurePosixPath(norm(self.stages["preview"]["artifacts"]["preview_file"])).parent
        return str(base / poster) if isinstance(poster, str) and "/" not in poster and "\\" not in poster else None

    def _snap(self, role, relative, path, *, index=None, limit=MAX_JSON_BYTES, keep=False):
        ref = norm(relative)
        if not isinstance(ref, str) or not REF.match(ref):
            return None
        return self.snapshot.read(("file", ref), path, role=role, ref=ref, index=index, max_bytes=limit, keep=keep)

    def _artifact(self, check, stage, key, digest_key):
        artifacts = self.stages[stage]["artifacts"]
        limit = MAX_VIDEO_BYTES if key == "preview_file" else MAX_JSON_BYTES
        path, problem = safe_file(self.folder, artifacts.get(key), limit)
        if path is None:
            check.fail(f"{stage}_artifact_missing" if problem == "missing" else
                       f"{stage}_artifact_too_large" if problem == "file_too_large" else f"{stage}_artifact_outside_production")
            return None
        self._snap(ROLE_OF[key], artifacts.get(key), path, limit=limit, keep=key != "preview_file")   # JSON kept: read once
        digest = self.snapshot.entries.get(("file", norm(artifacts.get(key))), {}).get("sha256")
        if digest is None:
            check.fail(f"{stage}_artifact_missing")
            return None
        if digest != artifacts.get(digest_key):
            check.fail(f"{stage}_artifact_hash_mismatch")
            return None
        return path

    def _load(self, check, stage, key, digest_key, validate, code):
        path = self._artifact(check, stage, key, digest_key)
        if path is None:
            return None
        try:
            document = _json_bytes(self._snap(ROLE_OF[key], self.stages[stage]["artifacts"][key], path, keep=True))
            validate(document)
        except (NetworkError, OSError, ValueError, UnicodeError, TypeError, KeyError):
            check.fail(code)
            return None
        return document

    # -- check sequence

    def _checks(self, production_id):
        results = {}
        state_check = Check("state")
        path = self.store.state_path(production_id)
        self.folder = self.store.folder(production_id).resolve()
        data = self.snapshot.read(("file", "state.json"), path, role="production_state", ref="state.json", keep=True)
        entry = self.snapshot.entries[("file", "state.json")]
        if entry["status"] == "missing":
            raise NetworkError("production_not_found", "Production does not exist.")
        try:
            self.state = _json_bytes(data)
            validate_state(self.state)
            if self.state["production_id"] != production_id:
                raise ValueError
        except (NetworkError, ValueError, UnicodeError, TypeError, AttributeError):
            state_check.fail("production_state_invalid")
            self.state = None
        if self.state is not None:
            for name, key in CONFIGS:
                config_path = self.state["config"][key]["path"]
                found, _ = safe_file(PROJECT, config_path, MAX_JSON_BYTES)
                if found is not None:
                    self.snapshot.read(("config", name), found, role=name, ref=config_path)
            self.stages = {s["name"]: s for s in self.state["stages"]}
            incomplete = [s["name"] for s in self.state["stages"] if s["status"] != "completed"]
            state_check.details.update(status=self.state["status"], completed_stages=len(STAGES) - len(incomplete),
                                       creator=self.state["config"]["creator"]["adapter"])
            if incomplete or self.state["status"] != "completed":
                state_check.fail("production_incomplete")
                state_check.details["first_incomplete_stage"] = incomplete[0] if incomplete else None
            if self.state["result"] is not None and self.state["result"]["publishable"] is not False:
                state_check.fail("publishable_flag_invalid")
        results["state"] = state_check.result()
        if state_check.failures:
            # Without a valid, completed production the remaining checks cannot be trusted.
            for name in CHECK_ORDER[1:]:
                check = Check(name)
                check.skip("requires_completed_production")
                results[name] = check.result()
            return [results[name] for name in CHECK_ORDER]
        for name in CHECK_ORDER[1:]:
            results[name] = getattr(self, "_check_" + name)().result()
        return [results[name] for name in CHECK_ORDER]

    def _check_artifacts(self):
        check = Check("artifacts")
        self.brief = self._load(check, "brief", "brief_path", "brief_sha256", validate_story_brief, "brief_invalid")
        self.script = self._load(check, "creator", "script_path", "script_sha256", validate_short_script, "script_invalid")
        self.plan = self._load(check, "plan", "plan_path", "plan_sha256", validate_scene_plan, "scene_plan_invalid")
        preview = self.stages["preview"]["artifacts"]
        if "media_path" in preview:
            self._artifact(check, "preview", "media_path", "media_sha256")
        self.video_path = self._artifact(check, "preview", "preview_file", "video_sha256")
        manifest_path = self._artifact(check, "preview", "manifest_file", "manifest_sha256")
        self.manifest = None
        if manifest_path is not None:
            try:
                self.manifest = _json_bytes(self._snap("preview_manifest", preview["manifest_file"], manifest_path, keep=True))
                if not isinstance(self.manifest, dict) or self.manifest.get("contract") != "preview_render":
                    raise ValueError
                if self.manifest.get("version") == "1.1":
                    validate_manifest(self.manifest)       # malformed poster hashes are rejected here
            except (OSError, ValueError, UnicodeError, NetworkError, AttributeError, TypeError):
                check.fail("manifest_invalid")
                self.manifest = None
        if self.plan is not None and self.script is not None and self.plan["script"] != self.script:
            check.fail("plan_script_mismatch")
        if self.stages["validate"]["artifacts"].get("script_sha256") != self.stages["creator"]["artifacts"].get("script_sha256"):
            check.fail("validation_script_mismatch")
        check.details.update(brief=self.brief is not None, script=self.script is not None, plan=self.plan is not None,
                             video=self.video_path is not None, manifest=self.manifest is not None,
                             audio_recorded=bool(preview.get("audio_present")))
        return check

    def _record(self):
        """(policy, record, problem_code). Never raises."""
        if hasattr(self, "_record_cache"):
            return self._record_cache
        from .verification import load_policy
        from .verification_cli import load_record
        config = self.state["config"]
        result = (None, None, None)
        try:
            if self.snapshot.entries.get(("config", "verification_policy"), {}).get("sha256") != config["policy"]["sha256"]:
                result = (None, None, "verification_policy_changed")
            else:
                policy, policy_sha = load_policy(config["policy"]["path"])
                from .verification_cli import paths as verification_paths
                records, _ = verification_paths(self.root)
                found, _ = safe_file(records, config["record_id"] + ".json", MAX_JSON_BYTES)
                if found is not None:
                    self.snapshot.read(("record", config["record_id"]), found, role="verification_record",
                                       ref=None, ident=config["record_id"])
                record = load_record(config["record_id"], policy, policy_sha, self.root)
                result = (policy, record, None)
        except NetworkError as error:
            mapping = {"record_not_found": "evidence_record_unavailable", "policy_mismatch": "verification_policy_changed",
                       "invalid_verification_record": "evidence_record_invalid"}
            result = (None, None, mapping.get(error.code, "evidence_record_unavailable"))
        except OSError:
            result = (None, None, "verification_policy_unavailable")
        self._record_cache = result
        return result

    def _check_provenance(self):
        check = Check("provenance")
        if self.brief is None or self.script is None:
            check.skip("artifacts_unavailable")
            return check
        brief, script = self.brief, self.script
        if script["claims"] != brief["claims"] or script["sources"] != brief["sources"]:
            check.fail("script_claims_differ_from_brief")
        links = brief.get("verification")
        if links is None or links["record_ids"] != [self.state["config"]["record_id"]]:
            check.fail("brief_not_linked_to_record")
            return check
        if brief.get("editorial", {}).get("selection_run_id") != self.state["config"]["selection_run_id"]:
            check.fail("brief_not_from_selection")
        _, record, problem = self._record()
        check.details.update(claims=len(brief["claims"]), sources=len(brief["sources"]),
                             claims_unverified=count_unverified(brief))
        if record is None:
            (check.fail if problem == "evidence_record_invalid" else check.review)(problem)
            return check
        claims = {c["claim_id"]: c for c in record["claims"]}
        sources = {s["source_id"]: s for s in brief["sources"]}
        mapped = {link["claim_id"]: link for link in links["claims"]}
        for claim in brief["claims"]:
            link = mapped.get(claim["claim_id"])
            source = claims.get(link["record_claim_id"]) if link else None
            if source is None or source["text"] != claim["text"] or source["status"] != link["verification_status"]:
                check.fail("claim_not_matching_record")
                continue
            if (claim["status"] == "verified") != (source["status"] == "verified"):
                check.fail("claim_status_not_matching_record")
            backing = [e for e in source["evidence"] if e["relation"] == "supports" and "superseded_by" not in e
                       and (claim["status"] != "verified" or (e["tier"] == "primary" and e["first_hand"]))]
            urls = {e["url"] for e in backing}
            for source_id in claim["source_ids"]:
                if source_id not in sources or sources[source_id].get("url") not in urls:
                    check.fail("source_not_backed_by_evidence")
        return check

    def _check_evidence_freshness(self):
        check = Check("evidence_freshness")
        policy, record, problem = self._record()
        if record is None:
            check.skip(problem or "evidence_record_unavailable")
            return check
        age = _parse(self.clock()) - _parse(record["verified_at"])
        limit = policy["rules"]["max_record_age_days"]
        check.details.update(verified_at=record["verified_at"], age_hours=round(age.total_seconds() / 3600, 1),
                             max_age_days=limit)
        if age > timedelta(days=limit):
            check.review("evidence_stale")
        elif age < timedelta(minutes=-5):
            check.fail("evidence_from_future")
        return check

    def _check_draft_restrictions(self):
        check = Check("draft_restrictions")
        if self.script is None or self.plan is None or self.manifest is None:
            check.skip("artifacts_unavailable")
            return check
        unverified = count_unverified(self.script)
        draft = self.plan["blocked_for_production"]
        check.details.update(draft=draft, claims_unverified=unverified,
                             allow_draft_preview=self.state["config"]["allow_draft_preview"])
        if draft != (unverified > 0) or self.stages["validate"]["artifacts"].get("draft") != draft:
            check.fail("draft_flag_inconsistent")
        if self.manifest.get("source_blocked_for_production") != draft:
            check.fail("manifest_draft_flag_mismatch")
        if self.manifest.get("publishable") is not False or self.manifest.get("preview_only") is not True:
            check.fail("manifest_publishable_flags_invalid")
        if draft:
            check.review("draft_content")
            if not self.state["config"]["allow_draft_preview"]:
                check.fail("draft_rendered_without_consent")
        posters = [s.get("poster") for s in self.manifest.get("scenes", [])]
        colours_ok, available = True, True
        for poster in posters:
            path = self._path(self._poster_ref(poster))
            if path is None or not path.is_file():
                continue  # Missing posters are reported by manifest_consistency.
            try:
                _, colours = self.poster_reader(path)
            except ProbeUnavailable:
                available = False
                break
            except ProbeFailed:
                colours_ok = False
                continue
            expected = WATERMARK[draft]
            if not all(all(abs(a - b) <= 12 for a, b in zip(colour, expected)) for colour in colours):
                colours_ok = False
        check.details["watermark_checked"] = available
        if not available:
            check.skip("watermark_check_unavailable")
        elif not colours_ok:
            check.fail("watermark_missing_or_wrong")
        return check

    def _check_scene_timing(self):
        check = Check("scene_timing")
        if self.plan is None or self.manifest is None:
            check.skip("artifacts_unavailable")
            return check
        template = FORMATS[self.plan["script"]["format"]]["beats"]
        windows = [(s["beat"]["beat"], s["beat"]["start_seconds"], s["beat"]["end_seconds"]) for s in self.plan["scenes"]]
        check.details.update(scenes=len(windows), duration_seconds=self.plan["duration_seconds"])
        if windows != list(template):
            check.fail("plan_timing_not_format")
        if [s["index"] for s in self.plan["scenes"]] != list(range(1, len(windows) + 1)):
            check.fail("plan_scene_order_invalid")
        manifest_scenes = [(s.get("index"), s.get("method"), s.get("start_seconds"), s.get("end_seconds"))
                           for s in self.manifest.get("scenes", [])]
        planned = [(s["index"], s["selected_method"], s["beat"]["start_seconds"], s["beat"]["end_seconds"])
                   for s in self.plan["scenes"]]
        if manifest_scenes != planned:
            check.fail("manifest_scenes_not_matching_plan")
        if self.plan["duration_seconds"] != EXPECTED["duration"] or windows and windows[-1][2] != self.plan["duration_seconds"]:
            check.fail("plan_duration_invalid")
        return check

    def _measure(self):
        if hasattr(self, "_measure_cache"):
            return self._measure_cache
        if self.video_path is None:
            self._measure_cache = (None, "artifacts_unavailable", False)
        else:
            try:
                self._measure_cache = (self.prober(self.video_path), None, False)
            except ProbeUnavailable:
                self._measure_cache = (None, "media_probe_unavailable", False)
            except ProbeFailed as error:
                self._measure_cache = (None, str(error.args[0]) if error.args else "video_unreadable", True)
            except Exception:
                self._measure_cache = (None, "video_unreadable", True)
        return self._measure_cache

    def _check_video(self):
        check = Check("video")
        media, problem, broken = self._measure()
        if media is None:
            (check.fail if broken else check.skip)(problem)
            return check
        check.details.update({k: media[k] for k in ("video_codec", "width", "height", "fps", "duration", "frames")})
        if (media["width"], media["height"]) != (EXPECTED["width"], EXPECTED["height"]):
            check.fail("video_dimensions_wrong")
        if abs(media["fps"] - EXPECTED["fps"]) > 0.01:
            check.fail("video_fps_wrong")
        if abs(media["duration"] - EXPECTED["duration"]) > DURATION_TOLERANCE:
            check.fail("video_duration_wrong")
        if media["frames"] != EXPECTED["frames"]:
            check.fail("video_frame_count_wrong")
        return check

    def _check_audio(self):
        check = Check("audio")
        narration = self.state["config"]["narration"]
        expected = narration is not None
        check.details["narration_configured"] = expected
        if self.manifest is not None:
            if bool(self.manifest.get("audio_present")) != expected or bool(self.stages["preview"]["artifacts"].get("audio_present")) != expected:
                check.fail("manifest_audio_flag_mismatch")
        media, problem, broken = self._measure()
        if media is None:
            (check.fail if broken else check.skip)(problem)
        else:
            check.details.update(audio_present=media["audio_present"], audio_duration=media["audio_duration"])
            if media["audio_present"] != expected:
                check.fail("audio_stream_unexpected" if media["audio_present"] else "audio_stream_missing")
            elif expected and not (EXPECTED["duration"] - DURATION_TOLERANCE <= media["audio_duration"]
                                   <= EXPECTED["duration"] + AUDIO_MAX_EXTRA):
                check.fail("audio_duration_wrong")
        if expected and self.manifest is not None:
            from .narration import load_narration
            audio = self.manifest.get("audio") or {}
            path = Path(narration["path"])
            try:
                digest = self.snapshot.read(("narration", 0), path, role="narration", max_bytes=16 * 1024 * 1024)
                if digest is None:
                    raise OSError("narration unavailable")
                if digest != narration["sha256"]:
                    check.review("narration_source_changed")
                else:
                    metadata, _ = load_narration(path)
                    check.details["source_duration_seconds"] = metadata["source_duration_seconds"]
                    if metadata["normalized_sha256"] != audio.get("normalized_sha256") \
                            or metadata["source_duration_seconds"] != audio.get("source_duration_seconds"):
                        check.fail("narration_not_matching_manifest")
            except OSError:
                check.review("narration_source_unavailable")
            except NetworkError:
                check.fail("narration_source_invalid")
        return check

    def _check_manifest_consistency(self):
        check = Check("manifest_consistency")
        if self.manifest is None or self.plan is None or self.video_path is None:
            check.skip("artifacts_unavailable")
            return check
        m = self.manifest
        video_digest = self.snapshot.entries.get(("file", norm(self.stages["preview"]["artifacts"]["preview_file"])), {}).get("sha256")
        if m.get("video_sha256") != video_digest or m.get("video_sha256") != self.stages["preview"]["artifacts"].get("video_sha256"):
            check.fail("manifest_video_hash_mismatch")
        if m.get("plan_id") != self.plan["plan_id"] or m.get("input_sha256") != self.plan["input_sha256"]:
            check.fail("manifest_plan_mismatch")
        declared = (m.get("width"), m.get("height"), m.get("fps"), m.get("duration_seconds"))
        if declared != (EXPECTED["width"], EXPECTED["height"], EXPECTED["fps"], EXPECTED["duration"]):
            check.fail("manifest_format_wrong")
        media, _, _ = self._measure()
        if media is not None:
            if (media["width"], media["height"]) != (m.get("width"), m.get("height")) or abs(media["fps"] - (m.get("fps") or 0)) > 0.01 \
                    or abs(media["duration"] - (m.get("duration_seconds") or 0)) > DURATION_TOLERANCE:
                check.fail("manifest_not_matching_media")
            if media["audio_present"] != bool(m.get("audio_present")):
                check.fail("manifest_audio_not_matching_media")
        else:
            check.skip("media_measurements_unavailable")
        posters_ok, sized, bound = 0, True, posters_bound(m)
        for number, scene in enumerate(m.get("scenes", []), start=1):
            ref = self._poster_ref(scene.get("poster"))
            path = self._path(ref)
            if path is None or not path.is_file():
                check.fail("poster_missing")
                continue
            digest = self._snap("poster", ref, path, index=number, limit=MAX_POSTER_BYTES)
            entry = self.snapshot.entries.get(("file", norm(ref)))
            if digest is None:
                check.fail("poster_missing")
                continue
            if bound and (digest != scene.get("poster_sha256") or entry["bytes"] != scene.get("poster_bytes")):
                check.fail("poster_hash_mismatch")             # Step 36: manifest 1.1 binds every poster
            posters_ok += 1
            try:
                size, _ = self.poster_reader(path)
                if tuple(size) != (EXPECTED["width"], EXPECTED["height"]):
                    check.fail("poster_dimensions_wrong")
            except ProbeUnavailable:
                sized = False
            except ProbeFailed:
                check.fail("poster_unreadable")
        check.details.update(posters_found=posters_ok, posters_sized=sized, posters_hash_bound=bound,
                             media_measured=media is not None)
        if not sized:
            check.skip("poster_check_unavailable")
        return check

    def _check_history(self):
        check = Check("history")
        from .selection import load_profile
        from .selection_cli import load_history
        try:
            profile, _ = load_profile(self.state["config"]["editorial_profile"]["path"])
            history, _ = load_history(profile, self.root)
        except NetworkError:
            check.skip("history_unavailable")
            return check
        mine = [e for e in history["entries"] if e.get("production_id") == self.state["production_id"]]
        state = mine[0]["state"] if mine else None
        check.details["history_state"] = state
        if state is None:
            check.review("history_entry_missing")
        elif state != "produced":
            check.review("history_not_marked_produced")
        elif self.brief is not None and mine[0]["brief_id"] != self.brief["brief_id"]:
            check.fail("history_brief_mismatch")
        return check

    # -- report

    def _binding(self, changed):
        """What these checks read, and whether it stayed the same while they ran (Step 36)."""
        artifacts, configuration = self.snapshot.artifacts(), self.snapshot.configuration()
        reasons = []
        if changed:
            status = "changed_during_inspection"
            reasons = ["artifact_changed_during_inspection"]
            roles = {self.snapshot.entries[key]["role"] for key in changed}
            reasons += sorted("changed_" + role for role in roles)[:20]
        elif self.state is None or self.state["status"] != "completed" or any(
                row["status"] != "read" for row in artifacts + configuration):
            status = "incomplete"
            reasons = ["not_every_artifact_could_be_read"]
        else:
            expected = {"production_state", "brief", "script", "scene_plan", "preview_manifest", "video", "poster",
                        "verification_record"} | ({"narration"} if self.state["config"]["narration"] else set())
            missing = expected - {row["role"] for row in artifacts}
            status = "incomplete" if missing else "bound"
            reasons = sorted("not_read_" + role for role in missing)
        return {"binding_version": BINDING_VERSION, "status": status, "reasons": reasons[:30], "integrity_only": True,
                "artifacts": artifacts[:40], "configuration": configuration,
                "note": BINDING_NOTE}

    def _report(self, production_id, checks, binding):
        reasons = list(dict.fromkeys(code for check in checks for code in check["reasons"]))
        statuses = {check["status"] for check in checks}
        if "fail" in statuses:
            result = "fail"
        elif statuses & {"needs_review", "unavailable"}:
            result = "needs_review"
        else:
            result = "pass"
        if binding["status"] == "changed_during_inspection":
            reasons.append("artifacts_changed_during_inspection")   # a pass on moving files is not a pass
            result = "needs_review" if result == "pass" else result
        checked_at = self.clock()
        # A random component keeps every run's report separate (reports are never overwritten).
        digest = hashlib.sha256(json.dumps([production_id, checked_at, checks, uuid4().hex], sort_keys=True).encode()).hexdigest()
        report = {"contract": "production_quality_report", "version": "1.1", "report_id": "qr-" + digest[:24],
                  "production_id": production_id, "checked_at": checked_at, "result": result, "reasons": reasons,
                  "checks": checks,
                  "scope": {"technical_checks_only": True, "factual_accuracy_verified": False, "rights_cleared": False,
                            "publishable": False, "permission_to_publish": False},
                  "notes": list(NOTES), "binding": binding}
        validate_report(report)
        return report


def validate_report(report):
    reject_secrets(report)
    error = next(_validator().iter_errors(report), None)
    if error is not None:
        raise NetworkError("invalid_quality_report", f"Quality report rejected: schema rule '{error.validator}'.")


def load_report(report_id, root=None):
    """One saved report, validated, whose file name equals its report ID."""
    if not isinstance(report_id, str) or not re.fullmatch(r"qr-[0-9a-f]{24}", report_id):
        raise NetworkError("invalid_report_id", "Report IDs look like qr- followed by 24 hex characters.")
    folder = Path(root if root is not None else ROOT) / "runtime/quality"
    path, problem = safe_file(folder, report_id + ".json", MAX_JSON_BYTES)
    if path is None:
        raise NetworkError("quality_report_not_found" if problem == "missing" else "invalid_quality_report",
                           "No saved quality report with this ID.")
    try:
        report = read_json(path)
    except (OSError, ValueError, UnicodeError):
        raise NetworkError("invalid_quality_report", "The quality report is unreadable.") from None
    validate_report(report)
    if report["report_id"] != report_id:
        raise NetworkError("invalid_quality_report", "The report file name does not match its ID.")
    return report


def binding_of(report_id, root=None):
    """Step 36, read-only: a saved report's artifact binding re-checked against the current files."""
    from .artifact_binding import verify_binding
    report = load_report(report_id, root)
    store = ProductionStore(root)
    state = store.read(report["production_id"])
    binding = verify_binding(report, state, store.folder(report["production_id"]), root, PROJECT)
    freshness = next((c for c in report["checks"] if c["check_id"] == "evidence_freshness"), None)
    return {"report_id": report_id, "production_id": report["production_id"], "checked_at": report["checked_at"],
            "binding": binding, "technical_result": report["result"],
            "evidence_freshness_at_check": freshness["status"] if freshness else "unavailable",
            "scope": report["scope"],
            "note": "Binding compares file hashes only; it re-runs no checks and proves nothing about facts or rights."}


def list_reports(root=None):
    folder = Path(root if root is not None else ROOT) / "runtime/quality"
    rows = []
    if folder.is_dir():
        for path in sorted(folder.glob("qr-*.json")):
            try:
                report = read_json(path)
                validate_report(report)
                rows.append({"report_id": report["report_id"], "production_id": report["production_id"],
                             "checked_at": report["checked_at"], "result": report["result"],
                             "reasons": report["reasons"][:10]})
            except (NetworkError, OSError, ValueError, UnicodeError, KeyError, TypeError):
                rows.append({"report_id": path.stem[:40], "error": "invalid_quality_report"})
    return sorted(rows, key=lambda r: r.get("checked_at", ""), reverse=True)
