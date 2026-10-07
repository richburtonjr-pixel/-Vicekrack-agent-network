"""Structured execution events and bounded timeline replay (Step 31).

Shared core for both departments. `contract`, `sink` and `store` never import content or
trading code; department adapters live with their department and the CLI composes them.
"""
