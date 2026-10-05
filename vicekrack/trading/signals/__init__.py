"""Rule-based research signals on closed bars and indicators (Step 27).

VWAP reclaim, EMA crossover and breakout rules evaluated during a bounded Step 25 replay
with Step 26 indicators. Research signals are observations only: they carry
`authorization_possible: false`, have no order proposal, and never touch paper accounts.
"""
