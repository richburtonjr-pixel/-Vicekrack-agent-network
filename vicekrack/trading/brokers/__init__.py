"""Broker plug-in layer (paper only). `open_broker("ibkr")` returns a connected-ready adapter.

Registered tips: ibkr (Interactive Brokers paper). Alpaca paper (trading/broker) and Kraken
(practice prices only) stay as they are and can be fitted to this plug later.
"""

from .base import OrderIntent, PaperBroker, check_intent  # noqa: F401

REGISTRY = {
    "ibkr": {"label": "Interactive Brokers (paper)", "config": "config/broker.ibkr.paper.json"},
}


def open_broker(name, root=None, library=None, readonly=True):
    if name not in REGISTRY:
        from ..errors import TradingError
        raise TradingError("broker_unknown", f"Unknown broker {name!r}. Known: {', '.join(sorted(REGISTRY))}.")
    if name == "ibkr":
        from .ibkr import IBKRPaperBroker, load_ibkr_config
        kwargs = {} if root is None else {"root": root}
        return IBKRPaperBroker(load_ibkr_config(REGISTRY[name]["config"], **kwargs), library=library, readonly=readonly)
