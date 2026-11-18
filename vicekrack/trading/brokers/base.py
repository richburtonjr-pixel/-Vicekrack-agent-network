"""The universal broker plug: one interface every broker adapter follows (paper only).

Think of a phone charger with swappable tips: the agents talk to `PaperBroker`, and each broker
(Interactive Brokers first; Alpaca and Kraken later) is a tip that fits it. Nothing in the desk
needs to know which broker is on the other end.

Hard rules every adapter inherits:
  * mode is always "paper". There is no live mode, no live switch, and no setting that enables one.
  * Every order is checked by `check_intent` (shape + this config's limits) before it can be sent.
  * Account numbers are never stored or shown: they are replaced by a one-way fingerprint.
  * Results are plain dicts with only the fields ViceKrack needs (no raw broker objects).
"""

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..errors import TradingError

SIDES = ("buy", "sell")
ORDER_TYPES = ("market", "limit", "stop")
TIME_IN_FORCE = ("day", "gtc")


def fingerprint(broker, account_id):
    """A short one-way stand-in for an account number (the real number is never kept)."""
    digest = hashlib.sha256(f"vicekrack:{broker}:paper:{account_id}".encode("utf-8")).hexdigest()
    return f"{broker}-paper-{digest[:16]}"


def money(value, name):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TradingError("broker_bad_number", f"{name} is not a number.") from None
    if not number.is_finite():
        raise TradingError("broker_bad_number", f"{name} is not a number.")
    return number


@dataclass(frozen=True)
class OrderIntent:
    """What the agents want to do, in broker-neutral words. Long-only stocks for now (whole shares)."""
    symbol: str
    side: str
    quantity: int
    order_type: str = "market"
    limit_price: str = None
    stop_price: str = None
    time_in_force: str = "day"
    outside_regular_hours: bool = False
    tag: str = "vicekrack"
    reference_price: str = None        # last known price, used for the dollar-size check on market/stop orders
    extra: dict = field(default_factory=dict)


def check_intent(intent, limits):
    """Raise TradingError if the intent is malformed or breaks this broker config's limits."""
    symbol = intent.symbol
    if not isinstance(symbol, str) or not symbol or len(symbol) > 12 \
            or not all(c.isalnum() or c in ".-" for c in symbol) or symbol != symbol.upper():
        raise TradingError("broker_bad_symbol", "Symbols are short upper-case tickers like MU or BRK.B.")
    if intent.side not in SIDES:
        raise TradingError("broker_bad_side", "Side must be buy or sell.")
    if not isinstance(intent.quantity, int) or isinstance(intent.quantity, bool) or intent.quantity < 1:
        raise TradingError("broker_bad_quantity", "Quantity must be a whole number of shares, at least 1.")
    if intent.quantity > limits["max_quantity"]:
        raise TradingError("broker_limit_quantity", f"More than {limits['max_quantity']} shares in one order.")
    if intent.order_type not in ORDER_TYPES:
        raise TradingError("broker_bad_order_type", "Order type must be market, limit or stop.")
    if intent.time_in_force not in TIME_IN_FORCE:
        raise TradingError("broker_bad_tif", "Time in force must be day or gtc.")
    if intent.order_type == "limit" and intent.limit_price is None:
        raise TradingError("broker_missing_price", "A limit order needs a limit price.")
    if intent.order_type == "stop" and intent.stop_price is None:
        raise TradingError("broker_missing_price", "A stop order needs a stop price.")
    if intent.order_type == "stop" and intent.side != "sell":
        raise TradingError("broker_bad_stop", "Stops are only used to protect long positions (sell stops).")
    price = intent.limit_price or intent.stop_price or intent.reference_price
    if price is None:
        raise TradingError("broker_missing_price", "A reference price is needed to check the order's dollar size.")
    price = money(price, "price")
    if price <= 0:
        raise TradingError("broker_bad_number", "Prices must be above zero.")
    if intent.side == "buy" and price * intent.quantity > money(limits["max_order_dollars"], "max_order_dollars"):
        raise TradingError("broker_limit_dollars", f"Buy is bigger than the ${limits['max_order_dollars']} per-order limit.")
    return True


class PaperBroker:
    """Interface. Adapters implement these; all return plain dicts. `mode` is fixed to paper."""
    name = "base"
    mode = "paper"

    def connect(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def account(self):
        """{"broker", "mode", "account", "currency", "net_liquidation", "cash", "buying_power"}"""
        raise NotImplementedError

    def positions(self):
        """[{"symbol", "quantity", "average_cost"}]"""
        raise NotImplementedError

    def open_orders(self):
        """[{"order_id", "symbol", "side", "quantity", "order_type", "status", "filled", "tag"}]"""
        raise NotImplementedError

    def place(self, intent):
        """Check, then send one paper order. -> {"order_id", "status", ...}"""
        raise NotImplementedError

    def cancel(self, order_id):
        raise NotImplementedError
