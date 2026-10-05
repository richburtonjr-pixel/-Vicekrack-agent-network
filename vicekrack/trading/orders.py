"""Paper order intents. An intent is a simulated record of what *would* be ordered.

`authorized_paper` means the risk engine allowed it on paper. Nothing is submitted or
executed: `execution.submitted` and `execution.executed` are always false and there is no
broker. Without an allowed risk decision the intent is `blocked`.
"""

from .contracts import sha256, validate_order_intent
from .money import fmt, multiply, parse
from .risk import reference_price


def build_intent(signal, decision, snapshot, created_at):
    if decision["signal_id"] != signal["signal_id"]:
        raise ValueError("decision does not belong to this signal")
    allowed = decision["outcome"] == "allowed"
    proposal = signal["proposal"]
    price = notional = None
    if allowed:
        reference = reference_price(signal, snapshot)
        price, notional = fmt(reference), fmt(multiply(reference, parse(proposal["quantity"])))
    intent = {
        "contract": "paper_order_intent", "version": "1.0",
        "intent_id": "pint-" + sha256({"decision_id": decision["decision_id"], "signal_id": signal["signal_id"]})[:24],
        "mode": "paper", "status": "authorized_paper" if allowed else "blocked",
        "signal_id": signal["signal_id"], "decision_id": decision["decision_id"], "symbol": signal["symbol"],
        "side": proposal["side"], "quantity": proposal["quantity"], "order_type": proposal["order_type"],
        "limit_price": proposal["limit_price"], "reference_price": price, "notional": notional,
        "created_at": created_at, "execution": {"submitted": False, "executed": False, "broker": None},
        "simulated": True,
    }
    validate_order_intent(intent)
    return intent

