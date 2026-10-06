"""Bounded, deterministic research-agent workflow (Step 28).

Market Scout -> Trend Agent -> Strategy Agent -> Risk Review, run by a controller over a
frozen evidence package built from closed bars, indicators and research signals at a
simulated time. Local handlers only (no AI providers). Output is research only:
`research_only: true`, `authorization_possible: false`; no paper accounts, risk engine,
order intents or brokers.
"""
