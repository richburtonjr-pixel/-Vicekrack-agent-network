"""Persistent paper account risk state (Step 24). PAPER ONLY: nothing is submitted or executed.

Layout (ignored by Git): runtime/trading/accounts/<account_id>/
    state.json      validated `paper_account_state` 1.0 document with a self-hash and revision
    pending.json    write-ahead marker for an operation in flight (absent when clean)
    account.lock    OS lock shared by every process that touches this account

Every authorization is ONE operation under the account lock:
    load state -> duplicate check + risk checks (including pending reservations)
    -> write pending.json -> journal risk + intent events -> atomically replace state.json
    -> remove pending.json.
If anything fails after pending.json is written, the marker stays and the account blocks
every new authorization until `trading-state recover` decides, from the revisions,
whether the operation committed or rolled back. A rolled-back signal is still recorded as
processed, and recovery reconciles the journal so an intent that reached the journal is
never authorized a second time.

Missing (unexpectedly), corrupted, incompatible or uncertain state blocks authorization.
A new account must be created explicitly with `initialize`.

Trading day: the account's IANA timezone (default America/New_York), rolling over at local
midnight. Rollover archives the day's counters and resets them; processed signals and
active reservations are never cleared by a day change.
"""

import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ROOT, parse_time, sha256, utc_now, validate_schema
from .errors import TradingError
from .money import add, fmt, parse
from .orders import build_intent
from .risk import evaluate

ACCOUNT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
DEFAULT_TIMEZONE = "America/New_York"
CLOCK_SKEW = timedelta(seconds=5)
LEDGER_NOTE = "Paper authorizations only. Nothing was submitted or executed; realized P&L is not tracked."
MAX_SIGNALS = 10000
MAX_HISTORY = 400
NOTE = re.compile(r"^[A-Za-z0-9 .,:;()'_/-]{0,200}$")


def account_id_for(name):
    name = str(name)
    if name.startswith("acct-"):
        name = name[5:]
    if not ACCOUNT_NAME.match(name):
        raise TradingError("invalid_account_id", "Account names use 3-41 lowercase letters, digits or hyphens.")
    return "acct-" + name


def trading_date_for(as_of, timezone):
    return parse_time(as_of).astimezone(_zone(timezone)).date().isoformat()


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise TradingError("invalid_timezone", "The trading-day timezone is not a known IANA timezone.") from None


def _seal(state):
    body = {k: v for k, v in state.items() if k != "state_sha256"}
    state["state_sha256"] = sha256(body)
    return state


def _ledger(state):
    intents = state["intents"]
    return {"authorized_paper_intents": len(intents),
            "active_reservations": sum(i["reservation"]["status"] == "active" for i in intents),
            "cancelled_intents": sum(i["status"] == "cancelled" for i in intents),
            "submitted_orders": 0, "executed_trades": 0, "realized_pnl": None, "note": LEDGER_NOTE}


def validate_state(state, account_id=None):
    """Schema + integrity + internal consistency. Any problem -> state_corrupt/incompatible."""
    if isinstance(state, dict) and (state.get("contract") != "paper_account_state" or state.get("version") != "1.0"):
        raise TradingError("state_incompatible", "The paper account state has an unsupported contract or version.")
    try:
        validate_schema("paper_account_state", state)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        raise TradingError("state_corrupt", "The paper account state failed validation.") from None

    def bad(reason):
        raise TradingError("state_corrupt", f"The paper account state is inconsistent: {reason}.")
    if account_id is not None and state["account_id"] != account_id:
        bad("account mismatch")
    if _seal(deepcopy(state))["state_sha256"] != state["state_sha256"]:
        bad("integrity hash mismatch")
    _zone(state["trading_day"]["timezone"])
    signals = {s["signal_id"]: s for s in state["processed_signals"]}
    if len(signals) != len(state["processed_signals"]):
        bad("duplicate processed signal")
    intents = {i["intent_id"]: i for i in state["intents"]}
    if len(intents) != len(state["intents"]):
        bad("duplicate intent")
    for intent in state["intents"]:
        record = signals.get(intent["signal_id"])
        if record is None or record["intent_id"] != intent["intent_id"] or record["outcome"] != "authorized_paper":
            bad("intent without matching authorized signal")
        reservation = intent["reservation"]
        if (intent["status"] == "cancelled") != (reservation["status"] == "released"):
            bad("reservation status does not match intent status")
        if reservation["status"] == "released" and (reservation["released_at"] is None or reservation["release_reason"] is None):
            bad("released reservation without a reason")
        if reservation["status"] == "active" and reservation["release_reason"] is not None:
            bad("active reservation with a release reason")
        quantity = parse(intent["quantity"])
        expected = quantity if intent["side"] == "buy" else -quantity
        if parse(reservation["reserved_quantity"], "signed_decimal") != expected:
            bad("reservation quantity mismatch")
    for record in state["processed_signals"]:
        if record["outcome"] == "authorized_paper" and record["intent_id"] not in intents:
            bad("authorized signal without intent")
    day = state["trading_day"]
    if day["current_date"] is not None:
        authorized = sum(i["trading_date"] == day["current_date"] for i in state["intents"])
        if authorized != day["authorized_count"]:
            bad("daily counter does not match authorized intents")
    if [h["date"] for h in state["day_history"]] != sorted({h["date"] for h in state["day_history"]}):
        bad("day history out of order")
    if state["ledger"] != _ledger(state):
        bad("ledger summary mismatch")


def reserved_by_symbol(state):
    reserved = {}
    for intent in state["intents"]:
        if intent["reservation"]["status"] == "active":
            reserved[intent["symbol"]] = add(reserved.get(intent["symbol"], Decimal(0)),
                                             parse(intent["reservation"]["reserved_quantity"], "signed_decimal"))
    return reserved


class PaperAccount:
    def __init__(self, account, root=None, clock=None, lock_timeout=10.0):
        self.account_id = account_id_for(account)
        self.folder = Path(root if root is not None else ROOT) / "runtime/trading/accounts" / self.account_id
        self.state_path = self.folder / "state.json"
        self.pending_path = self.folder / "pending.json"
        self.lock_path = self.folder / "account.lock"
        self.clock = clock
        self.lock_timeout = lock_timeout
        self._locked = False

    def now(self):
        return self.clock() if self.clock is not None else utc_now()

    # ------------------------------------------------------------------ locking
    @contextmanager
    def lock(self):
        """Exclusive OS lock shared across processes; waits up to lock_timeout seconds."""
        if not self.folder.is_dir():
            raise TradingError("state_not_initialized", "This paper account does not exist; run trading-state init first.")
        if self.lock_path.is_symlink():
            raise TradingError("state_corrupt", "Account locks cannot be symbolic links.")
        try:
            stream = self.lock_path.open("a+b")
        except OSError:
            raise TradingError("state_unavailable", "Cannot open the paper account lock.") from None
        locked = False
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            attempts = max(1, int(self.lock_timeout / 0.02)) + 1   # bounded wait, never an endless loop
            for attempt in range(attempts):
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if attempt + 1 < attempts:
                        time.sleep(0.02)
            if not locked:
                raise TradingError("account_busy", "Another process holds this paper account; nothing was authorized.")
            self._locked = True
            yield self
        finally:
            self._locked = False
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def _require_lock(self):
        if not self._locked:
            raise TradingError("account_not_locked", "Paper account changes require the account lock.")

    # ------------------------------------------------------------------ storage
    def _write_json(self, path, document, exclusive=False):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.folder, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if exclusive:
                os.link(temporary, path)
            else:
                os.replace(temporary, path)
                temporary = None
            self._sync_folder()
        except FileExistsError:
            raise TradingError("state_recovery_required", "An unfinished paper account operation exists; run trading-state recover.") from None
        except (OSError, ValueError, TypeError):
            raise TradingError("state_write_failed", "Could not save paper account state; run trading-state recover before continuing.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _sync_folder(self):
        if os.name != "nt":
            try:
                descriptor = os.open(self.folder, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                pass

    def _read_state(self):
        if not self.folder.is_dir():
            raise TradingError("state_not_initialized", "This paper account does not exist; run trading-state init first.")
        if not self.state_path.exists():
            raise TradingError("state_missing", "The paper account state file is missing; authorization is blocked.")
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise TradingError("state_corrupt", "The paper account state file is unreadable.") from None
        if not isinstance(state, dict):
            raise TradingError("state_corrupt", "The paper account state file is not an object.")
        validate_state(state, self.account_id)
        return state

    def load(self):
        """Validated state for authorization: blocks while an operation is unresolved."""
        if self.pending_path.exists():
            raise TradingError("state_recovery_required", "An unfinished paper account operation exists; run trading-state recover.")
        return self._read_state()

    def inspect(self):
        """Read-only view (no lock): includes whether recovery is required."""
        state = self._read_state()
        return {"state": state, "recovery_required": self.pending_path.exists(),
                "reserved_by_symbol": {k: _fmt(v) for k, v in sorted(reserved_by_symbol(state).items())}}

    def _commit(self, state, new_state, operation, journal_writes, *, signal_ids=(), intent_ids=(), run_id=None):
        """Write-ahead marker -> journal -> state -> clear marker. Failure leaves the marker."""
        self._require_lock()
        new_state["revision"] = state["revision"] + 1
        new_state["updated_at"] = self.now()
        new_state["ledger"] = _ledger(new_state)
        _seal(new_state)
        validate_state(new_state, self.account_id)
        operation_id = "op-" + uuid.uuid4().hex
        pending = {"contract": "paper_state_pending", "version": "1.0", "account_id": self.account_id,
                   "operation_id": operation_id, "operation": operation, "revision_before": state["revision"],
                   "revision_after": new_state["revision"], "state_sha256_after": new_state["state_sha256"],
                   "signal_ids": list(signal_ids), "intent_ids": list(intent_ids), "run_id": run_id,
                   "created_at": new_state["updated_at"]}
        validate_schema("paper_state_pending", pending)
        self._write_json(self.pending_path, pending, exclusive=True)
        journal_writes(operation_id)          # raises journal_write_failed -> marker stays
        self._write_json(self.state_path, new_state)
        try:
            self.pending_path.unlink()
            self._sync_folder()
        except OSError:
            raise TradingError("state_write_failed", "State saved but the operation marker remains; run trading-state recover.") from None
        return new_state

    # ------------------------------------------------------------------ initialize
    def initialize(self, timezone=DEFAULT_TIMEZONE, journal=None):
        _zone(timezone)
        try:
            self.folder.parent.mkdir(parents=True, exist_ok=True)
            self.folder.mkdir()
        except FileExistsError:
            raise TradingError("account_exists", "This paper account already exists; it was not changed.") from None
        except OSError:
            raise TradingError("state_write_failed", "Cannot create paper account storage.") from None
        now = self.now()
        state = {
            "contract": "paper_account_state", "version": "1.0", "account_id": self.account_id, "mode": "paper",
            "currency": "USD", "simulated": True, "created_at": now, "updated_at": now, "revision": 0,
            "trading_day": {"timezone": timezone, "rollover": "local_midnight", "current_date": None,
                            "authorized_count": 0, "cancelled_count": 0, "blocked_count": 0, "last_decision_at": None},
            "day_history": [], "processed_signals": [], "intents": [], "recoveries": [],
        }
        state["ledger"] = _ledger(state)
        _seal(state)
        validate_state(state, self.account_id)
        with self.lock():
            self._write_json(self.state_path, state)
        if journal is not None:
            run_id = journal.start_run()
            journal.append(run_id, stage="state_initialized", status="completed", agent="paper_state", recorded_at=now,
                           subject={"account_id": self.account_id},
                           observed={"timezone": timezone, "rollover": "local_midnight", "revision": 0})
        return state

    # ------------------------------------------------------------------ day handling
    def _roll(self, state, as_of):
        """Return (trading_date, problems). Mutates state counters on a forward day change."""
        day = state["trading_day"]
        trading_date = trading_date_for(as_of, day["timezone"])
        problems = []
        if day["last_decision_at"] is not None and parse_time(as_of) < parse_time(day["last_decision_at"]) - CLOCK_SKEW:
            problems.append("clock_regression")
        if day["current_date"] is not None and trading_date < day["current_date"]:
            if "clock_regression" not in problems:
                problems.append("clock_regression")
        elif day["current_date"] != trading_date and not problems:
            if day["current_date"] is not None:
                state["day_history"].append({"date": day["current_date"], "authorized_count": day["authorized_count"],
                                             "cancelled_count": day["cancelled_count"], "blocked_count": day["blocked_count"]})
                if len(state["day_history"]) > MAX_HISTORY:
                    raise TradingError("state_capacity_reached", "Paper account day history is full; start a new account.")
            day.update(current_date=trading_date, authorized_count=0, cancelled_count=0, blocked_count=0)
        return trading_date, problems

    # ------------------------------------------------------------------ authorize
    def authorize(self, *, signal, snapshot, portfolio, config, config_sha256, kill_switch, as_of,
                  input_problems=(), journal=None, run_id=None, causation_id=None):
        """Duplicate check, risk checks and reservation as one locked, crash-safe operation.

        Returns (decision, intent). `signal` must be a validated signal (invalid signals are
        blocked by the caller without touching state)."""
        self._require_lock()
        state = self.load()
        new_state = deepcopy(state)
        trading_date, problems = self._roll(new_state, as_of)
        day = new_state["trading_day"]
        processed = {s["signal_id"] for s in new_state["processed_signals"]}
        state_orders = state["trading_day"]["authorized_count"] if state["trading_day"]["current_date"] == trading_date else 0
        if signal["signal_id"] not in processed and len(processed) >= MAX_SIGNALS:
            problems.append("state_capacity_reached")
        decision = evaluate(config=config, config_sha256=config_sha256, snapshot=snapshot, signal=signal,
                            portfolio=portfolio, as_of=as_of, kill_switch=kill_switch,
                            orders_today=day["authorized_count"], used_signal_ids=processed,
                            input_problems=list(input_problems) + problems,
                            reserved=reserved_by_symbol(new_state), trading_date=trading_date)
        intent = build_intent(signal, decision, snapshot, as_of)
        allowed = intent["status"] == "authorized_paper"
        if not problems:
            day["last_decision_at"] = as_of if day["last_decision_at"] is None else max(day["last_decision_at"], as_of)
        if allowed:
            quantity = parse(intent["quantity"])
            new_state["intents"].append({
                "intent_id": intent["intent_id"], "signal_id": intent["signal_id"], "decision_id": intent["decision_id"],
                "intent_sha256": sha256(intent), "symbol": intent["symbol"], "side": intent["side"],
                "quantity": intent["quantity"], "order_type": intent["order_type"],
                "reference_price": intent["reference_price"], "notional": intent["notional"],
                "trading_date": trading_date, "authorized_at": as_of, "status": "authorized_paper",
                "reservation": {"status": "active", "reserved_quantity": _fmt(quantity if intent["side"] == "buy" else -quantity),
                                "released_at": None, "release_reason": None, "release_note": None},
                "submitted": False, "executed": False})
            day["authorized_count"] += 1
        else:
            day["blocked_count"] += 1
        if signal["signal_id"] not in processed and "state_capacity_reached" not in problems:
            new_state["processed_signals"].append({
                "signal_id": signal["signal_id"], "outcome": intent["status"], "decision_id": decision["decision_id"],
                "intent_id": intent["intent_id"] if allowed else None, "processed_at": as_of,
                "trading_date": trading_date, "reason_codes": decision["reason_codes"]})

        def journal_writes(operation_id):
            if journal is None:
                return
            subject = {"account_id": self.account_id, "operation_id": operation_id}
            risk_event = journal.append(
                run_id, stage="risk_check", status=decision["outcome"], agent="risk_engine", recorded_at=self.now(),
                causation_id=causation_id,
                subject={**subject, "signal_id": signal["signal_id"], "decision_id": decision["decision_id"],
                         **({"snapshot_id": decision["snapshot_id"]} if decision["snapshot_id"] else {})},
                observed={"checks_failed": sum(c["status"] == "fail" for c in decision["checks"]),
                          "checks_passed": sum(c["status"] == "pass" for c in decision["checks"]),
                          "checks_skipped": sum(c["status"] == "skipped" for c in decision["checks"]),
                          "trading_date": trading_date, "orders_today_before": state_orders,
                          "state_revision_before": state["revision"]},
                reason_codes=decision["reason_codes"], document=decision)
            journal.append(
                run_id, stage="order_intent", status="recorded" if allowed else "blocked", agent="paper_order_desk",
                recorded_at=self.now(), causation_id=risk_event["event_id"],
                subject={**subject, "symbol": intent["symbol"], "signal_id": intent["signal_id"],
                         "decision_id": intent["decision_id"], "intent_id": intent["intent_id"]},
                observed={"intent_status": intent["status"], "notional": intent["notional"],
                          "reservation": "active" if allowed else "none", "submitted": False, "executed": False},
                reason_codes=decision["reason_codes"], document=intent)

        self._commit(state, new_state, "authorize", journal_writes, signal_ids=[signal["signal_id"]],
                     intent_ids=[intent["intent_id"]] if allowed else [], run_id=run_id)
        return decision, intent

    # ------------------------------------------------------------------ cancel
    def cancel(self, intent_id, reason, note=None, *, journal=None):
        """Release the reservation of an authorized, unsubmitted paper intent. History is kept."""
        self._require_lock()
        if not re.match(r"^[a-z0-9_]{1,60}$", str(reason)):
            raise TradingError("invalid_cancel_reason", "A cancel reason is a short lowercase code such as operator_request.")
        if note is not None and not NOTE.match(note):
            raise TradingError("invalid_cancel_note", "A cancel note is plain text up to 200 characters.")
        state = self.load()
        new_state = deepcopy(state)
        intent = next((i for i in new_state["intents"] if i["intent_id"] == intent_id), None)
        if intent is None:
            raise TradingError("intent_not_found", "No authorized paper intent with this ID exists in this account.")
        if intent["status"] != "authorized_paper" or intent["reservation"]["status"] != "active" or intent["submitted"]:
            raise TradingError("intent_not_cancellable", "Only active, unsubmitted paper intents can be cancelled.")
        at = self.now()
        intent["status"] = "cancelled"
        intent["reservation"].update(status="released", released_at=at, release_reason=reason, release_note=note)
        if new_state["trading_day"]["current_date"] is not None:
            new_state["trading_day"]["cancelled_count"] += 1
        run_id = journal.start_run() if journal is not None else None

        def journal_writes(operation_id):
            if journal is None:
                return
            journal.append(run_id, stage="intent_cancelled", status="cancelled", agent="paper_state", recorded_at=at,
                           subject={"account_id": self.account_id, "operation_id": operation_id, "intent_id": intent_id,
                                    "signal_id": intent["signal_id"], "symbol": intent["symbol"]},
                           observed={"release_reason": reason, "released_quantity": intent["reservation"]["reserved_quantity"],
                                     "submitted": False, "executed": False},
                           reason_codes=[reason])
        self._commit(state, new_state, "cancel", journal_writes, signal_ids=[intent["signal_id"]],
                     intent_ids=[intent_id], run_id=run_id)
        return {"intent_id": intent_id, "status": "cancelled", "release_reason": reason, "released_at": at,
                "run_id": run_id, "submitted": False, "executed": False}

    # ------------------------------------------------------------------ recover
    def recover(self, journal):
        """Explicit recovery: resolve an unfinished operation and reconcile the journal."""
        self._require_lock()
        state = self._read_state()                       # corrupted state is never auto-repaired
        self._remove_orphans()
        pending, readable = None, True
        if self.pending_path.exists():
            try:
                pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
                validate_schema("paper_state_pending", pending)
                if pending["account_id"] != self.account_id:
                    raise ValueError
            except (OSError, ValueError, UnicodeError, TradingError):
                pending, readable = None, False
        elif not self._unreconciled(state, journal):
            return {"status": "clean", "account_id": self.account_id, "revision": state["revision"]}

        operation_id = pending["operation_id"] if pending else None
        already = operation_id is not None and any(r["operation_id"] == operation_id for r in state["recoveries"])
        new_state = deepcopy(state)
        if already:
            outcome = "already_recovered"
        elif pending is None:
            outcome = "discarded_unreadable" if not readable else "reconciled"
        elif state["revision"] == pending["revision_after"] and state["state_sha256"] == pending["state_sha256_after"]:
            outcome = "committed"
        elif state["revision"] == pending["revision_before"]:
            outcome = "rolled_back"
        else:
            raise TradingError("state_recovery_conflict", "State and the unfinished operation disagree; nothing was changed.")

        if outcome != "already_recovered":
            if outcome == "rolled_back" and pending["operation"] == "authorize":
                self._mark_processed(new_state, pending["signal_ids"])
            reconciled = self._unreconciled(new_state, journal)
            self._mark_processed(new_state, reconciled)
            new_state["recoveries"].append({
                "operation_id": operation_id, "operation": pending["operation"] if pending else "unknown",
                "outcome": outcome, "recovered_at": self.now(),
                "signal_ids": pending["signal_ids"] if pending else [], "reconciled_signal_ids": reconciled[:100]})
            if len(new_state["recoveries"]) > 1000:
                raise TradingError("state_capacity_reached", "Paper account recovery history is full; start a new account.")
            new_state["revision"] = state["revision"] + 1
            new_state["updated_at"] = self.now()
            new_state["ledger"] = _ledger(new_state)
            _seal(new_state)
            validate_state(new_state, self.account_id)
            self._write_json(self.state_path, new_state)
        else:
            reconciled = []
        try:
            self.pending_path.unlink(missing_ok=True)
            self._sync_folder()
        except OSError:
            raise TradingError("state_write_failed", "Could not clear the operation marker; run trading-state recover again.") from None
        run_id = None
        if outcome != "already_recovered":
            run_id = journal.start_run()
            journal.append(run_id, stage="state_recovery",
                           status={"committed": "committed", "rolled_back": "rolled_back"}.get(outcome, "completed"),
                           agent="paper_state", recorded_at=self.now(),
                           subject={"account_id": self.account_id, **({"operation_id": operation_id} if operation_id else {})},
                           observed={"outcome": outcome, "operation": pending["operation"] if pending else "unknown",
                                     "reconciled_signals": len(reconciled), "revision": new_state["revision"]},
                           reason_codes=[outcome])
        return {"status": outcome, "account_id": self.account_id, "operation_id": operation_id,
                "reconciled_signal_ids": reconciled, "revision": new_state["revision"], "run_id": run_id}

    def _mark_processed(self, state, signal_ids):
        known = {s["signal_id"] for s in state["processed_signals"]}
        for signal_id in signal_ids:
            if signal_id not in known:
                if len(known) >= MAX_SIGNALS:
                    raise TradingError("state_capacity_reached", "Paper account signal history is full; start a new account.")
                state["processed_signals"].append({
                    "signal_id": signal_id, "outcome": "rolled_back", "decision_id": None, "intent_id": None,
                    "processed_at": self.now(), "trading_date": None, "reason_codes": ["recovered_not_committed"]})
                known.add(signal_id)

    def _unreconciled(self, state, journal):
        """Signals whose paper intent reached the journal for this account but not the state."""
        known_intents = {i["intent_id"] for i in state["intents"]}
        known_signals = {s["signal_id"] for s in state["processed_signals"]}
        missing = []
        for run in journal.list_runs():
            if not run["readable"]:
                raise TradingError("journal_corrupt", "A journal run is unreadable; recovery cannot be certain.")
            for event in journal.read_run(run["run_id"]):
                subject = event["subject"]
                if (event["stage"] == "order_intent" and event["status"] == "recorded"
                        and subject.get("account_id") == self.account_id
                        and subject.get("intent_id") not in known_intents
                        and subject.get("signal_id") not in known_signals and subject.get("signal_id") not in missing):
                    missing.append(subject["signal_id"])
        return missing

    def _remove_orphans(self):
        for path in self.folder.glob("*.tmp"):
            try:
                path.unlink()
            except OSError:
                pass


def _fmt(value):
    return fmt(value)
