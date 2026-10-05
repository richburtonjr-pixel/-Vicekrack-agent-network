"""ViceKrack trading subsystem (Step 23 foundation): PAPER ONLY.

This package is separate from the content pipeline. It defines versioned contracts
(market snapshot, signal, risk decision, paper order intent, journal event), a
paper-only configuration with validated limits and a kill switch, a deterministic risk
engine, and an append-only local journal. There are no market feeds, indicators, AI
trading decisions, broker connections, live orders or background loops. Nothing here
submits or executes an order; every intent is a simulated paper record.

Shared core used from the rest of ViceKrack: `vicekrack.errors.NetworkError` (common error
type with fixed codes) and `vicekrack.persistence.reject_secrets` (credential rejection).
See docs/subsystems.md for boundaries.
"""

PAPER_ONLY_NOTICE = "SIMULATED: paper trading only. No order was sent to any broker or executed."
