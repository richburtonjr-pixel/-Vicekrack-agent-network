"""Living HQ paper-broker status (Step 41): a read-only view of saved Alpaca PAPER broker records.

It reads only what the broker-paper commands saved (re-validated) and derives each intent's
state exactly as the CLI does. It never contacts Alpaca, never takes the broker lock, and
has no way to prepare, submit, refresh or cancel anything. Paper-broker activity is kept
apart from simulation results: it is not shown in the house or the trading results desk.
"""

import json
from datetime import datetime, timezone
from functools import lru_cache

from jsonschema import Draft202012Validator

from ..errors import NetworkError
from ..events.contract import ROOT

MAX_INTENTS = 100
NOTICE = ("ALPACA PAPER ACCOUNT records: a broker's simulated account, no real money. Separate from the offline "
          "simulator and from Step 24 local paper accounts. Read-only: this page cannot prepare, submit, refresh or "
          "cancel orders. States are as of the last explicit status refresh, not live.")


@lru_cache(maxsize=None)
def _validator(name):
    return Draft202012Validator(json.loads((ROOT / f"schemas/{name}.schema.json").read_text(encoding="utf-8")))


def _check(name, document):
    if next(_validator(name).iter_errors(document), None) is not None:
        raise NetworkError("invalid_broker_view", "The paper-broker view does not match its contract.")
    return document


def _row(item):
    order = item["order"]
    return {"intent_id": item["intent_id"], "created_at": item["created_at"], "symbol": order["symbol"],
            "side": order["side"], "qty": order["qty"], "limit_price": order["limit_price"],
            "max_notional": item["max_notional"], "state": item["state"], "terminal": item["terminal"],
            "reconciled": item["reconciled"], "outcome": item["outcome"], "broker_status": item["broker_status"],
            "filled_qty": item["filled_qty"], "filled_avg_price": item["filled_avg_price"],
            "submitted_at": item["submitted_at"], "last_observed_at": item["last_observed_at"],
            "observations": item["observations"], "cancel_requests": item["cancel_requests"]}


def _context(root):
    from ..trading.broker.cli import load_broker_config
    from ..trading.broker.store import BrokerPaperStore
    from ..trading.errors import TradingError
    try:
        config = load_broker_config()
    except TradingError:
        raise NetworkError("invalid_broker_view", "The paper-broker configuration is invalid.") from None
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return config, BrokerPaperStore(root), now


def broker_index(root=None):
    from ..trading.broker.orders import summary
    from ..trading.errors import TradingError
    config, store, now = _context(root)
    engaged, source = store.kill_switch(config)
    rows, problems = [], []
    ids = store.intent_ids()
    for intent_id in ids[-MAX_INTENTS:]:
        try:
            rows.append(_row(summary(store, intent_id, config, now)))
        except TradingError as error:
            problems.append({"intent_id": intent_id, "code": error.code})
    rows.sort(key=lambda r: (r["created_at"], r["intent_id"]), reverse=True)
    check = store.latest_check()
    document = {
        "contract": "hq_broker_paper", "version": "1.0", "read_only": True, "broker_paper": True, "simulated": False,
        "real_money": False, "notice": NOTICE,
        "kill_switch": {"engaged": engaged, "source": source},
        "last_check": None if check is None else {k: check[k] for k in (
            "checked_at", "account_fingerprint", "account_status", "trading_allowed", "market_open", "clock_time_utc")},
        "intents": rows, "total": len(ids), "shown_limit": MAX_INTENTS, "unreadable": problems[:MAX_INTENTS],
        "unreconciled": sum(1 for r in rows if r["submitted_at"] and not r["reconciled"]),
        "open_orders": sum(1 for r in rows if r["state"] in ("accepted", "partially_filled", "cancel_pending",
                                                              "cancel_requested", "done_for_day")),
        "commands": ["python -m vicekrack broker-paper-list", "python -m vicekrack broker-paper-status INTENT_ID --allow-network"],
    }
    return _check("hq-broker-paper", document)


def broker_intent(intent_id, root=None):
    from ..trading.broker.orders import consent_phrase, summary
    from ..trading.broker.store import INTENT_ID
    from ..trading.errors import TradingError
    if not INTENT_ID.match(str(intent_id)):
        raise NetworkError("invalid_intent_id", "Use a bpi- intent ID.")
    config, store, now = _context(root)
    try:
        item = summary(store, intent_id, config, now)
        intent, submission, _, _, _, _ = store.records(intent_id)
    except TradingError as error:
        raise NetworkError(error.code, "The paper intent could not be loaded safely.") from None
    commands = [f"python -m vicekrack broker-paper-inspect {intent_id}"]
    if submission is not None:
        commands.append(f"python -m vicekrack broker-paper-status {intent_id} --allow-network")
    if submission is not None and item["state"] in ("accepted", "partially_filled", "done_for_day"):
        commands.append(f"python -m vicekrack broker-paper-cancel {intent_id} --consent "
                        f"{consent_phrase(intent_id, 'cancel')} --allow-network")
    document = {
        "contract": "hq_broker_paper_intent", "version": "1.0", "read_only": True, "broker_paper": True,
        "simulated": False, "real_money": False, "notice": NOTICE, **_row(item),
        "client_order_id": item["client_order_id"], "time_in_force": intent["order"]["time_in_force"],
        "expires_at": item["expires_at"], "exposure": intent["exposure"]["explanation"],
        "quote_at_preparation": intent["inputs_at_preparation"]["quote"],
        "preview_checks": intent["preview_checks"],
        "submission_checks": submission["checks"] if submission else None,
        "events": [{k: e[k] for k in ("sequence", "event_type", "recorded_at", "reason_codes", "details")}
                   for e in item["events"]][-200:],
        "commands": commands,
    }
    return _check("hq-broker-paper-intent", document)
