"""Trading research session storage (Step 39): runtime/trading/sessions/ (ignored by Git).

One folder per session:

  <session_id>/session.json      the immutable session record (dataset, configuration
                                 snapshots and hashes, component versions)
  <session_id>/checkpoint.json   stage progress; replaced atomically (temp file + fsync +
                                 os.replace). Each revision carries its own SHA-256 and the
                                 previous revision's, so edits are detected.
  <session_id>/artifacts/N-<stage>.json
                                 one artifact per completed stage, published once with an
                                 exclusive hard link: never partial, never overwritten
  <session_id>/session.lock      OS lock held by the one process running the session; the
                                 operating system releases it if that process dies

A session folder is created complete (record + first checkpoint) in a temporary folder
and renamed into place, so a crash never leaves half a session behind. Readers (list,
inspect, the Living HQ) never take the lock, never create folders and never write.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from ...events.store import _Lock
from ..contracts import ROOT, reject_trading_secrets, sha256, validate_schema
from ..errors import TradingError

SESSION_ID = re.compile(r"^tss-[0-9a-f]{24}$")
STAGES = ("dataset_validation", "research_analysis", "simulation", "performance_analytics", "hq_summary")


def artifact_name(stage):
    return f"artifacts/{STAGES.index(stage) + 1}-{stage}.json"


def encode(document):
    """The exact bytes an artifact is stored as (the file hash is taken over these bytes)."""
    return (json.dumps(document, indent=1, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def file_sha256(data):
    return hashlib.sha256(data).hexdigest()


def seal_checkpoint(checkpoint):
    body = {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
    return {**body, "checkpoint_sha256": sha256(body)}


def validate_checkpoint(checkpoint, session_id):
    validate_schema("trading_session_checkpoint", checkpoint)
    body = {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
    if sha256(body) != checkpoint["checkpoint_sha256"] or checkpoint["session_id"] != session_id:
        raise TradingError("session_checkpoint_corrupt", "The session checkpoint failed its integrity check.")
    if [s["stage"] for s in checkpoint["stages"]] != list(STAGES) or [s["position"] for s in checkpoint["stages"]] != [1, 2, 3, 4, 5]:
        raise TradingError("session_checkpoint_corrupt", "The session checkpoint does not hold the fixed five stages.")
    seen_open = False
    for stage in checkpoint["stages"]:
        numbers = [a["attempt"] for a in stage["attempts"]]
        if numbers != list(range(1, len(numbers) + 1)):
            raise TradingError("session_checkpoint_corrupt", "Stage attempts are not numbered 1..N.")
        if (stage["status"] == "completed") != (stage["artifact"] is not None):
            raise TradingError("session_checkpoint_corrupt", "Only completed stages hold an artifact.")
        if stage["status"] == "pending" and stage["attempts"]:
            raise TradingError("session_checkpoint_corrupt", "A pending stage cannot have attempts.")
        if stage["status"] != "pending" and not stage["attempts"]:
            raise TradingError("session_checkpoint_corrupt", "A started stage needs an attempt.")
        if seen_open and stage["status"] != "pending":
            raise TradingError("session_checkpoint_corrupt", "A stage ran after an incomplete stage.")
        if stage["status"] != "completed":
            seen_open = True
        if stage["artifact"] is not None and stage["artifact"]["file"] != artifact_name(stage["stage"]):
            raise TradingError("session_checkpoint_corrupt", "An artifact path does not match its stage.")
    return checkpoint


def validate_record(record, session_id=None):
    validate_schema("trading_session", record)
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    if sha256(body) != record["record_sha256"] or (session_id is not None and record["session_id"] != session_id):
        raise TradingError("session_corrupt", "The session record failed its integrity check.")
    for name, item in record["inputs"].items():
        if sha256(item["document"]) != item["sha256"]:
            raise TradingError("session_corrupt", "A configuration snapshot does not match its hash.")
    return record


class SessionStore:
    def __init__(self, root=None):
        self.base = Path(root if root is not None else ROOT) / "runtime/trading/sessions"

    def folder(self, session_id):
        if not SESSION_ID.match(str(session_id)):
            raise TradingError("invalid_session_id", "A session ID looks like tss- followed by 24 hex characters.")
        return self.base / session_id

    # ------------------------------------------------------------------ writing (lock holder only)
    def create(self, record, checkpoint):
        """Create the session folder complete, or refuse if it already exists (`session_exists`)."""
        target = self.folder(record["session_id"])
        if target.exists():
            raise TradingError("session_exists", "This session already exists; inspect or resume it instead.")
        staging = None
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".creating-", dir=self.base))
            for name, document in (("session.json", record), ("checkpoint.json", checkpoint)):
                with open(staging / name, "wb") as stream:
                    stream.write(encode(document))
                    stream.flush()
                    os.fsync(stream.fileno())
            (staging / "artifacts").mkdir()
            os.rename(staging, target)                 # atomic; fails if the session appeared meanwhile
            staging = None
        except OSError:
            if target.exists():
                raise TradingError("session_exists", "This session already exists; inspect or resume it instead.") from None
            raise TradingError("session_write_failed", "Could not create the session; nothing was stored.") from None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def lock(self, session_id):
        lock = _Lock(self.folder(session_id) / "session.lock")
        if not lock.acquire(create=True):
            raise TradingError("session_busy", "Another process is running this session; nothing was started.")
        return lock

    def save_checkpoint(self, session_id, checkpoint):
        """Atomically replace the checkpoint (temp file + fsync + os.replace)."""
        reject_trading_secrets(checkpoint)
        validate_checkpoint(checkpoint, session_id)
        folder = self.folder(session_id)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=folder, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encode(checkpoint))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, folder / "checkpoint.json")
            temporary = None
        except OSError:
            raise TradingError("session_checkpoint_failed", "Could not save the session checkpoint.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return checkpoint

    def publish_artifact(self, session_id, stage, document):
        """Publish one stage artifact exactly once. Returns (relative path, file SHA-256)."""
        reject_trading_secrets(document)
        relative = artifact_name(stage)
        target = self.folder(session_id) / relative
        data = encode(document)
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        except FileExistsError:
            raise TradingError("session_artifact_exists",
                               "This stage's artifact already exists; it was not overwritten.") from None
        except OSError:
            raise TradingError("session_write_failed", "Could not save the stage artifact; nothing was stored.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return relative, file_sha256(data)

    # ------------------------------------------------------------------ reading (no lock, no writes)
    def exists(self, session_id):
        return (self.folder(session_id) / "session.json").is_file()

    def _read(self, path):
        if path.is_symlink():
            raise ValueError
        return path.read_bytes()

    def load_record(self, session_id):
        path = self.folder(session_id) / "session.json"
        if not path.is_file():
            raise TradingError("session_not_found", "No session with this ID.")
        try:
            record = json.loads(self._read(path).decode("utf-8"))
            return validate_record(record, session_id)
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("session_corrupt", "The session record failed validation.") from None
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError):
            raise TradingError("session_corrupt", "The session record is unreadable.") from None

    def load_checkpoint(self, session_id):
        path = self.folder(session_id) / "checkpoint.json"
        try:
            checkpoint = json.loads(self._read(path).decode("utf-8"))
            return validate_checkpoint(checkpoint, session_id)
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("session_checkpoint_corrupt", "The session checkpoint failed validation.") from None
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError):
            raise TradingError("session_checkpoint_corrupt", "The session checkpoint is unreadable.") from None

    def read_artifact(self, session_id, stage):
        """(document, file SHA-256) or None when the stage has no artifact file."""
        path = self.folder(session_id) / artifact_name(stage)
        if not path.exists() and not path.is_symlink():
            return None
        try:
            data = self._read(path)
            return json.loads(data.decode("utf-8")), file_sha256(data)
        except (OSError, ValueError, UnicodeError):
            raise TradingError("session_artifact_tampered", "A stage artifact is unreadable.") from None

    def live(self, session_id):
        """True only while a process holds the session lock (same machine)."""
        path = self.folder(session_id) / "session.lock"
        if not path.is_file():
            return False
        probe = _Lock(path)
        if probe.acquire(create=False):
            probe.release()
            return False
        return True

    def ids(self):
        if not self.base.is_dir():
            return []
        return sorted(p.name for p in self.base.iterdir() if SESSION_ID.match(p.name) and p.is_dir())
