"""Historical market-data fetch CLI (Step 40). Opt-in network; historical completed bars only.

python -m vicekrack market-fetch --provider alpaca --symbol AAPL --interval 5m \
    --start 2026-09-01 --end 2026-09-05 --feed iex --adjustment raw --allow-network
python -m vicekrack market-fetch --provider alpaca --symbol BTC/USD --interval 1h \
    --start 2026-09-01 --end 2026-09-30 --allow-network          # HQ Phase 3a: crypto (UTC, 24/7)

Credentials are read only from APCA_API_KEY_ID and APCA_API_SECRET_KEY in the environment.
The result is an ordinary immutable Step 25 dataset (`mds-...`) labelled `historical`,
usable by `trading-session-start`, `agent-run`, `sim-run` and the Living HQ like any other.
There are no orders, streaming, polling or retries here.
"""

import argparse
import json
import sys

from ..errors import TradingError
from ..market.cli import DATA_NOTICE, summary
from ..market.store import MarketStore, load_market_config
from .alpaca import TIMEFRAMES, AlpacaHistoricalAdapter, load_provider_config
from .alpaca_crypto import AlpacaCryptoAdapter, is_crypto_symbol

COMMANDS = {"market-fetch"}
NOTICE = ("HISTORICAL data downloaded once from the named provider and stored as an immutable dataset. Not live, "
          "not a quote; retrieval time is not market time. " + DATA_NOTICE)


def parser():
    root = argparse.ArgumentParser(prog="python -m vicekrack",
                                   description="ViceKrack opt-in historical market data (no streaming, no orders)")
    commands = root.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("market-fetch", help="Download completed historical bars once and store them as a dataset")
    fetch.add_argument("--provider", choices=("alpaca",), required=True)
    fetch.add_argument("--symbol", required=True, help="One US stock symbol (AAPL) or a crypto pair (BTC/USD, ETH/USD)")
    fetch.add_argument("--interval", choices=sorted(TIMEFRAMES), required=True)
    fetch.add_argument("--start", required=True, help="First date, YYYY-MM-DD (New York for stocks, UTC for crypto)")
    fetch.add_argument("--end", required=True, help="Last date, YYYY-MM-DD (must be before today)")
    fetch.add_argument("--feed", help="Stocks only, required: iex or sip (your account must be entitled to it)")
    fetch.add_argument("--adjustment", help="Stocks only, required: raw, split, dividend or all (explicit; never mixed)")
    fetch.add_argument("--allow-network", action="store_true",
                       help="Required: explicitly allow this one bounded download from the provider")
    return root


def main(argv=None, root=None, transport=None, clock=None, environ=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = load_provider_config()
        market_config, market_sha = load_market_config()
        if args.interval not in market_config["allowed_intervals"]:
            raise TradingError("interval_not_allowed", "This interval is not allowed by config/market-data.json.")
        extra = {k: v for k, v in (("transport", transport), ("clock", clock), ("environ", environ)) if v is not None}
        if is_crypto_symbol(args.symbol, config):
            if args.feed is not None or args.adjustment is not None:
                raise TradingError("crypto_options_not_allowed",
                                   "Crypto has no --feed or --adjustment choice; leave them out. Nothing was requested.")
            adapter = AlpacaCryptoAdapter(symbol=args.symbol, interval=args.interval, start=args.start, end=args.end,
                                          allow_network=args.allow_network, config=config, **extra)
        else:
            adapter = AlpacaHistoricalAdapter(symbol=args.symbol, interval=args.interval, start=args.start, end=args.end,
                                              feed=args.feed, adjustment=args.adjustment,
                                              allow_network=args.allow_network, config=config, **extra)
        store = MarketStore(root, **({"clock": clock} if clock is not None else {}))
        dataset = store.import_dataset(adapter, config=market_config, config_sha256=market_sha, label="historical")
        output = {"imported": True, **summary(dataset, 3), "notice": NOTICE,
                  "next": [f"python -m vicekrack market-inspect {dataset['dataset_id']}",
                           f"python -m vicekrack trading-session-start {dataset['dataset_id']} --record-events"]}
        code = 0
    except TradingError as error:
        output, code = {"error": error.as_dict()}, 1
    except (OSError, ValueError, UnicodeError):
        output, code = {"error": {"code": "market_storage_error", "message": "Cannot read or write local market data."}}, 1
    print(json.dumps(output, indent=2, allow_nan=False))
    return code
