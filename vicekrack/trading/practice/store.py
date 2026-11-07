"""Practice account storage (Trading PR 1): runtime/trading/practice/ (ignored by Git).

state.json        the current account, replaced atomically (temp file + fsync + rename)
ledger.jsonl      append-only history: every fill, every refusal reason and every halt
account.lock      OS lock: one process changes the account at a time (watch loop, phone, CLI)
history/          a stopped account is moved here, unchanged, when a person restarts

Every change is: lock -> load and re-validate -> guard -> change -> append ledger -> save state.
A missing, corrupted or unreadable account blocks trading; it is never guessed or rebuilt.
"""

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from ..contracts import ROOT, reject_trading_secrets, sha256, utc_now
from ..errors import TradingError
from . import account as book

RESTART_PHRASE = "restart-practice"


def load_config(path="config/practice-account.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_practice_config", "The practice settings must stay inside the project.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_practice_config", "Cannot read the practice account settings.") from None
    book.validate_config(config)
    return config


class PracticeStore:
    def __init__(self, root=None, lock_timeout=10.0):
        self.folder = Path(root if root is not None else ROOT) / "runtime/trading/practice"
        self.state_path = self.folder / "state.json"
        self.ledger_path = self.folder / "ledger.jsonl"
        self.lock_path = self.folder / "account.lock"
        self.history = self.folder / "history"
        self.lock_timeout = lock_timeout

    # ------------------------------------------------------------------ locking
    @contextmanager
    def lock(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.lock_path.is_symlink():
            raise TradingError("practice_state_corrupt", "The practice account lock cannot be a link.")
        stream = self.lock_path.open("a+b")
        locked = False
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            attempts = max(1, int(self.lock_timeout / 0.02)) + 1
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
                raise TradingError("practice_busy", "Another part of ViceKrack is using the practice account; try again.")
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

    # ------------------------------------------------------------------ files
    def _save(self, state):
        reject_trading_secrets(state)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.folder, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            temporary = None
        except (OSError, ValueError, TypeError):
            raise TradingError("practice_write_failed", "Could not save the practice account; nothing was changed.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _append(self, records, state):
        if not records:
            return
        try:
            with self.ledger_path.open("a", encoding="utf-8") as stream:
                for record in records:
                    state["sequence"] += 1
                    line = {"sequence": state["sequence"], "account_id": state["account_id"], "simulated": True, **record}
                    stream.write(json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, ValueError, TypeError):
            raise TradingError("practice_write_failed", "Could not write the practice ledger; nothing was changed.") from None

    def load(self, config):
        if not self.state_path.is_file():
            raise TradingError("practice_not_started", "No practice account yet; run practice-start first.")
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise TradingError("practice_state_corrupt", "The saved practice account is unreadable.") from None
        book.validate_state(state, config)
        return state

    def ledger(self, limit=50):
        if not self.ledger_path.is_file():
            return []
        lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:] if line.strip()]

    # ------------------------------------------------------------------ operations
    def start(self, config, now=None):
        now = now or utc_now()
        with self.lock():
            if self.state_path.exists():
                raise TradingError("practice_exists", "A practice account already exists. Use practice-restart to replace it.")
            state = book.new_state(config, now)
            self._append([{"kind": "start", "at": now, "starting_cash": state["starting_cash"], "floor": state["floor"]}], state)
            self._save(state)
            return state

    def restart(self, config, phrase, now=None):
        """A person's typed decision is the only way to clear a stop or replace an account."""
        if phrase != RESTART_PHRASE:
            raise TradingError("restart_not_confirmed", f"Type --confirm {RESTART_PHRASE} to restart the practice account.")
        now = now or utc_now()
        with self.lock():
            if self.state_path.exists():
                try:
                    old = json.loads(self.state_path.read_text(encoding="utf-8"))
                    name = old.get("account_id", "unknown")
                except (OSError, ValueError, UnicodeError):
                    name = "unreadable-" + sha256({"at": now})[:8]
                self.history.mkdir(parents=True, exist_ok=True)
                os.replace(self.state_path, self.history / f"{name}.json")
            state = book.new_state(config, now)
            self._append([{"kind": "restart", "at": now, "starting_cash": state["starting_cash"], "floor": state["floor"]}], state)
            self._save(state)
            return state

    def apply(self, config, operation, *args, now=None):
        """Run one account operation ('mark', 'buy' or 'sell') under the lock. Always saves the guard's outcome."""
        now = now or utc_now()
        with self.lock():
            state = self.load(config)
            refusal = None
            if operation == "mark":
                verdict, records = book.mark(state, config, *args, now)
            elif operation == "buy":
                verdict, records, refusal = book.buy(state, config, *args, now)
            elif operation == "sell":
                verdict, records, refusal = book.sell(state, config, *args, now)
            else:
                raise TradingError("unknown_operation", "Unknown practice account operation.")
            if refusal is not None:
                records.append({"kind": "refused", "at": now, "operation": operation, "code": refusal.code})
            state["updated_at"] = now
            self._append(records, state)
            self._save(state)
            return state, verdict, refusal
