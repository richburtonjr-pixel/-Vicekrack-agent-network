"""Bounded offline paper-execution simulation (Step 29). SIMULATED ONLY.

Turns Step 27 research signals into simulated orders only under an explicit, validated
simulation policy, fills them at later bar opens with documented slippage and fees, and
tracks a separate in-run simulation account. Never touches Step 24 paper accounts, the
paper risk engine, intents, brokers or live data.
"""
