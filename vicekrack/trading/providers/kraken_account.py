"""Kraken READ-ONLY account connection (first slice).

Reads balances, open/closed orders, trade history, ledger entries and open positions.
It cannot place, change, cancel or withdraw anything: the client only ever calls the fixed
query endpoints listed in READ_ENDPOINTS, and any other name is refused before a request is
built. There is no order or withdrawal code in this file.

Credentials:
- Read only from the environment: KRAKEN_API_KEY and KRAKEN_API_SECRET.
- The secret is never stored, logged, printed, put in an error, or shown in repr().
- Create the key with ONLY these Kraken permissions: Query Funds, Query Open Orders & Trades,
  Query Closed Orders & Trades, Query Ledger Entries. Do not enable Create/Modify orders,
  Cancel orders, Withdraw funds, or any transfer permission.

Signing (Kraken REST): API-Sign = base64( HMAC-SHA512( key = base64decode(secret),
  msg = path + SHA256(nonce + postdata) ) ), where postdata is the URL-encoded form body.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

from ..errors import TradingError
from .alpaca import urllib_transport

HOST = "https://api.kraken.com"
PRIVATE_PREFIX = "/0/private/"
TIMEOUT_SECONDS = 20
MAX_BYTES = 4_000_000
READ_ENDPOINTS = frozenset({"Balance", "TradeBalance", "OpenOrders", "ClosedOrders", "TradesHistory",
                            "Ledgers", "OpenPositions"})
ENV_KEY, ENV_SECRET = "KRAKEN_API_KEY", "KRAKEN_API_SECRET"


def sign(path, nonce, postdata, secret_b64):
    """Kraken's API-Sign header value. Pure function, no I/O."""
    try:
        key = base64.b64decode(secret_b64, validate=True)
    except (ValueError, TypeError):
        raise TradingError("kraken_secret_invalid", "The Kraken secret is not valid base64.") from None
    digest = hashlib.sha256((str(nonce) + postdata).encode("utf-8")).digest()
    mac = hmac.new(key, path.encode("utf-8") + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode("ascii")


class KrakenReadOnlyClient:
    def __init__(self, *, api_key, api_secret, transport=urllib_transport, clock=time.time):
        if not api_key or not api_secret:
            raise TradingError("kraken_not_configured",
                               "Set KRAKEN_API_KEY and KRAKEN_API_SECRET in this PC's environment first.")
        self._api_key = api_key
        self._api_secret = api_secret
        self.transport = transport
        self.clock = clock
        self._last_nonce = 0

    @classmethod
    def from_environment(cls, environ=None, **kwargs):
        env = os.environ if environ is None else environ
        return cls(api_key=env.get(ENV_KEY, ""), api_secret=env.get(ENV_SECRET, ""), **kwargs)

    def __repr__(self):
        return "KrakenReadOnlyClient(credentials=hidden)"

    def _nonce(self):
        # Strictly increasing, in microseconds, as Kraken requires for each key.
        value = int(self.clock() * 1_000_000)
        self._last_nonce = value if value > self._last_nonce else self._last_nonce + 1
        return self._last_nonce

    def query(self, endpoint, params=None):
        if endpoint not in READ_ENDPOINTS:
            raise TradingError("endpoint_not_allowed", "That Kraken endpoint is not a read-only query.")
        path = PRIVATE_PREFIX + endpoint
        nonce = self._nonce()
        postdata = urlencode({**(params or {}), "nonce": nonce})
        headers = {"API-Key": self._api_key, "API-Sign": sign(path, nonce, postdata, self._api_secret),
                   "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        try:
            status, _, body = self.transport(HOST + path, headers, timeout=TIMEOUT_SECONDS,
                                             max_bytes=MAX_BYTES, method="POST", body=postdata.encode("utf-8"))
        except TradingError:
            raise
        except Exception:
            raise TradingError("provider_unreachable", "Kraken could not be reached. Nothing was changed.") from None
        if status != 200:
            raise TradingError("kraken_refused", f"Kraken answered with status {status}. Nothing was changed.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise TradingError("kraken_data_invalid", "Kraken returned unreadable data.") from None
        if not isinstance(payload, dict):
            raise TradingError("kraken_data_invalid", "Kraken returned an unexpected response.")
        if payload.get("error"):
            # Kraken error strings are fixed codes such as "EAPI:Invalid key"; they are shown as a code only.
            raise TradingError("kraken_error", "Kraken refused the read-only request. Check the key permissions.")
        return payload.get("result", {})

    def balance(self):
        return self.query("Balance")

    def open_orders(self):
        return self.query("OpenOrders")

    def closed_orders(self):
        return self.query("ClosedOrders")

    def trades_history(self):
        return self.query("TradesHistory")

    def ledgers(self):
        return self.query("Ledgers")

    def open_positions(self):
        return self.query("OpenPositions")
