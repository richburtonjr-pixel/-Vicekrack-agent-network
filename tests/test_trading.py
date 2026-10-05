"""Step 23: paper-trading foundation. Synthetic fixtures only; no network, no broker."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from vicekrack.trading import contracts, demo
from vicekrack.trading.cli import main as trading_main
from vicekrack.trading.config import kill_switch_state, load_config, set_kill_switch, switch_path, validate_config
from vicekrack.trading.contracts import (ROOT, sha256, snapshot_age_problem, validate_journal_event, validate_order_intent,
                                         validate_portfolio, validate_risk_decision, validate_signal, validate_snapshot)
from vicekrack.trading.errors import TradingError
from vicekrack.trading.journal import TradingJournal, summarize
from vicekrack.trading.money import fmt, multiply, parse
from vicekrack.trading.orders import build_intent
from vicekrack.trading.risk import evaluate

AS_OF = "2026-01-15T15:00:00Z"


def fixture(name="allowed"):
    return json.loads((ROOT / "examples/trading" / f"scenario-{name}.json").read_text(encoding="utf-8"))


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config, self.config_sha = load_config()
        scenario = fixture()
        self.snapshot, self.signal, self.portfolio = scenario["snapshot"], scenario["signals"][0], scenario["portfolio"]

    def decide(self, **overrides):
        values = dict(config=self.config, config_sha256=self.config_sha, snapshot=self.snapshot, signal=self.signal,
                      portfolio=self.portfolio, as_of=AS_OF, kill_switch=(False, None))
        values.update(overrides)
        return evaluate(**values)

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class MoneyTests(unittest.TestCase):
    def test_exact_decimal_parsing_and_formatting(self):
        self.assertEqual(parse("0.1") + parse("0.2"), Decimal("0.3"))
        self.assertEqual(fmt(multiply(parse("10"), parse("50.00"))), "500")
        self.assertEqual(fmt(multiply(parse("0.00000003"), parse("0.5"))), "0.00000002")  # banker's rounding
        self.assertEqual(fmt(parse("123456789012345678.12345678")), "123456789012345678.12345678")
        self.assertEqual(parse("-12.50", "signed_money"), Decimal("-12.5"))

    def test_invalid_monetary_values_rejected(self):
        for bad in (50.0, 50, True, None, "1e3", "NaN", "Infinity", "-1", "01", "1.", ".5", " 1", "1,000",
                    "1.123456789", "1234567890123456789"):
            with self.subTest(bad=bad), self.assertRaises(TradingError):
                parse(bad)
        for bad in ("1.234", "-5.00", "12345678901234567"):
            with self.subTest(bad=bad), self.assertRaises(TradingError):
                parse(bad, "money")


class ContractTests(Base):
    def test_valid_contracts(self):
        validate_snapshot(self.snapshot)
        validate_signal(self.signal, [self.snapshot])
        validate_portfolio(self.portfolio)
        decision = self.decide()
        validate_risk_decision(decision)
        intent = build_intent(self.signal, decision, self.snapshot, AS_OF)
        validate_order_intent(intent)
        for name in demo.SCENARIOS:
            self.assertTrue(fixture(name)["synthetic"])
            self.assertTrue(fixture(name)["description"].startswith("SYNTHETIC FIXTURE"))

    def test_schemas_are_versioned(self):
        for path in (ROOT / "schemas/trading").glob("*.schema.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(schema["properties"].get("version", schema["properties"].get("config_version"))["const"], "1.0")
            self.assertFalse(schema["additionalProperties"])

    def test_malformed_snapshots(self):
        cases = {
            "float price": lambda s: s["price"].__setitem__("last", 50.0),
            "exponent": lambda s: s["price"].__setitem__("bid", "4.998e1"),
            "zero price": lambda s: s["price"].__setitem__("last", "0"),
            "crossed": lambda s: s["price"].update(bid="50.10", ask="50.00"),
            "lower symbol": lambda s: s.__setitem__("symbol", "synth1"),
            "bad time": lambda s: s.__setitem__("observed_at", "2026-01-15 14:59:00"),
            "extra field": lambda s: s.__setitem__("broker", "x"),
            "not synthetic": lambda s: s.__setitem__("synthetic", False),
            "live source": lambda s: s["source"].__setitem__("kind", "live_feed"),
            "missing volume": lambda s: s.pop("volume"),
            "wrong version": lambda s: s.__setitem__("version", "2.0"),
        }
        for label, mutate in cases.items():
            snapshot = deepcopy(self.snapshot)
            mutate(snapshot)
            with self.subTest(label), self.assertRaises(TradingError):
                validate_snapshot(snapshot)

    def test_stale_and_future_snapshots(self):
        self.assertIsNone(snapshot_age_problem(self.snapshot, AS_OF, 300))
        stale = dict(self.snapshot, observed_at="2026-01-15T14:54:59Z")
        self.assertEqual(snapshot_age_problem(stale, AS_OF, 300), "snapshot_stale")
        self.assertEqual(snapshot_age_problem(self.snapshot, AS_OF, 30), "snapshot_stale")  # config tighter
        future = dict(self.snapshot, observed_at="2026-01-15T15:01:00Z")
        self.assertEqual(snapshot_age_problem(future, AS_OF, 300), "snapshot_from_future")

    def test_malformed_signals(self):
        def mutate(change):
            signal = deepcopy(self.signal)
            change(signal)
            return signal
        self.assertCode("invalid_trading_signal", validate_signal, mutate(lambda s: s.__setitem__("expires_at", s["created_at"])))
        self.assertCode("invalid_trading_signal", validate_signal, mutate(lambda s: s["proposal"].__setitem__("quantity", "0")))
        self.assertCode("invalid_trading_signal", validate_signal, mutate(lambda s: s["proposal"].__setitem__("order_type", "limit")))
        self.assertCode("invalid_trading_signal", validate_signal, mutate(lambda s: s["proposal"].__setitem__("limit_price", "49.00")))
        self.assertCode("invalid_trading_signal", validate_signal, mutate(lambda s: s.__setitem__("observations", [])))
        self.assertCode("invalid_trading_signal", validate_signal, mutate(lambda s: s["rationale"].__setitem__("codes", [])))
        self.assertCode("signal_observation_mismatch", validate_signal,
                        mutate(lambda s: s["observations"][0].__setitem__("value", "51.00")), [self.snapshot])
        self.assertCode("signal_observation_unknown", validate_signal,
                        mutate(lambda s: s["observations"][0].__setitem__("snapshot_id", "snap-other-0001")), [self.snapshot])
        validate_signal(mutate(lambda s: s["observations"][0].__setitem__("value", "50")), [self.snapshot])  # exact equality

    def test_portfolio_and_decision_rules(self):
        duplicate = deepcopy(self.portfolio)
        duplicate["positions"] = [{"symbol": "SYNTH1", "quantity": "1", "average_price": "1"}] * 2
        self.assertCode("invalid_paper_portfolio_state", validate_portfolio, duplicate)
        decision = self.decide()
        forged = dict(decision, outcome="allowed", reason_codes=["daily_loss_limit_reached"])
        self.assertCode("invalid_risk_decision", validate_risk_decision, forged)
        blocked = self.decide(kill_switch=(True, "config"))
        self.assertCode("invalid_risk_decision", validate_risk_decision, dict(blocked, outcome="allowed", reason_codes=[]))

    def test_intent_never_executes(self):
        intent = build_intent(self.signal, self.decide(), self.snapshot, AS_OF)
        self.assertEqual(intent["execution"], {"submitted": False, "executed": False, "broker": None})
        for change in ({"submitted": True, "executed": False, "broker": None},
                       {"submitted": False, "executed": True, "broker": None},
                       {"submitted": False, "executed": False, "broker": "any"}):
            with self.subTest(change), self.assertRaises(TradingError):
                validate_order_intent(dict(intent, execution=change))
        self.assertCode("invalid_paper_order_intent", validate_order_intent, dict(intent, mode="live"))
        self.assertCode("invalid_paper_order_intent", validate_order_intent, dict(intent, simulated=False))

    def test_credentials_rejected(self):
        for key in ("api_key", "apiSecret", "broker_password", "private_key", "access_token", "account_number", "passphrase"):
            with self.subTest(key):
                self.assertCode("sensitive_state", validate_snapshot, dict(self.snapshot, **{key: "x"}))
        shaped = deepcopy(self.signal)
        shaped["rationale"]["summary"] = "AKIA" + "ABCDEFGHIJKLMNOP"  # built at runtime; not a real key
        self.assertCode("sensitive_state", validate_signal, shaped)
        with patch.dict(os.environ, {"BROKER_API_TOKEN": "zz-secret-value-123"}):
            leaked = deepcopy(self.signal)
            leaked["rationale"]["summary"] = "note zz-secret-value-123"
            error = self.assertCode("sensitive_state", validate_signal, leaked)
            self.assertNotIn("zz-secret-value-123", str(error))
        validate_snapshot(self.snapshot)  # our own risk-/pint- IDs are not mistaken for keys

    def test_error_messages_do_not_echo_input(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["symbol"] = "UNIQUE-<script>"
        with self.assertRaises(TradingError) as caught:
            validate_snapshot(snapshot)
        self.assertNotIn("<script>", str(caught.exception))


class ConfigTests(Base):
    def test_shipped_config_is_paper_only(self):
        self.assertEqual(self.config["mode"], "paper")
        self.assertFalse(self.config["allow_short"])
        self.assertFalse(self.config["kill_switch"]["engaged"])

    def test_invalid_limits_rejected(self):
        cases = [("max_daily_loss", 200.0), ("max_daily_loss", "-1"), ("max_order_notional", "1e3"),
                 ("max_position_fraction", "0"), ("max_position_fraction", "1.5"), ("max_quantity", "0"),
                 ("max_order_notional", "3000.00"), ("max_orders_per_day", 0)]
        for key, value in cases:
            config = deepcopy(self.config)
            config["limits"][key] = value
            with self.subTest(key=key, value=value):
                self.assertCode("invalid_paper_config", validate_config, config)
        for change in ({"mode": "live"}, {"allow_short": True}, {"broker": "x"}):
            with self.subTest(change):
                self.assertRaises(TradingError, validate_config, dict(deepcopy(self.config), **change))
        missing = deepcopy(self.config)
        del missing["limits"]["max_daily_loss"]
        self.assertRaises(TradingError, validate_config, missing)

    def test_config_path_must_stay_in_project(self):
        self.assertCode("invalid_paper_config", load_config, "../outside.json")
        self.assertCode("invalid_paper_config", load_config, "config/missing-trading.json")

    def test_kill_switch_sources(self):
        self.assertEqual(kill_switch_state(self.config, self.root), (False, None))
        self.assertEqual(kill_switch_state(None, self.root), (True, "config_unavailable"))
        engaged = deepcopy(self.config)
        engaged["kill_switch"]["engaged"] = True
        self.assertEqual(kill_switch_state(engaged, self.root), (True, "config"))
        set_kill_switch(True, AS_OF, self.root)
        self.assertEqual(kill_switch_state(self.config, self.root), (True, "switch_file"))
        set_kill_switch(False, AS_OF, self.root)
        self.assertEqual(kill_switch_state(self.config, self.root), (False, None))
        self.assertEqual(kill_switch_state(engaged, self.root), (True, "config"))  # file release can't override config
        switch_path(self.root).write_text("{not json", encoding="utf-8")
        self.assertEqual(kill_switch_state(self.config, self.root), (True, "switch_file_unreadable"))
        switch_path(self.root).write_text('{"engaged": "no"}', encoding="utf-8")
        self.assertEqual(kill_switch_state(self.config, self.root)[0], True)


class RiskTests(Base):
    def test_allowed_when_every_check_passes(self):
        decision = self.decide()
        self.assertEqual(decision["outcome"], "allowed")
        self.assertTrue(all(c["status"] == "pass" for c in decision["checks"]))
        self.assertEqual(decision, self.decide())  # deterministic
        self.assertTrue(decision["decision_id"].startswith("rdec-"))

    def blocked(self, code, **overrides):
        decision = self.decide(**overrides)
        self.assertEqual(decision["outcome"], "blocked")
        self.assertIn(code, decision["reason_codes"])
        intent = build_intent(overrides.get("signal", self.signal), decision, self.snapshot, AS_OF) \
            if overrides.get("signal", self.signal) is not None else None
        if intent:
            self.assertEqual(intent["status"], "blocked")
            self.assertIsNone(intent["notional"])
        return decision

    def test_kill_switch_blocks(self):
        self.blocked("kill_switch_engaged", kill_switch=(True, "switch_file"))
        self.blocked("kill_switch_unreadable", kill_switch=(True, "switch_file_unreadable"))

    def test_missing_or_invalid_inputs_block(self):
        for name in ("config", "snapshot", "signal", "portfolio"):
            with self.subTest(name):
                decision = self.blocked("invalid_or_missing_inputs", **{name: None})
                self.assertIn(f"missing_{name}", decision["reason_codes"])
                self.assertTrue(all(c["status"] == "skipped" for c in decision["checks"][2:]))
        self.blocked("invalid_market_snapshot", input_problems=["invalid_market_snapshot"])

    def test_limit_breaches(self):
        def signal(**proposal):
            changed = deepcopy(self.signal)
            changed["proposal"].update(proposal)
            return changed
        self.blocked("quantity_limit_exceeded", signal=signal(quantity="101"))
        self.blocked("order_notional_exceeded", signal=signal(quantity="20.00000001"))
        self.decide(signal=signal(quantity="20"))  # exactly at the limit: 1000.00
        self.assertEqual(self.decide(signal=signal(quantity="20"))["outcome"], "allowed")
        position = deepcopy(self.portfolio)
        position["positions"] = [{"symbol": "SYNTH1", "quantity": "31", "average_price": "48.00"}]
        self.blocked("position_exposure_exceeded", portfolio=position)
        small = deepcopy(self.portfolio)
        small["equity"] = "4000.00"
        self.blocked("position_fraction_exceeded", portfolio=small)
        self.blocked("short_selling_not_allowed", signal=signal(side="sell"))
        loss = deepcopy(self.portfolio)
        loss["day"]["pnl"] = "-200.00"
        self.blocked("daily_loss_limit_reached", portfolio=loss)
        loss["day"]["pnl"] = "-199.99"
        self.assertEqual(self.decide(portfolio=loss)["outcome"], "allowed")
        old_day = deepcopy(self.portfolio)
        old_day["day"]["trading_date"] = "2026-01-14"
        self.blocked("portfolio_day_mismatch", portfolio=old_day)
        self.blocked("orders_per_day_exceeded", orders_today=5)
        self.blocked("duplicate_signal", used_signal_ids=[self.signal["signal_id"]])
        self.blocked("symbol_not_allowed", config=dict(self.config, allowed_symbols=["SYNTH2"]))
        self.blocked("order_type_not_allowed", config=dict(self.config, allowed_order_types=["limit"]))

    def test_limit_orders_use_limit_price(self):
        changed = deepcopy(self.signal)
        changed["proposal"].update(order_type="limit", limit_price="60.00", quantity="17")
        self.blocked("order_notional_exceeded", signal=changed)  # 17 * 60 = 1020 > 1000 (17 * 50 would pass)

    def test_time_checks(self):
        self.blocked("snapshot_stale", as_of="2026-01-15T15:05:01Z")
        self.blocked("signal_expired", as_of="2026-01-15T15:29:30Z")
        self.blocked("signal_from_future", as_of="2026-01-15T14:59:20Z")
        long = deepcopy(self.signal)
        long["expires_at"] = "2026-01-15T16:59:31Z"
        self.blocked("signal_lifetime_exceeded", signal=long)


class JournalTests(Base):
    def event(self, journal, run, **extra):
        values = dict(stage="run_started", status="started", agent="trading_demo", recorded_at=AS_OF)
        values.update(extra)
        return journal.append(run, **values)

    def test_append_and_read(self):
        journal = TradingJournal(self.root)
        run = journal.start_run()
        first = self.event(journal, run)
        self.event(journal, run, stage="market_snapshot", status="accepted", agent="demo_market_data",
                   document=self.snapshot, causation_id=first["event_id"])
        events = journal.read_run(run)
        self.assertEqual([e["sequence"] for e in events], [1, 2])
        self.assertEqual(events[1]["document_sha256"], sha256(self.snapshot))
        self.assertEqual(journal.list_runs()[0]["run_id"], run)
        self.assertFalse(list((self.root / "runtime/trading/journal" / run).glob("*.tmp")))

    def test_duplicate_ids_rejected(self):
        journal = TradingJournal(self.root)
        run = journal.start_run()
        event = self.event(journal, run)
        self.assertCode("journal_duplicate_event", self.event, journal, run, event_id=event["event_id"])
        other = journal.start_run()
        self.assertCode("journal_duplicate_event", self.event, journal, other, event_id=event["event_id"])
        self.assertCode("journal_duplicate_run", TradingJournal(self.root).start_run, run)
        self.assertCode("invalid_run_id", journal.start_run, "run-../../x")
        self.assertEqual(len(journal.read_run(run)), 1)

    def test_write_failure_is_sanitized_and_leaves_nothing(self):
        journal = TradingJournal(self.root)
        run = journal.start_run()
        with patch("vicekrack.trading.journal.os.link", side_effect=OSError("/secret/path disk full")):
            error = self.assertCode("journal_write_failed", self.event, journal, run)
        self.assertNotIn("/secret/path", str(error))
        self.assertEqual(list((self.root / "runtime/trading/journal" / run).iterdir()), [])
        self.event(journal, run)  # sequence was not consumed
        self.assertEqual(journal.read_run(run)[0]["sequence"], 1)

    def test_invalid_events_are_not_written(self):
        journal = TradingJournal(self.root)
        run = journal.start_run()
        self.assertRaises(TradingError, self.event, journal, run, stage="run_started", document=self.snapshot)
        self.assertRaises(TradingError, self.event, journal, run, agent="live_broker")
        self.assertCode("sensitive_state", self.event, journal, run, observed={"api_key": "x"})
        self.assertCode("journal_unknown_run", TradingJournal(self.root).append, run, stage="run_started",
                        status="started", agent="trading_demo", recorded_at=AS_OF)
        self.assertFalse(any((self.root / "runtime/trading/journal" / run).iterdir()))

    def test_corrupt_and_tampered_journal(self):
        journal = TradingJournal(self.root)
        run = journal.start_run()
        event = self.event(journal, run, stage="market_snapshot", status="accepted", agent="demo_market_data",
                           document=self.snapshot)
        path = next((self.root / "runtime/trading/journal" / run).glob("*.json"))
        tampered = deepcopy(event)
        tampered["document"]["price"]["last"] = "1.00"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertCode("journal_corrupt", journal.read_run, run)
        path.write_text("{", encoding="utf-8")
        self.assertCode("journal_corrupt", journal.read_run, run)
        self.assertFalse(journal.list_runs()[0]["readable"])
        self.assertCode("journal_run_not_found", journal.read_run, "run-" + "0" * 32)


class DemoTests(Base):
    EXPECTED = {"allowed": [("authorized_paper", [])],
                "exposure-breach": [("blocked", ["position_exposure_exceeded"])],
                "daily-loss": [("blocked", ["daily_loss_limit_reached"])],
                "stale-data": [("blocked", ["snapshot_stale"])],
                "invalid-money": [("blocked", ["invalid_or_missing_inputs", "invalid_market_snapshot"])],
                "duplicate-signal": [("authorized_paper", []), ("blocked", ["duplicate_signal"])]}

    def test_every_scenario(self):
        for name, expected in self.EXPECTED.items():
            with self.subTest(name):
                result = demo.run_demo(name, root=self.root, clock=lambda: AS_OF)
                self.assertEqual([(i["status"], i["reason_codes"]) for i in result["intents"]], expected)
                self.assertFalse(result["executed"])
                self.assertFalse(result["submitted"])
                self.assertIn("SIMULATED", result["notice"])
                events = TradingJournal(self.root).read_run(result["run_id"])
                self.assertEqual(events[0]["stage"], "run_started")
                self.assertEqual(events[-1]["stage"], "run_finished")
                self.assertTrue(all(e["simulated"] and e["mode"] == "paper" for e in events))
                risk = [e for e in events if e["stage"] == "risk_check"]
                self.assertEqual(len(risk), len(expected))
                self.assertTrue(all("checks" in item for item in summarize(events) if item["stage"] == "risk_check"))

    def test_kill_switch_blocks_demo(self):
        set_kill_switch(True, AS_OF, self.root)
        result = demo.run_demo("allowed", root=self.root)
        self.assertEqual(result["intents"][0]["status"], "blocked")
        self.assertEqual(result["intents"][0]["reason_codes"], ["kill_switch_engaged"])

    def test_invalid_config_blocks_demo(self):
        with patch.object(demo, "load_config", side_effect=TradingError("invalid_paper_config", "bad")):
            result = demo.run_demo("allowed", root=self.root)
        self.assertEqual(result["intents"][0]["status"], "blocked")
        self.assertIn("invalid_paper_config", result["intents"][0]["reason_codes"])
        self.assertIn("kill_switch_engaged", result["intents"][0]["reason_codes"])

    def test_journal_failure_stops_run(self):
        with patch("vicekrack.trading.journal.os.link", side_effect=OSError("boom")):
            self.assertCode("journal_write_failed", demo.run_demo, "allowed", root=self.root)

    def test_unknown_scenario(self):
        self.assertCode("unknown_scenario", demo.run_demo, "../config", root=self.root)

    def test_cli(self):
        def run(*argv):
            output = io.StringIO()
            with redirect_stdout(output):
                code = trading_main(list(argv), root=self.root)
            return code, json.loads(output.getvalue())
        code, result = run("trading-demo", "--scenario", "allowed")
        self.assertEqual(code, 0)
        self.assertFalse(result["executed"])
        code, listing = run("trading-journal")
        self.assertEqual(listing["runs"][0]["run_id"], result["run_id"])
        code, view = run("trading-journal", result["run_id"])
        self.assertEqual(view["events"][-1]["stage"], "run_finished")
        code, error = run("trading-journal", "run-bad")
        self.assertEqual((code, error["error"]["code"]), (1, "invalid_run_id"))
        self.assertEqual(run("trading-config-check")[1]["mode"], "paper")
        self.assertTrue(run("trading-kill-switch", "engage")[1]["kill_switch_engaged"])
        self.assertEqual(run("trading-demo")[1]["intents"][0]["status"], "blocked")
        self.assertFalse(run("trading-kill-switch", "release")[1]["kill_switch_engaged"])
        self.assertFalse(run("trading-kill-switch", "status")[1]["kill_switch_engaged"])

    def test_no_broker_or_network_code(self):
        source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "vicekrack/trading").glob("*.py"))
        for forbidden in ("import socket", "urllib", "requests", "http.client", "subprocess", "threading", "while True"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
