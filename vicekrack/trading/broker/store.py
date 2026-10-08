"""Broker-paper storage (Step 41): runtime/trading/broker-paper/ (ignored by Git).

Kept apart from Step 24 local paper accounts (runtime/trading/accounts/) and the Step 29
offline simulator (runtime/trading/simulation/): different folders, contracts and IDs.

  operation.lock                 OS lock: one broker-paper network operation at a time
  kill-switch.json               the broker-paper kill switch (blocks new submissions only)
  checks/<bpc-id>.json           sanitized account/connection checks
  intents/<bpi-id>/
      intent.json                the prepared order (immutable)
      submission.json            written BEFORE the order request: proves an attempt began
      outcome.json               what the submit request returned: accepted, rejected or unknown
      observations/NNNNNN.json   broker-confirmed order states (append-only)
      cancels/NNNNNN.json        cancel requests and their immediate answers (append-only)
      events/NNNNNN.json         sanitized broker-paper execution events (append-only)

Every file is written to a temporary file, fsynced and published with an exclusive hard
link: never partial, never overwritten. Readers re-validate schemas on every load.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from ...events.store import _Lock
from ..contracts import ROOT, reject_trading_secrets, sha256, validate_schema
from ..errors import TradingError

INTENT_ID = re.compile(r"^bpi-[0-9a-f]{24}$")
CHECK_ID = re.compile(r"^bpc-[0-9a-f]{24}$")
MAX_SEQUENCE = 10000


class BrokerPaperStore:
    def __init__(self, root=None):
        self.base = Path(root if root is not None else ROOT) / "runtime/trading/broker-paper"

    # ------------------------------------------------------------------ writing
    def _publish(self, target, document, contract):
        validate_schema(contract, document)
        reject_trading_secrets(document)
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, indent=1, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        except FileExistsError:
            raise TradingError("broker_record_exists", "This broker-paper record already exists; it was not overwritten.") from None
        except OSError:
            raise TradingError("broker_write_failed", "Could not save broker-paper data.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return document

    def intent_folder(self, intent_id):
        if not INTENT_ID.match(str(intent_id)):
            raise TradingError("invalid_intent_id", "A paper intent ID looks like bpi- followed by 24 hex characters.")
        return self.base / "intents" / intent_id

    def save_intent(self, intent):
        folder = self.intent_folder(intent["intent_id"])
        return self._publish(folder / "intent.json", intent, "broker_paper_intent")

    def save_check(self, check):
        return self._publish(self.base / "checks" / f"{check['check_id']}.json", check, "broker_paper_check")

    def begin_submission(self, intent_id, submission):
        try:
            return self._publish(self.intent_folder(intent_id) / "submission.json", submission, "broker_paper_submission")
        except TradingError as error:
            if error.code == "broker_record_exists":
                raise TradingError("already_submitted", "This intent was already submitted once; it is never sent again. "
                                                        "Refresh its status instead.") from None
            raise

    def save_outcome(self, intent_id, outcome):
        return self._publish(self.intent_folder(intent_id) / "outcome.json", outcome, "broker_paper_outcome")

    def append(self, intent_id, kind, document):
        """Append one numbered record (observations, cancels, events). Callers hold the operation lock."""
        contract = {"observations": "broker_paper_observation", "cancels": "broker_paper_cancel",
                    "events": "broker_paper_event"}[kind]
        folder = self.intent_folder(intent_id) / kind
        number = len(list(folder.glob("[0-9]*.json"))) + 1 if folder.is_dir() else 1
        if number > MAX_SEQUENCE:
            raise TradingError("broker_record_limit", "Too many records for this intent.")
        document = {**document, "sequence": number}
        return self._publish(folder / f"{number:06d}.json", document, contract)

    def kill_switch(self, config):
        if config["kill_switch"]["engaged"]:
            return True, "config"
        path = self.base / "kill-switch.json"
        if not path.exists():
            return False, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("engaged"), bool):
                raise ValueError
        except (OSError, ValueError, UnicodeError):
            return True, "switch_file_unreadable"
        return (True, "switch_file") if data["engaged"] else (False, None)

    def set_kill_switch(self, engaged, at):
        path = self.base / "kill-switch.json"
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"engaged": bool(engaged), "changed_at": at}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError:
            raise TradingError("broker_write_failed", "Could not save the kill switch.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def lock(self):
        self.base.mkdir(parents=True, exist_ok=True)
        lock = _Lock(self.base / "operation.lock")
        if not lock.acquire(create=True):
            raise TradingError("broker_busy", "Another broker-paper command is running; nothing was done.")
        return lock

    # ------------------------------------------------------------------ reading (re-validated)
    def _load(self, path, contract):
        try:
            if path.is_symlink():
                raise ValueError
            document = json.loads(path.read_text(encoding="utf-8"))
            validate_schema(contract, document)
            return document
        except (TradingError, OSError, ValueError, UnicodeError):
            raise TradingError("broker_record_corrupt", "A saved broker-paper record failed validation.") from None

    def load_intent(self, intent_id):
        path = self.intent_folder(intent_id) / "intent.json"
        if not path.is_file():
            raise TradingError("intent_not_found", "No prepared paper intent with this ID.")
        intent = self._load(path, "broker_paper_intent")
        from .orders import validate_intent
        validate_intent(intent)
        if intent["intent_id"] != intent_id:
            raise TradingError("broker_record_corrupt", "A saved broker-paper record failed validation.")
        return intent

    def records(self, intent_id):
        """Everything saved for one intent, re-validated: (intent, submission, outcome, observations, cancels, events)."""
        folder = self.intent_folder(intent_id)
        intent = self.load_intent(intent_id)
        submission = self._load(folder / "submission.json", "broker_paper_submission") \
            if (folder / "submission.json").exists() else None
        outcome = self._load(folder / "outcome.json", "broker_paper_outcome") if (folder / "outcome.json").exists() else None
        lists = {}
        for kind, contract in (("observations", "broker_paper_observation"), ("cancels", "broker_paper_cancel"),
                               ("events", "broker_paper_event")):
            paths = sorted((folder / kind).glob("[0-9]*.json")) if (folder / kind).is_dir() else []
            items = [self._load(p, contract) for p in paths]
            if [i["sequence"] for i in items] != list(range(1, len(items) + 1)) or \
                    [p.name for p in paths] != [f"{n:06d}.json" for n in range(1, len(items) + 1)]:
                raise TradingError("broker_record_corrupt", "Broker-paper records are missing or out of sequence.")
            lists[kind] = items
        for item in (submission, outcome):
            if item is not None and (item["intent_id"] != intent_id or item["client_order_id"] != intent["client_order_id"]):
                raise TradingError("broker_record_corrupt", "Broker-paper records do not belong to this intent.")
        if outcome is not None and submission is None:
            raise TradingError("broker_record_corrupt", "An outcome exists without a submission record.")
        return intent, submission, outcome, lists["observations"], lists["cancels"], lists["events"]

    def intent_ids(self):
        folder = self.base / "intents"
        return sorted(p.name for p in folder.iterdir() if INTENT_ID.match(p.name)) if folder.is_dir() else []

    def latest_check(self):
        folder = self.base / "checks"
        best = None
        for path in sorted(folder.glob("bpc-*.json")) if folder.is_dir() else []:
            try:
                check = self._load(path, "broker_paper_check")
            except TradingError:
                continue
            if best is None or check["checked_at"] > best["checked_at"]:
                best = check
        return best


def check_id(document):
    return "bpc-" + sha256(document)[:24]
