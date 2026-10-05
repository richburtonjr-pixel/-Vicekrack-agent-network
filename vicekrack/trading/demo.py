"""Bounded offline paper-trading demo.

Loads one clearly labelled synthetic scenario from examples/trading/, validates the
synthetic market snapshot, passes each synthetic signal (at most three) through the
deterministic risk engine against a persistent paper account (Step 24) and records an
allowed or blocked paper order intent in the local journal. Processed signals, daily
counters and pending reservations persist across runs. No network, no feeds, no broker,
no loops: one pass and it stops.
Decisions use the scenario's simulated clock (`as_of`); journal timestamps use the real clock.
"""

import json

from . import PAPER_ONLY_NOTICE
from .config import kill_switch_state, load_config
from .contracts import ROOT, parse_time, reject_trading_secrets, utc_now, validate_portfolio, validate_signal, validate_snapshot
from .errors import TradingError
from .journal import TradingJournal
from .risk import evaluate
from .state import PaperAccount

SCENARIOS = ("allowed", "exposure-breach", "daily-loss", "stale-data", "invalid-money", "duplicate-signal", "pending-exposure")
DEFAULT_ACCOUNT = "paper-demo"
SCENARIO_KEYS = {"fixture", "version", "scenario", "description", "synthetic", "as_of", "snapshot", "signals",
                 "portfolio"}
MAX_SIGNALS = 3


def load_scenario(name):
    if name not in SCENARIOS:
        raise TradingError("unknown_scenario", "Unknown trading demo scenario.")
    try:
        scenario = json.loads((ROOT / "examples/trading" / f"scenario-{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_scenario", "Cannot read the synthetic scenario fixture.") from None
    if (not isinstance(scenario, dict) or set(scenario) != SCENARIO_KEYS
            or scenario["fixture"] != "synthetic_trading_scenario" or scenario["version"] != "1.1"
            or scenario["synthetic"] is not True or scenario["scenario"] != name
            or not isinstance(scenario["signals"], list) or not 1 <= len(scenario["signals"]) <= MAX_SIGNALS
            or not isinstance(scenario["as_of"], str)):
        raise TradingError("invalid_scenario", "The synthetic scenario fixture has an invalid structure.")
    reject_trading_secrets(scenario)
    return scenario


def _try(validator, document, *args):
    try:
        validator(document, *args)
        return None
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        return error.code


def run_demo(name, *, account=DEFAULT_ACCOUNT, root=None, config_path="config/trading.paper.json", clock=utc_now):
    """One bounded pass over a synthetic scenario against a persistent paper account.

    The account must already exist (`trading-state init`). The whole run holds the account
    lock; each valid signal is authorized or blocked by `PaperAccount.authorize`."""
    root = root if root is not None else ROOT
    scenario = load_scenario(name)
    as_of = scenario["as_of"]
    try:
        parse_time(as_of)
    except ValueError:
        raise TradingError("invalid_scenario", "The scenario clock is not a UTC timestamp.") from None

    paper = PaperAccount(account, root=root, clock=clock)
    with paper.lock():
        paper.load()                    # missing, corrupt, incompatible or unresolved state blocks here
        config_problem = None
        try:
            config, config_sha = load_config(config_path)
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            config, config_sha, config_problem = None, None, error.code
        switch = kill_switch_state(config, root)

        journal = TradingJournal(root)
        run_id = journal.start_run()
        started = journal.append(run_id, stage="run_started", status="started", agent="trading_demo", recorded_at=clock(),
                                 subject={"scenario": name, "account_id": paper.account_id},
                                 observed={"simulated_as_of": as_of, "kill_switch_engaged": switch[0],
                                           "kill_switch_source": switch[1], "config_valid": config is not None,
                                           "signals": len(scenario["signals"])},
                                 reason_codes=[config_problem] if config_problem else [])

        snapshot = scenario["snapshot"]
        snapshot_problem = _try(validate_snapshot, snapshot)
        snap_event = journal.append(
            run_id, stage="market_snapshot", status="rejected" if snapshot_problem else "accepted", agent="demo_market_data",
            recorded_at=clock(), causation_id=started["event_id"],
            subject=_subject(snapshot, "symbol", "snapshot_id"),
            observed={} if snapshot_problem else {"last": snapshot["price"]["last"], "bid": snapshot["price"]["bid"],
                                                  "ask": snapshot["price"]["ask"], "observed_at": snapshot["observed_at"],
                                                  "source_kind": snapshot["source"]["kind"]},
            reason_codes=[snapshot_problem] if snapshot_problem else [], document=None if snapshot_problem else snapshot)
        if snapshot_problem:
            snapshot = None

        portfolio = scenario["portfolio"]
        portfolio_problem = _try(validate_portfolio, portfolio)
        if portfolio_problem:
            portfolio = None

        intents = []
        for signal in scenario["signals"]:
            signal_problem = _try(validate_signal, signal, [snapshot] if snapshot is not None else [])
            sig_event = journal.append(
                run_id, stage="signal", status="rejected" if signal_problem else "accepted", agent="demo_strategy",
                recorded_at=clock(), causation_id=snap_event["event_id"], subject=_subject(signal, "symbol", "signal_id"),
                observed={} if signal_problem else {"strategy": signal["strategy"]["strategy_id"],
                                                    "side": signal["proposal"]["side"],
                                                    "quantity": signal["proposal"]["quantity"],
                                                    "order_type": signal["proposal"]["order_type"],
                                                    "expires_at": signal["expires_at"],
                                                    "observations_checked": snapshot is not None,
                                                    "rationale": signal["rationale"]["summary"][:200]},
                reason_codes=[signal_problem] if signal_problem else list(signal["rationale"]["codes"]),
                document=None if signal_problem else signal)
            problems = [p for p in (config_problem, snapshot_problem, portfolio_problem, signal_problem) if p]
            if signal_problem:
                # No valid signal ID to protect: block without touching account state.
                decision = evaluate(config=config, config_sha256=config_sha, snapshot=snapshot, signal=None,
                                    portfolio=portfolio, as_of=as_of, kill_switch=switch, input_problems=problems)
                journal.append(run_id, stage="risk_check", status="blocked", agent="risk_engine", recorded_at=clock(),
                               causation_id=sig_event["event_id"],
                               subject={"account_id": paper.account_id, "decision_id": decision["decision_id"]},
                               observed={"checks_failed": sum(c["status"] == "fail" for c in decision["checks"]),
                                         "state_changed": False},
                               reason_codes=decision["reason_codes"], document=decision)
                intents.append({"intent_id": None, "status": "not_created", "signal_valid": False,
                                "reason_codes": decision["reason_codes"]})
                continue
            decision, intent = paper.authorize(
                signal=signal, snapshot=snapshot, portfolio=portfolio, config=config, config_sha256=config_sha,
                kill_switch=switch, as_of=as_of, input_problems=problems, journal=journal, run_id=run_id,
                causation_id=sig_event["event_id"])
            intents.append({"intent_id": intent["intent_id"], "status": intent["status"], "symbol": intent["symbol"],
                            "side": intent["side"], "quantity": intent["quantity"], "notional": intent["notional"],
                            "reason_codes": decision["reason_codes"],
                            "reservation": "active" if intent["status"] == "authorized_paper" else "none",
                            "submitted": False, "executed": False})

        authorized = sum(i["status"] == "authorized_paper" for i in intents)
        journal.append(run_id, stage="run_finished", status="completed", agent="trading_demo", recorded_at=clock(),
                       causation_id=started["event_id"], subject={"scenario": name, "account_id": paper.account_id},
                       observed={"authorized_paper": authorized, "blocked": len(intents) - authorized, "executed": False})
        view = paper.inspect()
    state = view["state"]
    return {
        "notice": PAPER_ONLY_NOTICE, "scenario": name, "description": scenario["description"], "synthetic": True,
        "simulated_as_of": as_of, "account_id": paper.account_id, "run_id": run_id, "kill_switch_engaged": switch[0],
        "intents": intents, "account": {"revision": state["revision"], "trading_date": state["trading_day"]["current_date"],
                                        "authorized_today": state["trading_day"]["authorized_count"],
                                        "reserved_by_symbol": view["reserved_by_symbol"], "ledger": state["ledger"]},
        "submitted": False, "executed": False,
        "journal": f"runtime/trading/journal/{run_id}",
    }


def _subject(document, *keys):
    if not isinstance(document, dict):
        return {}
    return {k: document[k] for k in keys if isinstance(document.get(k), str) and len(document[k]) <= 80}
