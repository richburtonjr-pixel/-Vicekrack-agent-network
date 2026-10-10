"""Step 46: Grok-generated narration jobs (xAI text to speech). One explicit paid request per job.

  prepare   offline: the spoken text is built deterministically from a validated ShortScript's
            narration beats, in order, words unchanged (no production directions, no speech tags).
            The job is bound to the script's SHA-256 and the chosen stock voice and output settings.
            The same script and settings always map to the same job, so it cannot be paid twice.
  inspect   read-only: the exact text, voice, settings, consent phrase and state.
  submit    the ONLY paid step. Needs --consent paid-speech:JOB_ID and --allow-network. The intent is
            saved (status "submitting") BEFORE the request is sent. One request, never retried
            automatically. A timeout or unclear failure leaves the job "uncertain"; another attempt
            needs --retry-uncertain --acknowledge-duplicate-billing.
  recover   offline: an interrupted submission becomes "uncertain" (never resent); provider audio
            that was received but not yet converted is converted locally (no request).

The endpoint is synchronous and documents no request ID, so none is recorded and nothing is polled.

Returned audio is kept as received (provider-audio.wav or .mp3, hashed, never exported) and
converted locally into the existing narration format: a canonical, metadata-free 16-bit PCM WAV
(narration.wav). Malformed or all-silent audio is rejected ("invalid_audio"). Speech longer than
15 seconds is kept but marked "too_long" and can never be used: it is not truncated, not sped up,
not rewritten, and no new request is made. A different text or setting is a different job that
needs its own review and consent.

Storage: runtime/speech-jobs/<job_id>/ (ignored by Git), schema-validated JSON written atomically,
one OS lock for every speech operation. No key, header, raw provider error or environment value is
stored or printed.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import NetworkError
from .events.store import _Lock
from .narration import MAX_SECONDS, canonical_wav, normalize_narration, parse_wav
from .orchestrator import ROOT
from .persistence import reject_secrets
from .speech_transport import SpeechRejected, transport as http_transport

CONTRACT, VERSION = "grok_speech_job", "1.0"
CONFIG_PATH = "config/speech.json"
JOB_ID = re.compile(r"^sp-[0-9a-f]{24}$")
MANAGED = "narration.wav"
MAX_SCRIPT_BYTES = 256 * 1024
NOTICE = ("AI-generated narration (xAI text to speech, stock voice). It reads the script's narration "
          "beats unchanged. Not a recording of a real person; publishable stays false.")
STATUSES = ("prepared", "submitting", "uncertain", "rejected", "received", "completed", "too_long", "invalid_audio")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@lru_cache(maxsize=None)
def _validator():
    schema = json.loads((ROOT / "schemas/grok-speech-job.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def load_config(root=ROOT):
    try:
        config = json.loads((Path(root) / CONFIG_PATH).read_text(encoding="utf-8"))
        defaults, limits = config["defaults"], config["limits"]
        if (config.get("config_version") != "1.0" or defaults["voice"] not in config["voices"]
                or not set(config["codecs"]) <= {"wav", "mp3"}
                or not all(isinstance(limits[k], int) and limits[k] > 0 for k in
                           ("max_text_characters", "request_timeout_seconds", "max_response_bytes", "max_attempts"))):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise NetworkError("invalid_speech_config", "config/speech.json is invalid.") from None
    return config


# ---------------------------------------------------------------- the spoken text

def script_digest(script):
    """The binding hash: canonical JSON of the validated script (independent of file formatting)."""
    return sha256_bytes(canonical(script))


def narration_text(script):
    """The narration beats in order, words unchanged (whitespace inside a beat collapsed), one beat per
    line, so the voice can pause between beats without any word or punctuation being added. Nothing else
    is spoken: no titles, on-screen text, visuals, sound cues or claim IDs."""
    from .short_script import validate_short_script
    validate_short_script(script)
    parts = [" ".join(beat["narration"].split()) for beat in script["beats"]]
    text = "\n".join(part for part in parts if part)
    if not text:
        raise NetworkError("speech_text_empty", "The script has no narration to speak.")
    if re.search(r"[\[\]<>]", text):
        # xAI reads [tags] and <tags> as speech directions; a script must not smuggle them in.
        raise NetworkError("speech_text_has_markup", "Narration contains [ ] or < > characters, which the speech "
                           "API treats as speech directions. Edit the script; it is never rewritten automatically.")
    return text


def read_script_file(path):
    try:
        with open(Path(path), "rb") as stream:
            data = stream.read(MAX_SCRIPT_BYTES + 1)
    except FileNotFoundError:
        raise NetworkError("script_not_found", "Script file was not found.") from None
    except (OSError, ValueError, TypeError):
        raise NetworkError("script_unreadable", "Script must be a readable local JSON file.") from None
    if len(data) > MAX_SCRIPT_BYTES:
        raise NetworkError("script_too_large", "Script file is too large.")
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise NetworkError("script_unreadable", "Script must be a readable local JSON file.") from None


def production_script(production_id, root=None):
    """A completed production's saved script, after the production verifies its own artifacts."""
    from .production import Pipeline, ProductionStore, _check_saved_config
    store = ProductionStore(root)
    pipeline = Pipeline(root=root)
    with store.lock(production_id):
        state = store.read(production_id)
        if state["status"] != "completed":
            raise NetworkError("production_incomplete", "The production is not completed.")
        pipeline._verify_artifacts(state, _check_saved_config(state))
        return pipeline._load_json(state, "creator", "script_path")


# ---------------------------------------------------------------- storage

def base(root=None):
    return Path(root if root is not None else ROOT) / "runtime" / "speech-jobs"


def folder(job_id, root=None):
    if not isinstance(job_id, str) or not JOB_ID.match(job_id):
        raise NetworkError("invalid_speech_job_id", "Speech job IDs look like sp- followed by 24 hex characters.")
    return base(root) / job_id


class _Locked:
    def __init__(self, root):
        self.path = base(root)

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = _Lock(self.path / "operation.lock")
        if not self.lock.acquire(create=True):
            raise NetworkError("speech_busy", "Another speech command is running; nothing was done.")
        return self

    def __exit__(self, *exc):
        self.lock.release()
        return False


def _validate(record):
    try:
        reject_secrets(record)
    except NetworkError:
        raise NetworkError("speech_job_corrupt", "The speech job contains credential-like data.") from None
    if not isinstance(record, dict) or next(_validator().iter_errors(record), None) is not None:
        raise NetworkError("speech_job_corrupt", "The speech job does not match its contract.")
    return record


def _write_atomic(target, data, *, create=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary, target)               # never overwrites
        else:
            os.replace(temporary, target)
            temporary = None
    except FileExistsError:
        raise NetworkError("speech_file_exists", "A speech job file already exists; it was not overwritten.") from None
    except OSError:
        raise NetworkError("speech_storage_failed", "Could not save the speech job.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _save(record, root, clock, *, create=False):
    record["updated_at"] = clock()
    _validate(record)
    _write_atomic(folder(record["job_id"], root) / "job.json",
                  json.dumps(record, indent=1, ensure_ascii=False).encode() + b"\n", create=create)
    return record


def _load(job_id, root=None):
    path = folder(job_id, root) / "job.json"
    if not path.is_file():
        raise NetworkError("speech_job_not_found", "No speech job with this ID.")
    try:
        if path.is_symlink() or path.stat().st_size > 512 * 1024:
            raise ValueError
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise NetworkError("speech_job_corrupt", "The speech job is unreadable.") from None
    _validate(record)
    if record["job_id"] != job_id:
        raise NetworkError("speech_job_corrupt", "The speech job belongs to another ID.")
    return record


# ---------------------------------------------------------------- prepare / inspect / list

def settings_for(config, voice=None, language=None, codec=None, sample_rate=None, bit_rate=None):
    defaults = config["defaults"]
    voice = (voice or defaults["voice"]).strip().lower()
    language = language or defaults["language"]
    codec = codec or defaults["codec"]
    sample_rate = sample_rate if sample_rate is not None else defaults["sample_rate"]
    if voice not in config["voices"]:
        raise NetworkError("invalid_speech_settings", "Voice must be one of the stock voices in config/speech.json.")
    if language not in config["languages"]:
        raise NetworkError("invalid_speech_settings", "Language is not listed in config/speech.json.")
    if codec not in config["codecs"]:
        raise NetworkError("invalid_speech_settings", "Codec must be wav or mp3.")
    if type(sample_rate) is not int or sample_rate not in config["sample_rates"]:
        raise NetworkError("invalid_speech_settings", "Sample rate is not a documented xAI value.")
    if codec == "mp3":
        bit_rate = bit_rate if bit_rate is not None else defaults["bit_rate"]
        if type(bit_rate) is not int or bit_rate not in config["bit_rates"]:
            raise NetworkError("invalid_speech_settings", "Bit rate is not a documented xAI value.")
    elif bit_rate is not None:
        raise NetworkError("invalid_speech_settings", "Bit rate applies to mp3 only.")
    return {"voice": voice, "language": language, "codec": codec, "sample_rate": sample_rate, "bit_rate": bit_rate}


def prepare(script, *, source="script_file", production_id=None, voice=None, language=None, codec=None,
            sample_rate=None, bit_rate=None, root=None, clock=None, config_root=None):
    """Offline. Returns the (new or existing) job record. Nothing is sent and nothing is paid."""
    clock = clock or utc_now
    config = load_config(config_root or ROOT)
    settings = settings_for(config, voice, language, codec, sample_rate, bit_rate)
    text = narration_text(script)
    if len(text) > config["limits"]["max_text_characters"]:
        raise NetworkError("speech_text_too_long", "Narration text exceeds max_text_characters.")
    output_format = {"codec": settings["codec"], "sample_rate": settings["sample_rate"]}
    if settings["codec"] == "mp3":
        output_format["bit_rate"] = settings["bit_rate"]
    request = {"text": text, "voice_id": settings["voice"], "language": settings["language"],
               "output_format": output_format}
    digest = script_digest(script)
    job_id = "sp-" + sha256_bytes(canonical({"script_sha256": digest, "request": request}))[:24]
    with _Locked(root):
        if (folder(job_id, root) / "job.json").exists():
            return _load(job_id, root)
        now = clock()
        record = {"contract": CONTRACT, "version": VERSION, "job_id": job_id, "status": "prepared",
                  "created_at": now, "updated_at": now, "notice": NOTICE, "publishable": False,
                  "script": {"script_id": script["script_id"], "script_sha256": digest, "source": source,
                             "production_id": production_id, "title": script["title"]},
                  "text": text, "text_sha256": sha256_bytes(text.encode()), "text_characters": len(text),
                  "settings": settings, "request": request, "consent_phrase": f"paid-speech:{job_id}",
                  "provider_request_id": None, "attempts": [], "raw_audio": None, "audio": None, "error_code": None}
        return _save(record, root, clock, create=True)


def inspect(job_id, root=None):
    """Read-only. A job left in "submitting" was interrupted after its intent was saved (maybe billed);
    speech-recover marks it uncertain. Nothing is ever resent automatically."""
    return _load(job_id, root)


def list_jobs(root=None):
    rows = []
    if not base(root).is_dir():
        return rows
    for path in sorted(base(root).iterdir())[:500]:
        if not JOB_ID.match(path.name):
            continue
        try:
            record = inspect(path.name, root)
            rows.append({"job_id": record["job_id"], "status": record["status"], "voice": record["settings"]["voice"],
                         "script_id": record["script"]["script_id"], "updated_at": record["updated_at"]})
        except NetworkError as error:
            rows.append({"job_id": path.name, "error": error.code})
    return rows


# ---------------------------------------------------------------- submit (paid) and recovery

def submit(job_id, *, consent, allow_network, retry_uncertain=False, acknowledge_duplicate_billing=False,
           transport=None, root=None, clock=None, config_root=None):
    clock = clock or utc_now
    transport = transport or http_transport
    config = load_config(config_root or ROOT)
    limits = config["limits"]
    with _Locked(root):
        record = _load(job_id, root)
        if consent != f"paid-speech:{job_id}":
            raise NetworkError("speech_consent_required", "Submit needs --consent paid-speech:JOB_ID for this exact job.")
        if not allow_network:
            raise NetworkError("network_not_allowed", "Submission needs --allow-network.")
        status = record["status"]
        if status in ("uncertain", "invalid_audio", "submitting"):
            if status == "submitting":
                raise NetworkError("speech_submit_refused", "A submission was interrupted; run speech-recover first.")
            if not retry_uncertain:
                raise NetworkError("speech_submit_refused", "The earlier request may have been billed; it is never "
                                   "retried automatically. Use --retry-uncertain --acknowledge-duplicate-billing.")
            if not acknowledge_duplicate_billing:
                raise NetworkError("duplicate_billing_ack_required",
                                   "Retrying may bill twice; add --acknowledge-duplicate-billing.")
        elif status not in ("prepared", "rejected"):
            raise NetworkError("speech_submit_refused", "This job is not waiting for a submission.")
        settings_for(config, **{k: record["settings"][k] for k in ("voice", "language", "codec", "sample_rate",
                                                                    "bit_rate")})          # config may have changed
        if not os.environ.get("XAI_API_KEY", "").strip():
            raise NetworkError("missing_speech_credential", "Set XAI_API_KEY in your local environment.")
        if len(record["attempts"]) >= limits["max_attempts"]:
            raise NetworkError("speech_attempt_limit", "This job reached max_attempts.")
        previous = status
        record["status"], record["error_code"] = "submitting", None
        record["attempts"].append({"at": clock(), "outcome": "uncertain", "http_status": None})
        _save(record, root, clock)                      # the intent exists before any byte is sent
        try:
            response = transport(record["request"], timeout_seconds=limits["request_timeout_seconds"],
                                 max_bytes=limits["max_response_bytes"])
            audio = response.get("audio") if isinstance(response, dict) else None
            content_type = response.get("content_type") if isinstance(response, dict) else None
            if not isinstance(audio, bytes) or not audio or len(audio) > limits["max_response_bytes"]:
                raise ValueError
        except SpeechRejected as error:
            record["status"], record["error_code"] = "rejected", "speech_rejected"
            record["attempts"][-1].update(outcome="rejected", http_status=error.http_status)
            _save(record, root, clock)
            raise NetworkError("speech_rejected", error.message) from None
        except NetworkError as error:
            if error.code in ("speech_not_sent", "missing_speech_credential"):
                record["status"], record["error_code"] = previous, error.code
                record["attempts"][-1].update(outcome="not_sent")
                _save(record, root, clock)
                raise NetworkError(error.code, error.message) from None
            return _uncertain(record, root, clock)
        except Exception:
            return _uncertain(record, root, clock)
        record["attempts"][-1].update(outcome="received")
        _store_raw(record, audio, content_type, root, clock)
        return _process(record, root, clock)


def _uncertain(record, root, clock):
    record["status"], record["error_code"] = "uncertain", "submission_outcome_unknown"
    _save(record, root, clock)
    raise NetworkError("speech_submit_uncertain", "The speech request's outcome is unknown and may have been billed. "
                       "It was not retried.")


def _store_raw(record, audio, content_type, root, clock):
    name = f"provider-audio-{len(record['attempts'])}.{record['settings']['codec']}"
    _write_atomic(folder(record["job_id"], root) / name, audio, create=True)
    record["raw_audio"] = {"file": name, "sha256": sha256_bytes(audio), "bytes": len(audio),
                           "content_type": content_type if isinstance(content_type, str) and
                           re.fullmatch(r"[a-z0-9.+/-]{0,100}", content_type) else ""}
    record["status"], record["error_code"], record["audio"] = "received", None, None
    _save(record, root, clock)


def recover(job_id, *, root=None, clock=None):
    """Offline. Never sends anything."""
    clock = clock or utc_now
    with _Locked(root):
        record = _load(job_id, root)
        if record["status"] == "submitting":
            return _uncertain_no_raise(record, root, clock)
        if record["status"] == "received" or (record["status"] == "invalid_audio"
                                              and record["error_code"] == "speech_decoder_unavailable"):
            return _process(record, root, clock)
        return record


def _uncertain_no_raise(record, root, clock):
    record["status"], record["error_code"] = "uncertain", "interrupted_submission"
    return _save(record, root, clock)


# ---------------------------------------------------------------- local conversion

def _ffmpeg():
    from .preview import dependencies
    try:
        return dependencies()[3]
    except NetworkError:
        return None


def _ffmpeg_decode(raw, codec, sample_rate):
    exe = _ffmpeg()
    if exe is None:
        raise NetworkError("speech_decoder_unavailable", "Install requirements-render.txt (FFmpeg) and run speech-recover.")
    with tempfile.TemporaryDirectory() as temp:
        source, target = Path(temp) / f"in.{codec}", Path(temp) / "out.wav"
        source.write_bytes(raw)
        try:
            result = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
                                     "-vn", "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
                                     "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-f", "wav", str(target)],
                                    capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            raise NetworkError("speech_audio_malformed", "The returned audio could not be decoded.") from None
        if result.returncode != 0 or not target.is_file():
            raise NetworkError("speech_audio_malformed", "The returned audio could not be decoded.")
        return parse_wav(target.read_bytes())


def decode_audio(raw, codec, sample_rate):
    """(channels, sample_rate, pcm) as 16-bit PCM. WAV is parsed in Python; MP3 (or a WAV variant the
    parser cannot read) is decoded with the local FFmpeg. Metadata is dropped either way."""
    if codec == "wav":
        if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            raise NetworkError("speech_audio_malformed", "The returned audio is not the requested WAV.")
        try:
            return parse_wav(raw)
        except NetworkError as error:
            # FFmpeg may read other WAV encodings (24-bit, float) or a streaming header with an unknown size;
            # anything without a format and a data chunk is simply malformed.
            if error.code not in ("narration_unsupported_format", "narration_corrupt") \
                    or b"fmt " not in raw[:4096] or b"data" not in raw[:65536]:
                raise NetworkError("speech_audio_malformed", "The returned audio is malformed.") from None
        try:
            return _ffmpeg_decode(raw, codec, sample_rate)
        except NetworkError as error:
            if error.code == "speech_decoder_unavailable":
                raise
            raise NetworkError("speech_audio_malformed", "The returned audio is malformed.") from None
    if not (raw[:3] == b"ID3" or (len(raw) > 1 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0)):
        raise NetworkError("speech_audio_malformed", "The returned audio is not the requested MP3.")
    try:
        return _ffmpeg_decode(raw, codec, sample_rate)
    except NetworkError as error:
        if error.code == "speech_decoder_unavailable":
            raise
        raise NetworkError("speech_audio_malformed", "The returned audio is malformed.") from None


def _process(record, root, clock):
    job_folder = folder(record["job_id"], root)
    raw_info = record["raw_audio"]
    try:
        raw = (job_folder / raw_info["file"]).read_bytes()
    except OSError:
        raw = None
    if raw is None or sha256_bytes(raw) != raw_info["sha256"]:
        record["status"], record["error_code"] = "invalid_audio", "speech_raw_audio_tampered"
        return _save(record, root, clock)
    try:
        channels, rate, pcm = decode_audio(raw, record["settings"]["codec"], record["settings"]["sample_rate"])
        if not pcm:
            raise NetworkError("speech_audio_malformed", "The returned audio has no samples.")
        if not any(pcm):
            raise NetworkError("speech_audio_silent", "The returned audio is completely silent.")
    except NetworkError as error:
        code = error.code if error.code in ("speech_audio_silent", "speech_decoder_unavailable") else "speech_audio_malformed"
        record["status"], record["error_code"] = ("received" if code == "speech_decoder_unavailable" else "invalid_audio"), code
        return _save(record, root, clock)
    wav = canonical_wav(channels, rate, pcm)
    duration = len(pcm) / (channels * 2) / rate
    target = job_folder / MANAGED
    if target.exists():
        if target.is_symlink() or target.read_bytes() != wav:
            target.unlink()                              # an earlier attempt's audio is replaced by the new one
    if not target.exists():
        _write_atomic(target, wav, create=True)
    audio = {"managed_file": MANAGED, "sha256": sha256_bytes(wav), "bytes": len(wav), "channels": channels,
             "sample_rate": rate, "duration_seconds": round(duration, 3), "normalized_sha256": None}
    if duration > MAX_SECONDS:
        record["audio"], record["status"], record["error_code"] = audio, "too_long", "narration_too_long"
        return _save(record, root, clock)
    try:
        metadata, _ = normalize_narration(wav)
    except NetworkError as error:
        record["status"], record["error_code"] = "invalid_audio", error.code if error.code.startswith("narration_") \
            else "speech_audio_malformed"
        return _save(record, root, clock)
    audio["normalized_sha256"] = metadata["normalized_sha256"]
    record["audio"], record["status"], record["error_code"] = audio, "completed", None
    return _save(record, root, clock)


def managed_audio(job_id, root=None):
    """(wav_bytes, record) of a completed job, hash-checked. Used by the video-production workflow."""
    record = _load(job_id, root)
    if record["status"] == "too_long":
        raise NetworkError("speech_narration_too_long", f"The generated narration is {record['audio']['duration_seconds']} s, "
                           "longer than 15 s. It is never truncated or sped up; shorten the script and prepare a new job.")
    if record["status"] != "completed":
        raise NetworkError("speech_not_completed", "This speech job has no usable narration yet.")
    path = folder(job_id, root) / MANAGED
    try:
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    except OSError:
        data = None
    if data is None or len(data) != record["audio"]["bytes"] or sha256_bytes(data) != record["audio"]["sha256"]:
        raise NetworkError("speech_audio_tampered", "The speech job's narration.wav is missing or changed.")
    return data, record


def view(record):
    """Printable summary. Raw audio stays on disk; nothing sensitive is in the record."""
    out = dict(record)
    status = record["status"]
    job = record["job_id"]
    nxt = []
    if status in ("prepared", "rejected"):
        nxt.append(f"python -m vicekrack speech-submit {job} --consent paid-speech:{job} --allow-network   (PAID)")
    elif status == "uncertain":
        nxt.append("Check your xAI usage first: the earlier request may have been billed.")
        nxt.append(f"python -m vicekrack speech-submit {job} --consent paid-speech:{job} --allow-network "
                   "--retry-uncertain --acknowledge-duplicate-billing   (PAID, may bill twice)")
    elif status == "submitting":
        nxt.append(f"python -m vicekrack speech-recover {job}")
    elif status == "received":
        nxt.append(f"python -m vicekrack speech-recover {job}   (converts the saved audio locally; no request)")
    elif status == "too_long":
        nxt.append("Narration too long: shorten the script's narration, then prepare a NEW job (nothing was cut off).")
    elif status == "invalid_audio":
        nxt.append("The returned audio was unusable. Prepare a job with a different voice or codec, or retry with "
                   "--retry-uncertain --acknowledge-duplicate-billing (PAID).")
    elif status == "completed":
        nxt.append(f"python -m vicekrack video-production-start --production PRODUCTION_ID --speech {job}")
    out["next"] = nxt
    return out
