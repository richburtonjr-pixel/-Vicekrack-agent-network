"""Alpaca PAPER trading client (Step 41): fixed endpoints, paper credentials, sanitized results.

Endpoints (Alpaca API reference, October 2026), all HTTPS, no redirects:
  https://paper-api.alpaca.markets   GET /v2/account, GET /v2/clock, GET /v2/assets/{symbol},
                                     GET /v2/positions, GET /v2/orders?status=open,
                                     POST /v2/orders, GET /v2/orders:by_client_order_id,
                                     DELETE /v2/orders/{order_id}
  https://data.alpaca.markets        GET /v2/stocks/{symbol}/quotes/latest   (fresh quote; IEX feed,
                                     the feed Alpaca documents for paper-only accounts)
The real-money host (api.alpaca.markets) and any other host are refused before a request is
built. There is no way to override the endpoint: if APCA_API_BASE_URL or
ALPACA_PAPER_BASE_URL is set to anything other than the paper URL, every command refuses.

Credentials: ALPACA_PAPER_API_KEY_ID and ALPACA_PAPER_API_SECRET_KEY, deliberately named
differently from the Step 40 data keys so live keys are never picked up by accident.
They are sent only as the APCA-API-KEY-ID / APCA-API-SECRET-KEY headers and never stored.

Sanitizing: every response is reduced to the few fields ViceKrack needs. The account ID and
number are replaced by a one-way fingerprint; balances are used for checks in memory and
never persisted. Error bodies are reduced to Alpaca's numeric error code. Nothing raw
(bodies, headers, URLs with tokens) is ever stored or printed.
"""

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

from ..contracts import sha256
from ..errors import TradingError
from ..providers.alpaca import urllib_transport

PAPER_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
LIVE_HOSTS = ("api.alpaca.markets",)
KEY_ENV, SECRET_ENV = "ALPACA_PAPER_API_KEY_ID", "ALPACA_PAPER_API_SECRET_KEY"
OVERRIDE_ENVS = ("APCA_API_BASE_URL", "ALPACA_PAPER_BASE_URL")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
ORDER_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")
AMBIGUOUS = {"provider_timeout", "provider_unreachable"}


class Ambiguous(TradingError):
    """The request may or may not have been processed by the broker (timeout, dropped
    connection, 5xx, unreadable answer). The caller must reconcile, never resend."""


def utc(text, field="time"):
    """RFC 3339 (any offset, optional fraction) -> 'YYYY-MM-DDTHH:MM:SSZ' (fraction truncated)."""
    match = TIME.match(text) if isinstance(text, str) else None
    if not match:
        raise TradingError("broker_malformed_response", f"The broker returned an invalid {field}.")
    moment = datetime.fromisoformat(match.group(1) + (match.group(3).replace("Z", "+00:00")))
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def decimal_text(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TradingError("broker_malformed_response", f"The broker returned an invalid {field}.") from None
    if not number.is_finite():
        raise TradingError("broker_malformed_response", f"The broker returned an invalid {field}.")
    text = format(number.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def fingerprint(account_id):
    return "pacct-" + sha256({"vicekrack_alpaca_paper_account": str(account_id)})[:24]


def check_endpoint(url):
    if not isinstance(url, str) or not (url.startswith(PAPER_BASE + "/") or url.startswith(DATA_BASE + "/")):
        raise TradingError("endpoint_not_allowed", "Only the fixed Alpaca paper-trading and market-data endpoints are allowed.")
    host = url.split("/")[2]
    if host in LIVE_HOSTS:
        raise TradingError("endpoint_not_allowed", "The real-money Alpaca endpoint is never used.")


class AlpacaPaperClient:
    def __init__(self, *, config, allow_network, transport=urllib_transport, environ=None):
        if not allow_network:
            raise TradingError("network_not_allowed",
                               "This command talks to the Alpaca PAPER account: add --allow-network. Nothing was requested.")
        self.environ = os.environ if environ is None else environ
        for name in OVERRIDE_ENVS:
            value = str(self.environ.get(name, "")).strip().rstrip("/")
            if value and value != PAPER_BASE:
                raise TradingError("endpoint_override_rejected",
                                   f"{name} points somewhere other than the Alpaca paper endpoint; endpoint overrides "
                                   "are refused. Unset it. Nothing was requested.")
        self.network = config["network"]
        self.feed = config["data_feed"]
        self.transport = transport
        self._headers = None

    def headers(self):
        if self._headers is None:
            key, secret = str(self.environ.get(KEY_ENV, "")).strip(), str(self.environ.get(SECRET_ENV, "")).strip()
            if not key or not secret:
                raise TradingError("broker_credentials_missing",
                                   f"Set {KEY_ENV} and {SECRET_ENV} (your Alpaca PAPER keys) in the environment. "
                                   "Nothing was requested.")
            if any(ord(ch) < 33 or ord(ch) > 126 for ch in key + secret):
                raise TradingError("broker_credentials_invalid", f"{KEY_ENV} or {SECRET_ENV} contains spaces or control "
                                                                 "characters. Nothing was requested.")
            self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json",
                             "User-Agent": "ViceKrack-paper-broker/1.0"}
        return self._headers

    # ------------------------------------------------------------------ one bounded request, no retries
    def request(self, method, url, payload=None):
        """(status, parsed JSON or None). Network failures and 5xx become Ambiguous; other errors are TradingErrors."""
        check_endpoint(url)
        headers = dict(self.headers())
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, _, data = self.transport(url, headers, timeout=self.network["timeout_seconds"],
                                             max_bytes=self.network["max_response_bytes"], method=method, body=body)
        except TradingError as error:
            if error.code in AMBIGUOUS:
                raise Ambiguous("broker_no_answer", "The paper broker did not answer in time or the connection dropped; "
                                                    "the outcome is unknown.") from None
            raise
        if 500 <= int(status) <= 599:
            raise Ambiguous("broker_server_error", f"The paper broker returned HTTP {int(status)}; the outcome is unknown.")
        if len(data) > self.network["max_response_bytes"]:
            raise Ambiguous("broker_response_too_large", "The paper broker's answer exceeded the size limit.")
        parsed = None
        if data:
            try:
                parsed = json.loads(data.decode("utf-8"), parse_float=str)
            except (UnicodeError, ValueError):
                if 200 <= int(status) <= 299:
                    raise Ambiguous("broker_malformed_response", "The paper broker's answer could not be read.") from None
        return int(status), parsed

    def get(self, path, query=None):
        url = PAPER_BASE + path + ("?" + urlencode(query) if query else "")
        status, data = self.request("GET", url)
        if status == 401:
            raise TradingError("broker_auth_failed", "Alpaca did not accept the PAPER credentials (HTTP 401).")
        if status == 403:
            raise TradingError("broker_forbidden", "Alpaca refused this paper request (HTTP 403).")
        if status == 429:
            raise TradingError("broker_rate_limited", "Alpaca's rate limit was reached (HTTP 429); it was not retried.")
        return status, data

    # ------------------------------------------------------------------ sanitized reads
    def account(self):
        status, data = self.get("/v2/account")
        if status != 200 or not isinstance(data, dict) or not data.get("id"):
            raise TradingError("broker_malformed_response", "The paper account could not be read.")
        try:
            return {"fingerprint": fingerprint(data["id"]), "status": str(data.get("status", "")),
                    "currency": str(data.get("currency", "")),
                    "trading_blocked": data.get("trading_blocked") is True, "account_blocked": data.get("account_blocked") is True,
                    "trade_suspended_by_user": data.get("trade_suspended_by_user") is True,
                    "buying_power": Decimal(decimal_text(data.get("buying_power"), "buying power"))}   # memory only
        except TradingError:
            raise
        except (InvalidOperation, TypeError, ValueError):
            raise TradingError("broker_malformed_response", "The paper account could not be read.") from None

    def clock(self):
        status, data = self.get("/v2/clock")
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("is_open"), bool):
            raise TradingError("broker_malformed_response", "The market clock could not be read.")
        return {"is_open": data["is_open"], "timestamp_utc": utc(data.get("timestamp"), "clock time"),
                "next_open_utc": utc(data.get("next_open"), "next open"), "next_close_utc": utc(data.get("next_close"), "next close")}

    def asset(self, symbol):
        status, data = self.get("/v2/assets/" + quote(symbol, safe=""))
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            raise TradingError("broker_malformed_response", "The asset could not be read.")
        return {"symbol": str(data.get("symbol", "")), "class": str(data.get("class", "")),
                "exchange": str(data.get("exchange", "")), "status": str(data.get("status", "")),
                "tradable": data.get("tradable") is True}

    def positions(self):
        status, data = self.get("/v2/positions")
        if status != 200 or not isinstance(data, list):
            raise TradingError("broker_malformed_response", "Positions could not be read.")
        out = []
        for item in data[:500]:
            if not isinstance(item, dict):
                raise TradingError("broker_malformed_response", "Positions could not be read.")
            out.append({"symbol": str(item.get("symbol", "")), "side": str(item.get("side", "")),
                        "qty": Decimal(decimal_text(item.get("qty"), "position quantity")),
                        "qty_available": Decimal(decimal_text(item.get("qty_available", item.get("qty")), "position quantity")),
                        "market_value": Decimal(decimal_text(item.get("market_value"), "position value")).copy_abs()})
        return out

    def open_orders(self):
        status, data = self.get("/v2/orders", {"status": "open", "limit": self.network["max_open_orders_listed"],
                                               "direction": "asc"})
        if status != 200 or not isinstance(data, list):
            raise TradingError("broker_malformed_response", "Open orders could not be read.")
        if len(data) >= self.network["max_open_orders_listed"]:
            raise TradingError("broker_too_many_open_orders", "More open orders exist than can be checked; nothing new is allowed.")
        return [order_view(item) for item in data]

    def order_by_client_id(self, client_order_id):
        status, data = self.get("/v2/orders:by_client_order_id", {"client_order_id": client_order_id})
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            raise TradingError("broker_malformed_response", "The order could not be read.")
        return order_view(data)

    def latest_quote(self, symbol):
        url = DATA_BASE + "/v2/stocks/" + quote(symbol, safe="") + "/quotes/latest?" + urlencode({"feed": self.feed})
        status, data = self.request("GET", url)
        if status in (401, 403):
            raise TradingError("quote_unavailable", f"The {self.feed} quote is not available to these paper credentials "
                                                    f"(HTTP {status}).")
        if status == 429:
            raise TradingError("broker_rate_limited", "Alpaca's rate limit was reached (HTTP 429); it was not retried.")
        quote_data = data.get("quote") if isinstance(data, dict) else None
        if status != 200 or not isinstance(quote_data, dict) or data.get("symbol") != symbol:
            raise TradingError("quote_unavailable", "No readable latest quote was returned.")
        return {"bid": decimal_text(quote_data.get("bp"), "bid"), "ask": decimal_text(quote_data.get("ap"), "ask"),
                "quote_time_utc": utc(quote_data.get("t"), "quote time"), "feed": self.feed}

    # ------------------------------------------------------------------ writes (callers record intent first)
    def submit(self, payload):
        """POST /v2/orders. Returns ('accepted', order) or ('rejected', {http_status, broker_code}).
        Raises Ambiguous when the outcome is unknown."""
        status, data = self.request("POST", PAPER_BASE + "/v2/orders", payload)
        if status == 200 and isinstance(data, dict):
            return "accepted", order_view(data)
        if status == 200:
            raise Ambiguous("broker_malformed_response", "The paper broker accepted something but the order could not be read.")
        if status in (400, 401, 403, 404, 422, 429):
            code = data.get("code") if isinstance(data, dict) else None
            return "rejected", {"http_status": status, "broker_code": int(code) if isinstance(code, int) else None}
        raise Ambiguous("broker_unexpected_status", f"The paper broker returned HTTP {status}; the outcome is unknown.")

    def cancel(self, order_id):
        if not ORDER_ID.match(str(order_id)):
            raise TradingError("broker_malformed_response", "The broker order ID is not valid.")
        status, _ = self.request("DELETE", PAPER_BASE + "/v2/orders/" + order_id)
        if status in (200, 204):
            return "cancel_requested"
        if status == 422:
            return "not_cancelable"
        if status == 404:
            return "not_found"
        if status in (401, 403, 429):
            raise TradingError("broker_cancel_refused", f"The cancel request was refused (HTTP {status}); it was not retried.")
        raise Ambiguous("broker_unexpected_status", f"The paper broker returned HTTP {status}; the cancel outcome is unknown.")


def order_view(data):
    """The sanitized subset of an Alpaca order that ViceKrack keeps."""
    if not isinstance(data, dict) or not ORDER_ID.match(str(data.get("id", ""))):
        raise TradingError("broker_malformed_response", "The broker order could not be read.")

    def optional_time(key):
        return utc(data[key], key) if data.get(key) else None

    def optional_decimal(key):
        return decimal_text(data[key], key) if data.get(key) not in (None, "") else None
    return {"broker_order_id": str(data["id"]), "client_order_id": str(data.get("client_order_id", ""))[:128],
            "symbol": str(data.get("symbol", "")), "side": str(data.get("side", "")), "type": str(data.get("type", "")),
            "time_in_force": str(data.get("time_in_force", "")), "status": str(data.get("status", "")),
            "qty": optional_decimal("qty"), "filled_qty": optional_decimal("filled_qty") or "0",
            "filled_avg_price": optional_decimal("filled_avg_price"), "limit_price": optional_decimal("limit_price"),
            "extended_hours": data.get("extended_hours") is True,
            "submitted_at": optional_time("submitted_at"), "filled_at": optional_time("filled_at"),
            "canceled_at": optional_time("canceled_at"), "expired_at": optional_time("expired_at"),
            "updated_at": optional_time("updated_at")}
