"""Offline market-data ingestion and bounded replay (Step 25).

Two adapters (synthetic fixtures, local CSV) feed one validated `market_dataset` contract;
`market-replay` replays a stored dataset on a simulation clock without future bars. There
are no live feeds, indicators, signals, orders or account access in this package.
"""
