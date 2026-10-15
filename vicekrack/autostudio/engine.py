"""Auto Studio engine: prepare (free) -> approve (one consent) -> every step runs on its own.

State lives in runtime/auto-studio/<asp-...>/state.json (plus brief.json, script.json, timeline.json,
voices/, clips/, work/, final.mp4, thumb.jpg). The background worker calls advance() until the video is
finished, failed or stopped; it survives restarts because every step resumes from what is saved.

Money rules:
- the approval names one production and a spending cap (the estimate plus room for retries);
- before every paid request the engine checks that it still fits under the cap; costs are recorded as
  they happen (requests whose outcome is unknown are counted as spent);
- a failed scene is retried automatically at most max_scene_retries times, still under the cap.
"""

import json
import math
import shutil
import time
from copy import deepcopy
from pathlib import Path

from .. import xai_responses
from ..errors import NetworkError
from . import assemble, clips as clip_api, music, planner, script as script_mod, verify, voice
from .common import (ACTIVE, CONTRACT, FINAL, STEPS, VERSION, add_cost, base, can_spend, canonical, folder,
                     list_ids, load_config, read_state, require_budget, save_state, sha256_bytes, tts_cost, utc_now,
                     video_price, write_json)

MAX_REWRITES = 2


class Providers:
    """Where paid work goes. Real by default; the demo and tests pass offline stand-ins."""

    def __init__(self, *, writer=None, tts=None, video=None, demo=False):
        self.writer, self.tts, self.video, self.demo = writer, tts, video, demo


def production_id_for(record_id, settings):
    return "asp-" + sha256_bytes(canonical({"record": record_id, "settings": settings}))[:24]


def normalize_settings(config, raw):
    try:
        settings = {"length": int(raw.get("length", 30)), "type": str(raw.get("type", "news")),
                    "audio": str(raw.get("audio", "narrator")), "music": bool(raw.get("music", True)),
                    "model": str(raw.get("model") or config["video"]["model"]),
                    "resolution": str(raw.get("resolution") or config["video"]["resolution"]),
                    "native_audio": bool(config["video"]["native_audio"])}
    except (TypeError, ValueError, AttributeError):
        raise NetworkError("invalid_auto_settings", "Those video settings are not valid.") from None
    if settings["length"] not in config["lengths"] or settings["type"] not in config["types"] \
            or settings["audio"] not in config["audio_styles"] or settings["model"] not in config["video"]["models"] \
            or settings["resolution"] not in config["video"]["resolutions"]:
        raise NetworkError("invalid_auto_settings", "Those video settings are not offered.")
    video_price(config, settings["model"], settings["resolution"])
    return settings


# ---------------------------------------------------------------- prepare / approve / stop / retry

def brief_for_card(desk, job_id, story_index, clock):
    """The checked Story Brief of one research card (free, local), exactly as Research's Make video does."""
    from ..production import load_configs
    from ..selection import prune_history, select_brief
    from ..selection_cli import load_history, load_report, select_stories
    from ..research_desk import load_config as research_config
    from ..verification_cli import load_record
    job = desk.read(job_id)
    cards = job.get("cards") or []
    if not isinstance(story_index, int) or not 0 <= story_index < len(cards):
        raise NetworkError("research_story_not_found", "No such story card.")
    card, offer = cards[story_index], cards[story_index].get("make_video")
    if not offer:
        raise NetworkError("story_not_makeable", "This story did not pass the checks, so it cannot be made.")
    config, _ = research_config()
    paths = {k: v for k, v in config["pipeline"].items() if k != "pick_profile"}
    if offer.get("pick"):
        paths["editorial_profile"] = config["pipeline"]["pick_profile"]
    fresh, _ = select_stories([offer["record_id"]], paths["policy"], paths["editorial_profile"], root=desk.root,
                              clock=clock)
    entry = fresh["entries"][0]
    if entry["disposition"] != "select":
        raise NetworkError("story_changed", "This story can't be made any more; research it again.")
    loaded = load_configs({**paths, "capabilities": "config/visual-capabilities.json"})
    record = load_record(offer["record_id"], loaded["policy"], loaded["policy_sha"], desk.root)
    report = load_report(fresh["selection_run_id"], desk.root)
    now = clock()
    history, _ = load_history(loaded["profile"], desk.root)
    brief, _ = select_brief(record, report, profile=loaded["profile"], profile_sha256=loaded["profile_sha"],
                            policy=loaded["policy"], policy_sha256=loaded["policy_sha"],
                            history=prune_history(history, loaded["profile"], now), now=now)
    return brief, card, offer


def prepare(root, desk, job_id, story_index, raw_settings, *, demo=False, clock=utc_now):
    config = load_config()
    settings = normalize_settings(config, raw_settings or {})
    brief, card, offer = brief_for_card(desk, job_id, story_index, clock)
    production_id = production_id_for(offer["record_id"], settings)
    path = folder(production_id, root) / "state.json"
    if path.exists():
        state = read_state(production_id, root)
        if state["status"] != "prepared":
            return state
    estimate = planner.estimate(config, settings["length"], settings["model"], settings["resolution"])
    now = clock()
    rumor = (brief.get("editorial") or {}).get("lane") == "rumor"
    state = {
        "contract": CONTRACT, "version": VERSION, "production_id": production_id, "demo": bool(demo),
        "created_at": now, "updated_at": now, "status": "prepared",
        "source": {"research_job": job_id, "story": story_index, "record_id": offer["record_id"],
                   "topic": card["topic"], "headline": card["headline"], "label": card["label"]},
        "settings": settings, "title": card["headline"][:90], "rumor": rumor,
        "estimate": estimate, "consent_phrase": f"paid-auto:{production_id}", "approved_at": None,
        "steps": [{"name": n, "status": "pending", "started_at": None, "finished_at": None, "note": None} for n in STEPS],
        "cost": {"spent_usd": 0.0, "items": []}, "rewrites": 0,
        "scenes": [], "final": None, "errors": [], "log": [],
    }
    write_json(folder(production_id, root) / "brief.json", brief)
    return save_state(state, root, clock)


def approve(root, production_id, consent, *, clock=utc_now):
    state = read_state(production_id, root)
    if consent != state["consent_phrase"]:
        raise NetworkError("auto_consent_required", "Tick the approval for this exact video and its spending cap.")
    if state["status"] != "prepared":
        raise NetworkError("auto_already_approved", "This video was already approved.")
    state["status"], state["approved_at"] = "approved", clock()
    _log(state, "Approved. Spending cap ${:.2f}.".format(state["estimate"]["cap_usd"]), clock)
    return save_state(state, root, clock)


def stop(root, production_id, *, clock=utc_now):
    state = read_state(production_id, root)
    if state["status"] in ACTIVE:
        state["status"] = "stopped"
        _log(state, "Stopped by you. No new paid requests will be sent.", clock)
        save_state(state, root, clock)
    return state


def retry(root, production_id, consent, *, clock=utc_now):
    """Start a failed or stopped video again from where it stopped. The cap grows by one more estimate."""
    state = read_state(production_id, root)
    if state["status"] not in ("failed", "stopped"):
        raise NetworkError("auto_not_retryable", "Only a failed or stopped video can be retried.")
    expected = f"paid-auto-retry:{production_id}:{len(state.get('retries', [])) + 1}"
    if consent != expected:
        raise NetworkError("auto_consent_required", "Tick the approval for this retry and its extra spending.")
    state.setdefault("retries", []).append({"at": clock(), "added_usd": state["estimate"]["expected_usd"]})
    state["estimate"]["cap_usd"] = round(state["estimate"]["cap_usd"] + state["estimate"]["expected_usd"], 2)
    for step in state["steps"]:
        if step["status"] in ("failed", "running"):
            step["status"] = "pending"
    for scene in state["scenes"]:
        if scene["status"] == "failed":
            scene["status"], scene["retries_left"] = "waiting", 1
    state["status"] = "approved"
    _log(state, "Retry approved. Spending cap now ${:.2f}.".format(state["estimate"]["cap_usd"]), clock)
    return save_state(state, root, clock)


def retry_consent(state):
    return f"paid-auto-retry:{state['production_id']}:{len(state.get('retries', [])) + 1}"


def _log(state, text, clock):
    state["log"] = (state["log"] + [{"at": clock(), "text": text[:300]}])[-200:]


def _step(state, name):
    return next(s for s in state["steps"] if s["name"] == name)


def _fail(state, step, error, clock):
    step["status"], step["finished_at"], step["note"] = "failed", clock(), error.message[:300]
    state["status"] = "failed"
    state["errors"] = (state["errors"] + [{"at": clock(), "step": step["name"], "code": error.code,
                                           "message": error.message[:300]}])[-50:]
    _log(state, f"{step['name'].capitalize()} failed: {error.message}", clock)


# ---------------------------------------------------------------- the engine

class Engine:
    def __init__(self, root=None, *, providers=None, clock=utc_now, sleep=time.sleep):
        self.root, self.providers, self.clock, self.sleep = root, providers or Providers(), clock, sleep
        self.config = load_config()

    def _save(self, state):
        """Save, but never undo a Stop you pressed while a step was running."""
        try:
            if read_state(state["production_id"], self.root)["status"] == "stopped" and state["status"] != "stopped":
                state["status"] = "stopped"
                _log(state, "Stopped while a step was running; nothing new will be sent.", self.clock)
        except NetworkError:
            pass
        return save_state(state, self.root, self.clock)

    def path(self, production_id):
        return folder(production_id, self.root)

    def active(self):
        out = []
        for pid in list_ids(self.root)[: self.config["limits"]["max_productions_listed"]]:
            try:
                if read_state(pid, self.root)["status"] in ACTIVE:
                    out.append(pid)
            except NetworkError:
                continue
        return out

    def advance(self, production_id):
        """Run steps until the video is done or must wait. Returns seconds to wait, or None when finished."""
        state = read_state(production_id, self.root)
        if state["status"] not in ACTIVE:
            return None
        for name in STEPS:
            step = _step(state, name)
            if step["status"] == "done":
                continue
            if read_state(production_id, self.root)["status"] == "stopped" or state["status"] == "stopped":
                return None
            if step["status"] != "running":
                step["status"], step["started_at"] = "running", self.clock()
                state["status"] = {"script": "scripting", "voices": "voicing", "timing": "planning",
                                   "scenes": "generating", "music": "assembling", "assembly": "assembling",
                                   "verify": "verifying"}[name]
                self._save(state)
            try:
                wait = getattr(self, "_" + name)(state)
            except NetworkError as error:
                _fail(state, step, error, self.clock)
                self._save(state)
                return None
            if state["status"] == "stopped":
                step["status"] = "pending" if step["status"] == "running" else step["status"]
                self._save(state)
                return None
            if wait:
                self._save(state)
                if state["status"] == "stopped":
                    return None
                return wait
            if step["status"] == "running":
                step["status"], step["finished_at"] = "done", self.clock()
            self._save(state)
            if step["status"] == "pending":            # a later step sent us back (for example a rewrite)
                return 0.01
        state["status"] = "completed"
        _log(state, "Finished and verified.", self.clock)
        self._save(state)
        return None

    def run_until_done(self, production_id, max_seconds=3600):
        """Tests and the demo: drive one video to the end in this thread."""
        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            wait = self.advance(production_id)
            if wait is None:
                return read_state(production_id, self.root)
            self.sleep(min(wait, 30))
        raise NetworkError("auto_timeout", "The video did not finish in time.")

    # -- steps ------------------------------------------------------------------------------------------

    def _brief(self, state):
        return json.loads((self.path(state["production_id"]) / "brief.json").read_text(encoding="utf-8"))

    def _script(self, state):
        folder_ = self.path(state["production_id"])
        if (folder_ / "script.json").exists():
            return 0
        brief, settings = self._brief(state), state["settings"]
        shrink = 0.85 ** state["rewrites"]
        plan = planner.budget(self.config, settings["length"], shrink=shrink)
        last = None
        for attempt in range(2):
            if self.providers.writer == "mock":
                raw, usage = script_mod.mock_write(self.config, brief, settings, plan)
                cost = 0.0
            else:
                require_budget(state, self.config["prices"]["script_estimate_usd"] * 3, "writing the script")
                try:
                    raw, usage = script_mod.write(self.config, brief, settings, plan, transport=self.providers.writer)
                except NetworkError as error:
                    if error.code in ("xai_rejected", "xai_not_sent", "missing_xai_credential"):
                        raise
                    add_cost(state, "script", "Script request (outcome unclear)", self.config["prices"]["script_estimate_usd"],
                             certain=False, clock=self.clock)
                    last = error
                    continue
                cost = xai_responses.cost_usd(usage, _research_prices())
                add_cost(state, "script", f"Grok script ({usage['output_tokens']} tokens)", cost, clock=self.clock)
            try:
                checked = script_mod.validate(raw, self.config, brief, settings, plan)
            except NetworkError as error:
                last = error
                _log(state, f"Script attempt {attempt + 1} was not usable: {error.message}", self.clock)
                continue
            checked["voices"] = voice.assign_voices(self.config, checked["speakers"])
            write_json(folder_ / "script.json", checked)
            state["title"] = checked["title"]
            state["scenes"] = []
            _log(state, f"Script ready: {len(checked['scenes'])} scenes, {checked['word_count']} words.", self.clock)
            return 0
        raise last or NetworkError("invalid_script", "The script could not be written.")

    def _load_script(self, state):
        return json.loads((self.path(state["production_id"]) / "script.json").read_text(encoding="utf-8"))

    def _voices(self, state):
        folder_ = self.path(state["production_id"])
        sc = self._load_script(state)
        changed = False
        for si, scene in enumerate(sc["scenes"]):
            for li, line in enumerate(scene["lines"]):
                target = folder_ / "voices" / f"s{si:02d}-l{li:02d}.wav"
                if line.get("timing") and target.exists() and sha256_bytes(target.read_bytes()) == line["timing"]["sha256"]:
                    continue
                cost = tts_cost(self.config, len(line["text"]))
                for attempt in range(3):
                    require_budget(state, cost, "making a voice line")
                    try:
                        result = voice.speak(line["text"], sc["voices"][line["speaker"]], target,
                                             transport=self.providers.tts)
                    except NetworkError as error:
                        if error.code in ("speech_rejected", "speech_not_sent", "missing_speech_credential"):
                            raise
                        add_cost(state, "voice", f"Voice line (outcome unclear) scene {si + 1}", cost, certain=False,
                                 clock=self.clock)
                        if attempt == 2:
                            raise
                        continue
                    add_cost(state, "voice", f"Voice: scene {si + 1}, line {li + 1}", cost, clock=self.clock)
                    line["timing"] = {"seconds": result["seconds"], "words": result["words"]["words"],
                                      "method": result["words"]["method"], "sha256": result["sha256"]}
                    changed = True
                    break
                write_json(folder_ / "script.json", sc)
                self._save(state)
        if changed:
            _log(state, "All voice lines are ready.", self.clock)
        return 0

    def _timing(self, state):
        folder_ = self.path(state["production_id"])
        sc = self._load_script(state)
        tl = planner.timeline(self.config, state["settings"]["length"],
                              [[line["timing"]["seconds"] for line in scene["lines"]] for scene in sc["scenes"]])
        if not tl["within_tolerance"] and tl["total_seconds"] > tl["target_seconds"]:
            if state["rewrites"] >= MAX_REWRITES:
                raise NetworkError("too_long", f"The spoken script runs {tl['total_seconds']:.0f} s for a "
                                   f"{tl['target_seconds']} s video, even after shortening it.")
            state["rewrites"] += 1
            _log(state, f"The voiceover ran {tl['total_seconds']:.0f} s; rewriting the script shorter.", self.clock)
            (folder_ / "script.json").unlink()
            shutil.rmtree(folder_ / "voices", ignore_errors=True)
            for name in ("script", "voices", "timing"):
                _step(state, name).update(status="pending", started_at=None, finished_at=None)
            return 0
        write_json(folder_ / "timeline.json", tl)
        settings = state["settings"]
        state["scenes"] = [{"index": i, "scene": seg["scene"], "duration": seg["duration"], "status": "waiting",
                            "retries_left": self.config["limits"]["max_scene_retries"], "attempts": [],
                            "request_id": None, "submitted_at": None, "clip": None,
                            "prompt": clip_api.request_for(seg, sc["scenes"][seg["scene"]], settings,
                                                           1 + sum(1 for s in tl["segments"][:i] if s["scene"] == seg["scene"]),
                                                           sum(1 for s in tl["segments"] if s["scene"] == seg["scene"]))["prompt"]}
                           for i, seg in enumerate(tl["segments"])]
        _log(state, f"Timing set: {len(tl['segments'])} clips, {tl['total_seconds']:.1f} s in total.", self.clock)
        return 0

    def _scenes(self, state):
        folder_ = self.path(state["production_id"])
        tl = json.loads((folder_ / "timeline.json").read_text(encoding="utf-8"))
        sc = self._load_script(state)
        settings = state["settings"]
        price = video_price(self.config, settings["model"], settings["resolution"])
        limits = self.config["limits"]
        pending = False
        for scene in state["scenes"]:
            seg = tl["segments"][scene["index"]]
            if scene["status"] == "done" and scene["clip"] and (folder_ / scene["clip"]["file"]).exists():
                continue
            if scene["status"] == "waiting":
                if state["status"] == "stopped":
                    return None
                cost = seg["duration"] * price
                require_budget(state, cost, f"making clip {scene['index'] + 1}")
                parts = sum(1 for s in tl["segments"] if s["scene"] == seg["scene"])
                part = 1 + sum(1 for s in tl["segments"][:scene["index"]] if s["scene"] == seg["scene"])
                request = clip_api.request_for(seg, sc["scenes"][seg["scene"]], settings, part, parts)
                scene["status"], scene["submitted_at"] = "submitting", self.clock()
                self._save(state)
                try:
                    scene["request_id"] = clip_api.submit(request, transport=self.providers.video)
                except NetworkError as error:
                    if error.code == "missing_video_credential":
                        scene["status"] = "waiting"
                        raise
                    add_cost(state, "clip", f"Clip {scene['index'] + 1} (outcome unclear)", cost, certain=False,
                             clock=self.clock)
                    scene["attempts"].append({"at": self.clock(), "outcome": "uncertain"})
                    self._scene_retry(state, scene, "the request failed")
                    pending = True
                    continue
                add_cost(state, "clip", f"Clip {scene['index'] + 1}: {seg['duration']} s", cost, clock=self.clock)
                scene["attempts"].append({"at": self.clock(), "outcome": "submitted", "request_id": scene["request_id"]})
                scene["status"] = "rendering"
                _log(state, f"Clip {scene['index'] + 1} sent to xAI ({seg['duration']} s).", self.clock)
                pending = True
                continue
            if scene["status"] == "submitting":          # interrupted while sending: treat as unclear
                add_cost(state, "clip", f"Clip {scene['index'] + 1} (interrupted, maybe billed)", seg["duration"] * price,
                         certain=False, clock=self.clock)
                scene["attempts"].append({"at": self.clock(), "outcome": "interrupted"})
                self._scene_retry(state, scene, "sending was interrupted")
                pending = True
                continue
            if scene["status"] == "rendering":
                try:
                    status, url = clip_api.status(scene["request_id"], transport=self.providers.video)
                except NetworkError:
                    pending = True
                    continue
                if status == "pending":
                    waited = (_epoch(self.clock()) - _epoch(scene["submitted_at"])) / 60
                    if waited > limits["max_wait_minutes_per_scene"]:
                        self._scene_retry(state, scene, "xAI took too long")
                    pending = True
                    continue
                if status in ("failed", "expired"):
                    scene["attempts"][-1]["outcome"] = status
                    self._scene_retry(state, scene, f"xAI reported the clip {status}")
                    pending = True
                    continue
                name = f"clips/clip-{scene['index']:03d}.mp4"
                try:
                    info = clip_api.download(url, folder_ / name, seg["duration"], transport=self.providers.video)
                except NetworkError as error:
                    if error.code == "clip_invalid":
                        self._scene_retry(state, scene, "the clip had the wrong length or shape")
                    pending = True
                    continue
                scene["clip"] = {"file": name, **info}
                scene["status"] = "done"
                _log(state, f"Clip {scene['index'] + 1} downloaded and checked.", self.clock)
                continue
            if scene["status"] == "failed":
                raise NetworkError("scene_failed", f"Clip {scene['index'] + 1} failed after its retries.")
        if any(s["status"] == "failed" for s in state["scenes"]):
            failed = next(s for s in state["scenes"] if s["status"] == "failed")
            raise NetworkError("scene_failed", f"Clip {failed['index'] + 1} failed after its retries.")
        if pending or any(s["status"] != "done" for s in state["scenes"]):
            return limits["poll_seconds"]
        return 0

    def _scene_retry(self, state, scene, why):
        if scene["retries_left"] > 0:
            scene["retries_left"] -= 1
            scene["status"], scene["request_id"] = "waiting", None
            _log(state, f"Clip {scene['index'] + 1}: {why}; trying again automatically.", self.clock)
        else:
            scene["status"] = "failed"
            _log(state, f"Clip {scene['index'] + 1}: {why}; no retries left.", self.clock)

    def _music(self, state):
        if not state["settings"]["music"]:
            _step(state, "music")["note"] = "Music off."
            return 0
        folder_ = self.path(state["production_id"])
        tl = json.loads((folder_ / "timeline.json").read_text(encoding="utf-8"))
        target = folder_ / "music.wav"
        if not target.exists():
            sc = self._load_script(state)
            music.compose(sc["music_mood"], tl["total_seconds"] + 0.5, target, folder_)
            _step(state, "music")["note"] = f"Original {sc['music_mood']} bed composed by ViceKrack."
        return 0

    def _assembly(self, state):
        folder_ = self.path(state["production_id"])
        if (folder_ / "final.mp4").exists() and state.get("final"):
            return 0
        tl = json.loads((folder_ / "timeline.json").read_text(encoding="utf-8"))
        sc = self._load_script(state)
        brief = self._brief(state)
        work = folder_ / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        voices = {(si, li): folder_ / "voices" / f"s{si:02d}-l{li:02d}.wav"
                  for si, scene in enumerate(sc["scenes"]) for li in range(len(scene["lines"]))}
        clips = {s["index"]: folder_ / s["clip"]["file"] for s in state["scenes"]}
        band = None
        if state["rumor"]:
            from ..rumor import BANDS
            band = BANDS.get((brief.get("editorial") or {}).get("rumor_label"), "UNCONFIRMED")
        publishers = list(dict.fromkeys(s["publisher"] for s in brief["sources"]))[:4]
        lines = ["Reported by: " + ", ".join(publishers)] if publishers else []
        if state["rumor"]:
            lines.append("Not officially confirmed.")
        lines += ["AI-generated visuals. Not real footage.", "Voices: AI text to speech."]
        if state["settings"]["music"]:
            lines.append("Music: original, made by ViceKrack.")
        kind = self.config["types"][state["settings"]["type"]]["label"].upper()
        assemble.assemble(work, timeline=tl, script=sc, clips=clips, voices=voices,
                          music=folder_ / "music.wav" if state["settings"]["music"] else None, title=sc["title"],
                          subtitle=("DEMO - " if state["demo"] else "") + kind, band=band, sources_lines=lines,
                          mix=self.config["mix"], speaker_labels=state["settings"]["audio"] != "narrator")
        for name in ("final.mp4", "thumb.jpg", "captions.json"):
            (work / name).replace(folder_ / name)
        (folder_ / "scenes").mkdir(exist_ok=True)
        for i in range(len(tl["segments"])):
            (work / f"scene-{i:03d}.jpg").replace(folder_ / "scenes" / f"scene-{i:03d}.jpg")
        data = (folder_ / "final.mp4").read_bytes()
        state["final"] = {"file": "final.mp4", "sha256": sha256_bytes(data), "bytes": len(data),
                          "duration": tl["total_seconds"], "verified": False, "checks": []}
        shutil.rmtree(work, ignore_errors=True)
        _log(state, "Assembled into one MP4.", self.clock)
        return 0

    def _verify(self, state):
        folder_ = self.path(state["production_id"])
        tl = json.loads((folder_ / "timeline.json").read_text(encoding="utf-8"))
        clips = {s["index"]: folder_ / s["clip"]["file"] for s in state["scenes"]}
        result = verify.verify(folder_ / "final.mp4", timeline=tl, clips=clips,
                               tolerance=self.config["timing"]["length_tolerance"], music=state["settings"]["music"])
        state["final"].update(verified=result["passed"], checks=result["checks"], facts=result["facts"],
                              duration=result["facts"]["duration"])
        if not result["passed"]:
            failed = [c["check"] for c in result["checks"] if not c["passed"]]
            raise NetworkError("verification_failed", "The finished file failed: " + ", ".join(failed))
        return 0


def _epoch(stamp):
    from datetime import datetime
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").timestamp() if stamp else time.time()


def _research_prices():
    from ..research_desk import load_config as research_config
    return research_config()[0]["prices"]


def summary(state):
    """What the library shows for one video (no internal paths)."""
    out = deepcopy({k: state[k] for k in ("production_id", "status", "title", "created_at", "updated_at", "demo",
                                          "settings", "estimate", "cost", "steps", "errors", "rumor", "source")})
    out["log"] = state["log"][-40:]
    out["final"] = None if not state.get("final") else {k: state["final"].get(k) for k in
                                                         ("duration", "verified", "checks", "bytes", "facts")}
    out["scenes"] = [{k: s.get(k) for k in ("index", "scene", "duration", "status", "retries_left")}
                     | {"has_still": False, "attempts": len(s.get("attempts", []))} for s in state["scenes"]]
    out["consent_phrase"] = state["consent_phrase"] if state["status"] == "prepared" else None
    out["retry_consent"] = retry_consent(state) if state["status"] in ("failed", "stopped") else None
    return out
