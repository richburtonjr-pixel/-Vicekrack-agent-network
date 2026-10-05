"""Step 24: persistent paper account risk state. Synthetic data only; no network, no credits."""

import json
import os
import shutil
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack.trading import demo
from vicekrack.trading.config import kill_switch_state, load_config, set_kill_switch, switch_path
from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.journal import TradingJournal
from vicekrack.trading.state import PaperAccount, account_id_for, trading_date_for, validate_state

AS_OF = "2026-01-15T15:00:00Z"


def scenario(name="allowed"):
    return json.loads((ROOT / "examples/trading" / f"scenario-{name}.json").read_text(encoding="utf-8"))


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config, self.config_sha = load_config()
        base = scenario()
        self.snapshot, self.signal, self.portfolio = base["snapshot"], base["signals"][0], base["portfolio"]
        self.journal = TradingJournal(self.root)
        self.account().initialize(journal=self.journal)

    def account(self, name="test-acct", **kwargs):
        kwargs.setdefault("clock", lambda: AS_OF)
        return PaperAccount(name, root=self.root, **kwargs)

    def signal_with(self, signal_id=None, quantity=None, side=None, created=None, expires=None):
        signal = deepcopy(self.signal)
        if signal_id:
            signal["signal_id"] = signal_id
        if quantity:
            signal["proposal"]["quantity"] = quantity
        if side:
            signal["proposal"]["side"] = side
        if created:
            signal["created_at"] = created
        if expires:
            signal["expires_at"] = expires
        return signal

    def authorize(self, signal=None, *, account=None, as_of=AS_OF, snapshot=None, portfolio=None, kill_switch=None,
                  journal=True, run=None):
        account = account or self.account()
        snapshot = snapshot or self.snapshot
        if as_of != AS_OF and snapshot is self.snapshot:
            snapshot = dict(self.snapshot, observed_at=as_of)
        portfolio = portfolio or self.portfolio
        with account.lock():
            return account.authorize(
                signal=signal or self.signal, snapshot=snapshot, portfolio=portfolio, config=self.config,
                config_sha256=self.config_sha, kill_switch=kill_switch or kill_switch_state(self.config, self.root),
                as_of=as_of, journal=self.journal if journal else None,
                run_id=(run or self.journal.start_run()) if journal else None)

    def state(self, name="test-acct"):
        return self.account(name).inspect()["state"]

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception


class InitializationTests(Base):
    def test_explicit_initialization_required(self):
        missing = PaperAccount("never-made", root=self.root)
        self.assertCode("state_not_initialized", missing.load)
        self.assertCode("state_not_initialized", self.authorize, account=missing)
        self.assertCode("state_not_initialized", demo.run_demo, "allowed", account="never-made", root=self.root)
        self.assertFalse((self.root / "runtime/trading/accounts/acct-never-made").exists())

    def test_initialize_once(self):
        state = self.state()
        self.assertEqual((state["revision"], state["trading_day"]["timezone"]), (0, "America/New_York"))
        self.assertEqual(state["ledger"]["submitted_orders"], 0)
        self.assertIsNone(state["ledger"]["realized_pnl"])
        self.assertCode("account_exists", self.account().initialize)
        self.assertEqual(self.state(), state)                      # unchanged
        stages = [e["stage"] for r in self.journal.list_runs() for e in self.journal.read_run(r["run_id"])]
        self.assertIn("state_initialized", stages)

    def test_names_and_timezones(self):
        self.assertEqual(account_id_for("demo-1"), "acct-demo-1")
        for bad in ("AB", "../x", "a", "acct-" + "x" * 50, "with space"):
            self.assertCode("invalid_account_id", account_id_for, bad)
        self.assertCode("invalid_timezone", self.account("tz-bad").initialize, "Mars/Olympus")
        self.account("tz-utc").initialize("UTC")
        self.assertEqual(self.state("tz-utc")["trading_day"]["timezone"], "UTC")

    def test_runtime_state_is_git_ignored(self):
        self.assertIn("runtime/", (ROOT / ".gitignore").read_text(encoding="utf-8").split())


class PersistenceTests(Base):
    def test_restart_keeps_duplicates_counters_and_reservations(self):
        decision, intent = self.authorize()
        self.assertEqual(intent["status"], "authorized_paper")
        restarted = self.account()                                 # new object = new process
        state = restarted.inspect()
        self.assertEqual(state["reserved_by_symbol"], {"SYNTH1": "10"})
        self.assertEqual(state["state"]["trading_day"]["authorized_count"], 1)
        decision, again = self.authorize(account=restarted)
        self.assertEqual(again["status"], "blocked")
        self.assertIn("duplicate_signal", decision["reason_codes"])
        state = self.state()
        self.assertEqual([s["signal_id"] for s in state["processed_signals"]], [self.signal["signal_id"]])
        self.assertEqual(state["trading_day"]["blocked_count"], 1)
        self.assertEqual(state["revision"], 2)

    def test_blocked_signals_are_also_never_reprocessed(self):
        engaged = (True, "switch_file")
        self.assertEqual(self.authorize(kill_switch=engaged)[1]["status"], "blocked")
        decision, intent = self.authorize()
        self.assertEqual(intent["status"], "blocked")
        self.assertEqual(decision["reason_codes"], ["duplicate_signal"])

    def test_authorized_is_not_submitted_or_executed(self):
        _, intent = self.authorize()
        state = self.state()
        record = state["intents"][0]
        self.assertEqual((record["submitted"], record["executed"]), (False, False))
        self.assertEqual(intent["execution"], {"submitted": False, "executed": False, "broker": None})
        self.assertEqual(state["ledger"], {"authorized_paper_intents": 1, "active_reservations": 1, "cancelled_intents": 0,
                                           "submitted_orders": 0, "executed_trades": 0, "realized_pnl": None,
                                           "note": state["ledger"]["note"]})
        forged = deepcopy(state)
        forged["ledger"]["executed_trades"] = 1
        self.assertCode("state_corrupt", validate_state, forged)

    def test_precise_decimal_reservations(self):
        self.authorize(self.signal_with("sig-precise-0001", quantity="0.12345678"))
        self.authorize(self.signal_with("sig-precise-0002", quantity="0.00000002"))
        self.assertEqual(self.account().inspect()["reserved_by_symbol"], {"SYNTH1": "0.1234568"})

    def test_orders_per_day_persist_across_runs(self):
        for number in range(5):
            self.assertEqual(self.authorize(self.signal_with(f"sig-day-{number:04d}", quantity="1"))[1]["status"],
                             "authorized_paper")
        decision, intent = self.authorize(self.signal_with("sig-day-0005", quantity="1"))
        self.assertIn("orders_per_day_exceeded", decision["reason_codes"])


class ExposureAndCancelTests(Base):
    def test_pending_reservations_count_toward_exposure(self):
        position = deepcopy(self.portfolio)
        position["positions"] = [{"symbol": "SYNTH1", "quantity": "25", "average_price": "48.00"}]
        # Alone: (25 + 10) * 50 = 1750 <= 2000. With a pending 10-unit reservation: 2250 > 2000.
        self.authorize()
        decision, intent = self.authorize(self.signal_with("sig-pending-0001"), portfolio=position)
        self.assertEqual(intent["status"], "blocked")
        self.assertEqual(decision["reason_codes"], ["position_exposure_exceeded"])
        check = next(c for c in decision["checks"] if c["check"] == "pending_reservations")
        self.assertEqual(check["observed"], "10")

    def test_cancellation_releases_reservation_and_keeps_history(self):
        _, intent = self.authorize()
        account = self.account()
        with account.lock():
            result = account.cancel(intent["intent_id"], "operator_request", "Synthetic demo cancel.", journal=self.journal)
        self.assertEqual((result["status"], result["submitted"], result["executed"]), ("cancelled", False, False))
        state = self.state()
        record = state["intents"][0]
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["reservation"]["release_reason"], "operator_request")
        self.assertEqual(record["reservation"]["release_note"], "Synthetic demo cancel.")
        self.assertEqual(self.account().inspect()["reserved_by_symbol"], {})
        self.assertEqual(state["ledger"]["cancelled_intents"], 1)
        self.assertEqual(state["trading_day"]["authorized_count"], 1)   # authorizations are not un-counted
        events = self.journal.read_run(result["run_id"])
        self.assertEqual((events[0]["stage"], events[0]["reason_codes"]), ("intent_cancelled", ["operator_request"]))
        with account.lock():
            self.assertCode("intent_not_cancellable", account.cancel, intent["intent_id"], "operator_request")
            self.assertCode("intent_not_found", account.cancel, "pint-" + "0" * 24, "operator_request")
            self.assertCode("invalid_cancel_reason", account.cancel, intent["intent_id"], "Bad Reason!")
            self.assertCode("invalid_cancel_note", account.cancel, intent["intent_id"], "x", "<script>")
        self.assertEqual(self.authorize()[0]["reason_codes"], ["duplicate_signal"])   # cancelled != reusable
        position = deepcopy(self.portfolio)
        position["positions"] = [{"symbol": "SYNTH1", "quantity": "25", "average_price": "48.00"}]
        self.assertEqual(self.authorize(self.signal_with("sig-after-cancel"), portfolio=position)[1]["status"],
                         "authorized_paper")

    def test_cancel_requires_lock(self):
        _, intent = self.authorize()
        self.assertCode("account_not_locked", self.account().cancel, intent["intent_id"], "operator_request")


class ConcurrencyTests(Base):
    def run_threads(self, signals):
        results, errors = [], []
        barrier = threading.Barrier(len(signals))

        def worker(signal):
            try:
                barrier.wait()
                results.append(self.authorize(signal, account=self.account(lock_timeout=20))[1]["status"])
            except Exception as error:     # noqa: BLE001 - surfaced through the assertion below
                errors.append(error)
        threads = [threading.Thread(target=worker, args=(s,)) for s in signals]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        return sorted(results)

    def test_same_signal_authorized_once(self):
        statuses = self.run_threads([deepcopy(self.signal) for _ in range(4)])
        self.assertEqual(statuses, ["authorized_paper", "blocked", "blocked", "blocked"])
        self.assertEqual(self.state()["ledger"]["authorized_paper_intents"], 1)

    def test_competing_signals_cannot_both_use_exposure(self):
        signals = [self.signal_with(f"sig-race-{n:04d}", quantity="20") for n in range(3)]   # 1000 each, cap 2000
        statuses = self.run_threads(signals)
        self.assertEqual(statuses.count("authorized_paper"), 2)
        self.assertEqual(self.account().inspect()["reserved_by_symbol"], {"SYNTH1": "40"})

    def test_busy_account_authorizes_nothing(self):
        holder = self.account()
        with holder.lock():
            self.assertCode("account_busy", self.authorize, account=self.account(lock_timeout=0))
        self.assertEqual(self.state()["revision"], 0)


class RolloverTests(Base):
    def test_trading_date_uses_new_york_midnight(self):
        self.assertEqual(trading_date_for("2026-01-16T04:59:59Z", "America/New_York"), "2026-01-15")
        self.assertEqual(trading_date_for("2026-01-16T05:00:00Z", "America/New_York"), "2026-01-16")
        self.assertEqual(trading_date_for("2026-07-16T03:59:59Z", "America/New_York"), "2026-07-15")   # EDT
        self.assertEqual(trading_date_for("2026-07-16T04:00:00Z", "America/New_York"), "2026-07-16")

    def next_day(self, signal_id, at="2026-01-16T15:00:00Z", **kwargs):
        portfolio = deepcopy(self.portfolio)
        portfolio["day"]["trading_date"] = "2026-01-16"
        signal = self.signal_with(signal_id, created="2026-01-16T14:59:30Z", expires="2026-01-16T15:29:30Z", **kwargs)
        return self.authorize(signal, as_of=at, portfolio=portfolio)

    def test_rollover_resets_counters_but_not_duplicates_or_reservations(self):
        for number in range(5):
            self.authorize(self.signal_with(f"sig-roll-{number:04d}", quantity="1"))
        self.assertEqual(self.state()["trading_day"]["authorized_count"], 5)
        decision, intent = self.next_day("sig-roll-next", quantity="1")
        self.assertEqual(intent["status"], "authorized_paper")
        state = self.state()
        self.assertEqual(state["trading_day"]["current_date"], "2026-01-16")
        self.assertEqual(state["trading_day"]["authorized_count"], 1)
        self.assertEqual(state["day_history"], [{"date": "2026-01-15", "authorized_count": 5, "cancelled_count": 0,
                                                 "blocked_count": 0}])
        self.assertEqual(self.account().inspect()["reserved_by_symbol"], {"SYNTH1": "6"})
        decision, intent = self.next_day("sig-roll-0000", quantity="1")
        self.assertEqual(decision["reason_codes"], ["duplicate_signal"])

    def test_clock_regression_blocks(self):
        self.next_day("sig-future-0001")
        decision, intent = self.authorize(self.signal_with("sig-past-0001"))
        self.assertEqual(intent["status"], "blocked")
        self.assertIn("clock_regression", decision["reason_codes"])
        self.assertEqual(self.state()["trading_day"]["current_date"], "2026-01-16")

    def test_portfolio_day_must_match_account_trading_day(self):
        late = "2026-01-16T03:00:00Z"            # still 2026-01-15 in New York
        portfolio = deepcopy(self.portfolio)
        decision, intent = self.authorize(self.signal_with("sig-late-0001", created="2026-01-16T02:59:30Z",
                                                           expires="2026-01-16T03:29:30Z"), as_of=late, portfolio=portfolio)
        self.assertEqual(intent["status"], "authorized_paper")


class CorruptionTests(Base):
    def path(self, name="state.json"):
        return self.root / "runtime/trading/accounts/acct-test-acct" / name

    def tamper(self, change):
        state = json.loads(self.path().read_text(encoding="utf-8"))
        change(state)
        self.path().write_text(json.dumps(state), encoding="utf-8")

    def assertBlocked(self, code):
        self.assertCode(code, self.authorize)
        self.assertCode(code, demo.run_demo, "allowed", account="test-acct", root=self.root)
        account = self.account()
        with account.lock():
            if code != "state_recovery_required":
                self.assertCode(code, account.recover, self.journal)

    def test_unreadable_state(self):
        self.path().write_text("{not json", encoding="utf-8")
        self.assertBlocked("state_corrupt")

    def test_hash_mismatch(self):
        self.authorize()
        self.tamper(lambda s: s["trading_day"].update(authorized_count=0))
        self.assertBlocked("state_corrupt")

    def test_inconsistent_but_resealed_state(self):
        from vicekrack.trading.state import _seal
        self.authorize()
        self.tamper(lambda s: (s["trading_day"].update(authorized_count=0), _seal(s)))
        self.assertBlocked("state_corrupt")
        self.tamper(lambda s: (s["trading_day"].update(authorized_count=1), s["processed_signals"].clear(), _seal(s)))
        self.assertBlocked("state_corrupt")

    def test_incompatible_version(self):
        self.tamper(lambda s: s.update(version="2.0"))
        self.assertBlocked("state_incompatible")

    def test_missing_state_file(self):
        self.path().unlink()
        self.assertBlocked("state_missing")

    def test_credentials_in_state_rejected(self):
        self.tamper(lambda s: s.update(api_key="x"))
        self.assertBlocked("sensitive_state")

    def test_errors_are_sanitized(self):
        self.path().write_text("{not json", encoding="utf-8")
        error = self.assertCode("state_corrupt", self.authorize)
        self.assertNotIn(str(self.root), str(error))
        self.assertNotIn("json", str(error).lower().replace("state", ""))


class InterruptedWriteTests(Base):
    def pending(self):
        return self.root / "runtime/trading/accounts/acct-test-acct/pending.json"

    def recover(self):
        account = self.account()
        with account.lock():
            return account.recover(self.journal)

    def test_journal_failure_rolls_back_and_never_reuses_signal(self):
        with patch.object(TradingJournal, "append", side_effect=TradingError("journal_write_failed", "x")):
            self.assertCode("journal_write_failed", self.authorize)
        self.assertTrue(self.pending().exists())
        self.assertEqual(self.state()["revision"], 0)
        self.assertCode("state_recovery_required", self.authorize, self.signal_with("sig-other-0001"))
        self.assertTrue(self.account().inspect()["recovery_required"])
        result = self.recover()
        self.assertEqual(result["status"], "rolled_back")
        self.assertFalse(self.pending().exists())
        state = self.state()
        self.assertEqual(state["processed_signals"][0]["outcome"], "rolled_back")
        self.assertEqual(state["intents"], [])
        self.assertEqual(self.authorize()[0]["reason_codes"], ["duplicate_signal"])
        self.assertEqual(self.authorize(self.signal_with("sig-other-0001"))[1]["status"], "authorized_paper")

    def test_state_write_failure_after_journal(self):
        real_replace = os.replace

        def fail_state(source, target):
            if str(target).endswith("state.json"):
                raise OSError("disk full /private/path")
            return real_replace(source, target)
        with patch("vicekrack.trading.state.os.replace", side_effect=fail_state):
            error = self.assertCode("state_write_failed", self.authorize)
        self.assertNotIn("/private/path", str(error))
        self.assertEqual(self.state()["intents"], [])                 # journal says recorded, state does not
        self.assertFalse(list(self.pending().parent.glob("*.tmp")))
        result = self.recover()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.authorize()[0]["reason_codes"], ["duplicate_signal"])
        self.assertEqual(self.state()["ledger"]["authorized_paper_intents"], 0)

    def test_crash_after_state_commit(self):
        real_unlink = Path.unlink

        def fail_marker(path, *args, **kwargs):
            if path.name == "pending.json":
                raise OSError("crash")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_marker):
            self.assertCode("state_write_failed", self.authorize)
        self.assertEqual(self.state()["ledger"]["authorized_paper_intents"], 1)
        self.assertCode("state_recovery_required", self.authorize, self.signal_with("sig-other-0001"))
        result = self.recover()
        self.assertEqual(result["status"], "committed")
        self.assertEqual(self.state()["ledger"]["authorized_paper_intents"], 1)     # not authorized twice
        self.assertEqual(self.recover()["status"], "clean")
        self.assertEqual(self.authorize()[0]["reason_codes"], ["duplicate_signal"])

    def test_recovery_is_idempotent_after_crash_during_recovery(self):
        with patch.object(TradingJournal, "append", side_effect=TradingError("journal_write_failed", "x")):
            self.assertCode("journal_write_failed", self.authorize)
        real_unlink = Path.unlink

        def fail_marker(path, *args, **kwargs):
            if path.name == "pending.json":
                raise OSError("crash")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_marker):
            self.assertCode("state_write_failed", self.recover)
        self.assertEqual(self.recover()["status"], "already_recovered")
        self.assertEqual(len(self.state()["recoveries"]), 1)

    def test_unreadable_marker_reconciles_journal(self):
        with patch("vicekrack.trading.state.os.replace", side_effect=OSError("x")):
            self.assertCode("state_write_failed", self.authorize)
        self.pending().write_text("{garbage", encoding="utf-8")
        (self.pending().parent / "leftover.tmp").write_text("partial", encoding="utf-8")
        result = self.recover()
        self.assertEqual(result["status"], "discarded_unreadable")
        self.assertEqual(result["reconciled_signal_ids"], [self.signal["signal_id"]])
        self.assertFalse((self.pending().parent / "leftover.tmp").exists())
        self.assertEqual(self.authorize()[0]["reason_codes"], ["duplicate_signal"])

    def test_recovery_conflict_changes_nothing(self):
        with patch.object(TradingJournal, "append", side_effect=TradingError("journal_write_failed", "x")):
            self.assertCode("journal_write_failed", self.authorize)
        marker = json.loads(self.pending().read_text(encoding="utf-8"))
        marker.update(revision_before=7, revision_after=8)
        self.pending().write_text(json.dumps(marker), encoding="utf-8")
        before = self.state()
        self.assertCode("state_recovery_conflict", self.recover)
        self.assertEqual(self.state(), before)
        self.assertTrue(self.pending().exists())

    def test_clean_recover(self):
        self.assertEqual(self.recover()["status"], "clean")


class KillSwitchTests(Base):
    def test_kill_switch_enforced_and_recorded(self):
        set_kill_switch(True, AS_OF, self.root)
        result = demo.run_demo("allowed", account="test-acct", root=self.root, clock=lambda: AS_OF)
        self.assertEqual(result["intents"][0]["reason_codes"], ["kill_switch_engaged"])
        self.assertEqual(result["account"]["reserved_by_symbol"], {})
        set_kill_switch(False, AS_OF, self.root)
        again = demo.run_demo("allowed", account="test-acct", root=self.root, clock=lambda: AS_OF)
        self.assertEqual(again["intents"][0]["reason_codes"], ["duplicate_signal"])   # blocked signal stays processed

    def test_unreadable_switch_blocks(self):
        switch_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        switch_path(self.root).write_text("?", encoding="utf-8")
        decision, intent = self.authorize()
        self.assertEqual(decision["reason_codes"], ["kill_switch_unreadable"])

    def test_demo_pending_exposure_after_allowed(self):
        demo.run_demo("allowed", account="test-acct", root=self.root, clock=lambda: AS_OF)
        result = demo.run_demo("pending-exposure", account="test-acct", root=self.root, clock=lambda: AS_OF)
        self.assertEqual(result["intents"][0]["reason_codes"], ["position_exposure_exceeded"])
        self.assertEqual(result["account"]["reserved_by_symbol"], {"SYNTH1": "10"})


if __name__ == "__main__":
    unittest.main()
