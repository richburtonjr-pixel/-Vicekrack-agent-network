"""Interactive Brokers PAPER adapter (the first tip on the universal broker plug).

How it connects: IBKR's own program (IB Gateway or TWS) runs on your PC, logged in to your PAPER
account. ViceKrack talks to it over a local socket using the free `ib_async` library, the same
library the Humbled Trader tutorial uses. There are no API keys: your IBKR login stays inside
IBKR's program and ViceKrack never sees or stores it.

Paper-only locks (all must pass, every time):
  1. config "mode" must be "ibkr_paper"; there is no live mode in this file.
  2. host must be this computer (127.0.0.1 / localhost); remote Gateways are refused.
  3. port must be a PAPER port: 4002 (IB Gateway paper) or 7497 (TWS paper).
     The live ports 4001 and 7496 are refused by name, and so is any other port.
  4. after connecting, every account IBKR reports must be a paper account (IDs start with "DU").
     If not, ViceKrack disconnects at once and refuses to do anything.
  5. the config's kill switch blocks new orders (status checks and cancels still work).
Account numbers are replaced by a fingerprint; balances are read in memory and returned, not stored.
"""

import json
import math
from pathlib import Path

from ..contracts import ROOT
from ..errors import TradingError
from .base import PaperBroker, check_intent, fingerprint, money

PAPER_PORTS = {4002: "IB Gateway (paper)", 7497: "TWS (paper)"}
LIVE_PORTS = {4001: "IB Gateway (LIVE)", 7496: "TWS (LIVE)"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
PAPER_ACCOUNT_PREFIX = "DU"
SUMMARY_TAGS = {"NetLiquidation": "net_liquidation", "TotalCashValue": "cash", "BuyingPower": "buying_power"}


def load_ibkr_config(path="config/broker.ibkr.paper.json", root=ROOT):
    try:
        config = json.loads((Path(root) / path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise TradingError("broker_config_unreadable", "Cannot read the IBKR paper broker config.") from None
    check_config(config)
    return config


def check_config(config):
    """The paper-only locks that can be checked before connecting."""
    if not isinstance(config, dict) or config.get("mode") != "ibkr_paper":
        raise TradingError("broker_not_paper", "The IBKR connector only runs in paper mode (mode: ibkr_paper).")
    connection = config.get("connection") or {}
    if connection.get("host") not in LOCAL_HOSTS:
        raise TradingError("broker_host_not_local", "IB Gateway/TWS must run on this computer (127.0.0.1).")
    port = connection.get("port")
    if port in LIVE_PORTS:
        raise TradingError("broker_live_port_refused",
                           f"Port {port} is {LIVE_PORTS[port]}. ViceKrack never connects to a live port.")
    if port not in PAPER_PORTS:
        raise TradingError("broker_port_not_paper", "Use 4002 (IB Gateway paper) or 7497 (TWS paper).")
    client_id = connection.get("client_id")
    if not isinstance(client_id, int) or isinstance(client_id, bool) or not 0 <= client_id <= 999:
        raise TradingError("broker_bad_client_id", "client_id must be a whole number from 0 to 999.")
    timeout = connection.get("timeout_seconds", 10)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 60:
        raise TradingError("broker_bad_timeout", "timeout_seconds must be between 1 and 60.")
    limits = config.get("limits") or {}
    if not isinstance(limits.get("max_quantity"), int) or limits["max_quantity"] < 1:
        raise TradingError("broker_bad_limits", "limits.max_quantity must be a whole number above zero.")
    if money(limits.get("max_order_dollars"), "max_order_dollars") <= 0:
        raise TradingError("broker_bad_limits", "limits.max_order_dollars must be above zero.")
    if not isinstance((config.get("kill_switch") or {}).get("engaged"), bool):
        raise TradingError("broker_bad_kill_switch", "kill_switch.engaged must be true or false.")
    return True


def _library():
    try:
        import ib_async
    except ImportError:
        raise TradingError("broker_library_missing",
                           "The ib_async package is not installed. Run the ViceKrack setup again to install it.") from None
    return ib_async


def _number(value):
    """IBKR sends floats (sometimes NaN or empty) -> text, or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    text = format(money(repr(number), "value").normalize(), "f")
    return text


class IBKRPaperBroker(PaperBroker):
    name = "ibkr"

    def __init__(self, config, library=None, readonly=True):
        check_config(config)
        self.config = config
        self.lib = library
        self.readonly = readonly
        self.ib = None
        self.accounts = []

    # -- connection -------------------------------------------------------------------------
    def connect(self):
        lib = self.lib or _library()
        self.lib = lib
        connection = self.config["connection"]
        ib = lib.IB()
        try:
            ib.connect(connection["host"], connection["port"], clientId=connection["client_id"],
                       timeout=connection.get("timeout_seconds", 10), readonly=self.readonly)
        except Exception:
            raise TradingError("broker_unreachable",
                               "Could not reach IB Gateway/TWS. Is it open, logged in to PAPER, with the API turned on?") from None
        accounts = [str(a) for a in (ib.managedAccounts() or [])]
        if not accounts or not all(a.startswith(PAPER_ACCOUNT_PREFIX) for a in accounts):
            ib.disconnect()
            raise TradingError("broker_not_paper_account",
                               "IBKR reported a non-paper account. Disconnected; nothing was read or sent.")
        self.ib, self.accounts = ib, accounts
        return self

    def close(self):
        if self.ib is not None:
            self.ib.disconnect()
            self.ib = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False

    def _need(self):
        if self.ib is None or not self.ib.isConnected():
            raise TradingError("broker_not_connected", "Not connected to IB Gateway/TWS.")

    # -- read ---------------------------------------------------------------------------------
    def account(self):
        self._need()
        account = self.accounts[0]
        values = {}
        currency = None
        for row in self.ib.accountSummary(account) or []:
            key = SUMMARY_TAGS.get(getattr(row, "tag", None))
            if key and getattr(row, "currency", "") not in ("", "BASE"):
                values[key] = _number(row.value)
                currency = currency or row.currency
        return {"broker": self.name, "mode": "paper", "account": fingerprint(self.name, account),
                "accounts_reported": len(self.accounts), "currency": currency,
                **{key: values.get(key) for key in SUMMARY_TAGS.values()}}

    def positions(self):
        self._need()
        out = []
        for row in self.ib.positions() or []:
            contract = row.contract
            if getattr(row, "account", self.accounts[0]) not in self.accounts or not row.position:
                continue
            out.append({"symbol": contract.symbol, "security": getattr(contract, "secType", "STK"),
                        "quantity": _number(row.position), "average_cost": _number(row.avgCost)})
        return sorted(out, key=lambda p: p["symbol"])

    def open_orders(self):
        self._need()
        out = []
        for trade in self.ib.openTrades() or []:
            order, status = trade.order, trade.orderStatus
            out.append({"order_id": order.orderId, "symbol": trade.contract.symbol,
                        "side": "buy" if order.action == "BUY" else "sell", "quantity": _number(order.totalQuantity),
                        "order_type": order.orderType, "status": status.status, "filled": _number(status.filled),
                        "tag": getattr(order, "orderRef", "") or ""})
        return out

    # -- write (paper) ------------------------------------------------------------------------
    def place(self, intent):
        if self.readonly:
            raise TradingError("broker_read_only", "This connection was opened read-only; it cannot place orders.")
        if self.config["kill_switch"]["engaged"]:
            raise TradingError("broker_kill_switch", "The IBKR paper kill switch is on. No new orders.")
        check_intent(intent, self.config["limits"])
        self._need()
        lib = self.lib
        contract = lib.Stock(intent.symbol, "SMART", "USD")
        qualified = [c for c in (self.ib.qualifyContracts(contract) or []) if c is not None and getattr(c, "conId", 0)]
        if not qualified:
            raise TradingError("broker_unknown_symbol", f"IBKR does not recognise {intent.symbol} as a US stock.")
        action = "BUY" if intent.side == "buy" else "SELL"
        if intent.order_type == "market":
            order = lib.MarketOrder(action, intent.quantity)
        elif intent.order_type == "limit":
            order = lib.LimitOrder(action, intent.quantity, float(money(intent.limit_price, "limit_price")))
        else:
            order = lib.StopOrder(action, intent.quantity, float(money(intent.stop_price, "stop_price")))
        order.tif = intent.time_in_force.upper()
        order.outsideRth = bool(intent.outside_regular_hours)
        order.orderRef = intent.tag[:40]
        order.account = self.accounts[0]                # always the paper account checked at connect
        trade = self.ib.placeOrder(qualified[0], order)
        return {"broker": self.name, "mode": "paper", "order_id": trade.order.orderId, "symbol": intent.symbol,
                "side": intent.side, "quantity": intent.quantity, "order_type": intent.order_type,
                "status": trade.orderStatus.status, "tag": order.orderRef}

    def cancel(self, order_id):
        self._need()
        for trade in self.ib.openTrades() or []:
            if trade.order.orderId == order_id:
                self.ib.cancelOrder(trade.order)
                return {"order_id": order_id, "status": "cancel_requested"}
        raise TradingError("broker_order_not_found", f"No open paper order {order_id}.")
