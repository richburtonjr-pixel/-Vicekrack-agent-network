# Subsystem boundaries

ViceKrack now has two independent subsystems that share a small core. This page records
what belongs where, so new work lands in the right place without a large migration.

```mermaid
flowchart TB
    subgraph Core[Shared core infrastructure]
        E[vicekrack/errors.py: NetworkError fixed codes]
        P[vicekrack/persistence.py: reject_secrets]
        R[runtime/: ignored local storage]
        CI[scripts/run_tests.py, audit_credentials.py, CI]
    end
    subgraph Content[Content pipeline: Steps 1-22]
        C1[Scout -> Verification -> Selection -> Creator -> Scene plan -> Preview -> Production -> Quality]
    end
    subgraph Trading[Trading subsystem: vicekrack/trading, Steps 23-26]
        T1[Contracts -> Paper account state + lock -> Risk engine -> Paper order intent -> Journal]
        T2[market/: offline datasets + replay, no account access]
        T3[indicators/: descriptive values on closed bars, no account access]
    end
    CC[Future Command Center: read-only views]
    Content --> Core
    Trading --> Core
    CC -. reads .-> R
```

## Content pipeline (existing)

- **Code:** `vicekrack/*.py` outside `vicekrack/trading/`, `schemas/*.schema.json`,
  `config/` content files, `examples/` content fixtures.
- **Storage:** `runtime/scout`, `runtime/verification`, `runtime/selection`,
  `runtime/productions`, `runtime/quality` and others.
- **Rule:** it never imports `vicekrack.trading`.

## Trading subsystem (new, paper only)

- **Code:** `vicekrack/trading/` only. Schemas live in `schemas/trading/`, config in
  `config/trading.paper.json` and fixtures in `examples/trading/`.
- **Storage:** `runtime/trading/` only: the journal, the kill-switch file, the paper
  accounts (`runtime/trading/accounts/`), market data (`runtime/trading/market/`) and
  indicator results (`runtime/trading/indicators/`).
- **Market-data boundary:** `vicekrack/trading/market/` (Step 25) never imports the
  account state, risk engine, order or journal modules. Replay can't authorize anything
  or change an account.
- **Indicator boundary:** `vicekrack/trading/indicators/` (Step 26) uses only the market
  package and shared money/contract helpers. Results are descriptive and are not wired
  into risk checks or accounts.
- **Imports:** only the shared core: `vicekrack.errors` and `vicekrack.persistence`. It
  never imports content modules, and content modules never import it.
- **CLI:** only the `trading-*`, `market-*` and `indicator-*` commands, routed by
  `vicekrack/__main__.py` to `vicekrack.trading.cli`, `vicekrack.trading.market.cli` and
  `vicekrack.trading.indicators.cli`.
- **Scope today:** paper only, with offline market data. There are no live market feeds, broker connections, live
  orders, AI trading decisions or background loops.

## Shared core infrastructure

- `vicekrack/errors.py`: one error type with fixed, sanitized codes. `TradingError`
  subclasses it.
- `vicekrack/persistence.py` `reject_secrets`: credential rejection. Trading adds a
  stricter layer on top (broker-style field names, key shapes, active `*_KEY`, `*_TOKEN`,
  `*_SECRET` and `*_PASSWORD` environment values) without changing the shared function.
- The `runtime/` convention: ignored by git, written atomically, and inspected only by
  explicit CLI commands.
- Test harness and checks: `scripts/run_tests.py` (network blocked),
  `scripts/audit_credentials.py` and CI.

Changes to the shared core must keep both subsystems' tests passing.

## Future Command Center (not built)

A future dashboard should be a **read-only** consumer.

- **Trading:** the journal events (`trading_journal_event` 1.1) and the account state
  (`trading-state show`) are designed for it. Each event says which `agent` acted, what
  `document` it saw or produced (with a hash), the key `observed` figures and its
  `reason_codes`. Each `risk_check` event carries every
  check with its limit and observed value. `python -m vicekrack trading-journal RUN_ID`
  already prints that timeline.
- **Content:** the production states and quality reports are its data source.
- **Rule:** the Command Center must not authorize orders, change limits or release the
  kill switch without a separate, explicit design step.

See also the [Roadmap](roadmap.md).
