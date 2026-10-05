"""Versioned trading contracts: JSON Schema + semantic validation.

Contracts (schemas/trading/*.schema.json, all version 1.0): market_snapshot,
trading_signal, paper_portfolio_state, risk_decision, paper_order_intent,
trading_journal_event. Every validator rejects recognized credentials first and raises
TradingError with a fixed code; messages never echo input values.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from ..persistence import reject_secrets
from .errors import TradingError
from .money import parse

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMAS = {
    "market_snapshot": "market-snapshot", "trading_signal": "signal", "paper_portfolio_state": "paper-portfolio",
    "risk_decision": "risk-decision", "paper_order_intent": "paper-order-intent",
    "trading_journal_event": "journal-event", "paper_config": "paper-config",
    "paper_account_state": "paper-account-state", "paper_state_pending": "paper-state-pending",
    "ohlcv_bar": "ohlcv-bar", "market_dataset": "market-dataset", "market_data_config": "market-data-config",
    "market_replay_report": "market-replay-report",
}
FUTURE_SKEW = timedelta(seconds=5)
# Field names that must never appear in trading data (in addition to the shared core list).
CREDENTIAL_FIELDS = ("apikey", "apisecret", "secretkey", "privatekey", "passphrase", "password", "token",
                     "accesstoken", "accountnumber", "brokerkey", "clientsecret", "authorization")
CREDENTIAL_SHAPES = re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-)?[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE)$")


@lru_cache(maxsize=None)
def _validator(contract):
    schema = json.loads((ROOT / "schemas/trading" / f"{SCHEMAS[contract]}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256(document):
    return hashlib.sha256(canonical(document).encode("utf-8")).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def reject_trading_secrets(document):
    """Shared core check plus broker-style credential fields, shapes and active env secrets."""
    try:
        reject_secrets(document)
        encoded = json.dumps(document, allow_nan=False)
    except (TypeError, ValueError):
        raise TradingError("invalid_document", "Trading data must contain finite JSON values.") from None
    except Exception:  # Shared core raises NetworkError('sensitive_state').
        raise TradingError("sensitive_state", "Trading data contains a credential.") from None

    def walk(item):
        if isinstance(item, dict):
            for key, value in item.items():
                normalized = re.sub(r"[^a-z]", "", str(key).lower())
                if any(normalized.endswith(name) for name in CREDENTIAL_FIELDS):
                    raise TradingError("sensitive_state", "Trading data contains a credential field.")
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)
    walk(document)
    if CREDENTIAL_SHAPES.search(encoded):
        raise TradingError("sensitive_state", "Trading data contains a credential-shaped value.")
    for name, value in os.environ.items():
        if SECRET_ENV.search(name.upper()) and len(value.strip()) >= 8 and value.strip() in encoded:
            raise TradingError("sensitive_state", "Trading data contains an active credential value.")


def validate_schema(contract, document):
    reject_trading_secrets(document)
    error = next(_validator(contract).iter_errors(document), None)
    if error is not None:
        location = ".".join(str(p) for p in error.absolute_path) or "$"
        raise TradingError(f"invalid_{contract}", f"{contract} rejected at {location}: schema rule '{error.validator}'.")


def _fail(contract, reason):
    raise TradingError(f"invalid_{contract}", f"{contract} rejected: {reason}.")


# ---------------------------------------------------------------- per-contract validation

def validate_snapshot(snapshot):
    validate_schema("market_snapshot", snapshot)
    last, bid, ask = (parse(snapshot["price"][k]) for k in ("last", "bid", "ask"))
    if last <= 0 or bid <= 0 or ask <= 0:
        _fail("market_snapshot", "prices must be greater than zero")
    if bid > ask:
        _fail("market_snapshot", "bid must not exceed ask")
    parse(snapshot["volume"])
    if snapshot["source"]["kind"] == "synthetic_fixture" and snapshot["synthetic"] is not True:
        _fail("market_snapshot", "synthetic fixtures must be labelled synthetic")


def snapshot_age_problem(snapshot, as_of, config_max_age):
    """None if fresh, else 'snapshot_stale' / 'snapshot_from_future'."""
    age = parse_time(as_of) - parse_time(snapshot["observed_at"])
    if age < -FUTURE_SKEW:
        return "snapshot_from_future"
    if age > timedelta(seconds=min(snapshot["freshness"]["max_age_seconds"], config_max_age)):
        return "snapshot_stale"
    return None


def validate_signal(signal, snapshots=()):
    """Schema + semantics. With snapshots, every observation must exactly match a snapshot value."""
    validate_schema("trading_signal", signal)
    created, expires = parse_time(signal["created_at"]), parse_time(signal["expires_at"])
    if expires <= created:
        _fail("trading_signal", "expiry must be after creation")
    proposal = signal["proposal"]
    if parse(proposal["quantity"]) <= 0:
        _fail("trading_signal", "quantity must be greater than zero")
    if (proposal["order_type"] == "limit") != (proposal["limit_price"] is not None):
        _fail("trading_signal", "a limit price is required for limit orders and only for them")
    if proposal["limit_price"] is not None and parse(proposal["limit_price"]) <= 0:
        _fail("trading_signal", "limit price must be greater than zero")
    for observation in signal["observations"]:
        parse(observation["value"])
    if snapshots:
        by_id = {s["snapshot_id"]: s for s in snapshots}
        for observation in signal["observations"]:
            snapshot = by_id.get(observation["snapshot_id"])
            if snapshot is None or snapshot["symbol"] != signal["symbol"]:
                raise TradingError("signal_observation_unknown", "A signal observation references an unknown snapshot.")
            actual = snapshot["volume"] if observation["field"] == "volume" else snapshot["price"][observation["field"]]
            if parse(actual) != parse(observation["value"]):
                raise TradingError("signal_observation_mismatch", "A signal observation does not match its snapshot.")


def validate_portfolio(portfolio):
    validate_schema("paper_portfolio_state", portfolio)
    parse(portfolio["equity"], "money")
    parse(portfolio["day"]["pnl"], "signed_money")
    symbols = [p["symbol"] for p in portfolio["positions"]]
    if len(symbols) != len(set(symbols)):
        _fail("paper_portfolio_state", "duplicate position symbols")
    for position in portfolio["positions"]:
        if parse(position["quantity"], "signed_decimal") != 0 and parse(position["average_price"]) <= 0:
            _fail("paper_portfolio_state", "open positions need a positive average price")


def validate_risk_decision(decision):
    validate_schema("risk_decision", decision)
    failed = [c["check"] for c in decision["checks"] if c["status"] == "fail"]
    if (decision["outcome"] == "allowed") != (not failed and not decision["reason_codes"]):
        _fail("risk_decision", "allowed only when no check failed and no reason is recorded")
    if decision["outcome"] == "allowed" and any(c["status"] != "pass" for c in decision["checks"]):
        _fail("risk_decision", "an allowed decision needs every check to pass")
    if decision["outcome"] == "allowed" and None in decision["inputs"].values():
        _fail("risk_decision", "an allowed decision needs every input")


def validate_order_intent(intent):
    validate_schema("paper_order_intent", intent)
    if intent["status"] == "authorized_paper":
        if intent["reference_price"] is None or intent["notional"] is None:
            _fail("paper_order_intent", "authorized intents need a reference price and notional")
        if parse(intent["quantity"]) <= 0:
            _fail("paper_order_intent", "quantity must be greater than zero")


V11_STAGES = {"state_initialized", "intent_cancelled", "state_recovery"}
V11_STATUSES = {"cancelled", "committed", "rolled_back"}


def validate_journal_event(event):
    validate_schema("trading_journal_event", event)
    if event["version"] == "1.0" and (event["stage"] in V11_STAGES or event["status"] in V11_STATUSES
                                     or event["agent"] == "paper_state"
                                     or {"account_id", "operation_id"} & set(event["subject"])):
        _fail("trading_journal_event", "version 1.0 events cannot use account-state fields")
    document = event["document"]
    if (document is None) != (event["document_sha256"] is None):
        _fail("trading_journal_event", "document and document hash must appear together")
    if document is not None:
        if sha256(document) != event["document_sha256"]:
            _fail("trading_journal_event", "document hash mismatch")
        expected = {"market_snapshot": validate_snapshot, "signal": validate_signal, "risk_check": validate_risk_decision,
                    "order_intent": validate_order_intent}.get(event["stage"])
        if expected is None:
            _fail("trading_journal_event", "this stage carries no document")
        expected(document)
