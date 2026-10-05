# Trading subsystem (Steps 23–24): paper only

> **SIMULATED.** Nothing in this subsystem connects to a broker, sends an order or
> executes a trade. An `authorized_paper` intent only means the risk rules would have
> allowed it on paper. All market data in the demo is a labelled synthetic fixture.

## Commands

```powershell
.\.venv\Scripts\python.exe -m vicekrack trading-config-check
.\.venv\Scripts\python.exe -m vicekrack trading-state init
.\.venv\Scripts\python.exe -m vicekrack trading-state show
.\.venv\Scripts\python.exe -m vicekrack trading-state list
.\.venv\Scripts\python.exe -m vicekrack trading-state cancel INTENT_ID --reason operator_request
.\.venv\Scripts\python.exe -m vicekrack trading-state recover
.\.venv\Scripts\python.exe -m vicekrack trading-demo --scenario allowed
.\.venv\Scripts\python.exe -m vicekrack trading-demo --scenario exposure-breach
.\.venv\Scripts\python.exe -m vicekrack trading-demo --scenario daily-loss
.\.venv\Scripts\python.exe -m vicekrack trading-demo --scenario stale-data
.\.venv\Scripts\python.exe -m vicekrack trading-demo --scenario invalid-money
.\.venv\Scripts\python.exe -m vicekrack trading-demo --scenario duplicate-signal
.\.venv\Scripts\python.exe -m vicekrack trading-demo --scenario pending-exposure
.\.venv\Scripts\python.exe -m vicekrack trading-journal
.\.venv\Scripts\python.exe -m vicekrack trading-journal RUN_ID
.\.venv\Scripts\python.exe -m vicekrack trading-journal RUN_ID --full
.\.venv\Scripts\python.exe -m vicekrack trading-kill-switch status
.\.venv\Scripts\python.exe -m vicekrack trading-kill-switch engage
.\.venv\Scripts\python.exe -m vicekrack trading-kill-switch release
```

On Linux/macOS use `.venv/bin/python`. Commands print JSON. Errors print
`{"error": {"code": ...}}` and exit with 1.

## Flow

```mermaid
flowchart LR
    F[Synthetic fixture] --> S[Validate market snapshot]
    F --> G[Validate signal against snapshot]
    F --> P[Validate paper portfolio]
    K[Config + kill switch] --> R
    S --> R[Deterministic risk engine]
    G --> R
    P --> R
    R --> D[risk_decision allowed / blocked]
    D --> I[paper_order_intent authorized_paper / blocked<br/>submitted=false, executed=false]
    S & G & D & I --> J[(runtime/trading/journal)]
```

## Contracts (version 1.0, `schemas/trading/`)

| Contract | Key fields |
|---|---|
| `market_snapshot` | `snapshot_id`, `symbol`, `observed_at`, `price{last,bid,ask}`, `volume`, `source{name,kind}`, `freshness.max_age_seconds`, `synthetic` |
| `trading_signal` | `signal_id`, `strategy{strategy_id,version}`, `observations[]` (snapshot field values), `rationale{codes,summary}`, `proposal`, `created_at`, `expires_at` |
| `paper_portfolio_state` | `equity`, `positions[]`, `day{trading_date,pnl}` |
| `risk_decision` | `decision_id`, `outcome`, `reason_codes`, `checks[]{check,status,limit,observed}`, `inputs` (SHA-256 of config, signal, snapshot, portfolio) |
| `paper_order_intent` | `symbol`, `side`, `quantity`, `order_type`, `limit_price`, originating `signal_id` and `decision_id`, `execution{submitted:false,executed:false,broker:null}` |
| `trading_journal_event` | `event_id`, `correlation_id` (run), `causation_id`, `sequence`, `recorded_at`, `stage`, `status`, `agent`, `subject`, `observed`, `reason_codes`, `document` + hash |

Semantic rules on top of the schemas:
- **Snapshot:** prices are above zero, bid ≤ ask, synthetic fixtures are labelled.
- **Signal:** expiry is after creation; quantity is above zero; a limit price is given
  only for limit orders; every observation must exactly equal the referenced snapshot
  value.
- **Risk decision:** it can be `allowed` only if every check passed.
- **Journal event:** its document hash must match, and its document must validate for its
  stage.

## Exact decimals

Prices, quantities and money are JSON **strings** parsed with `Decimal`.
- Prices and quantities allow up to 8 decimal places; money allows up to 2.
- Floats, exponents (`1e3`), `NaN`, `Infinity`, leading zeros, signs where not allowed,
  and excess precision are all rejected.
- Results are exact, for example 10 × 50.00 = 500. They are written as plain strings, with
  banker's rounding to 8 places.

## Paper configuration (`config/trading.paper.json`)

- **Fixed values:** `mode` is always `paper` and `allow_short` is always `false`.
- **Limits:** `max_order_notional`, `max_position_notional`, `max_position_fraction` (of
  equity), `max_daily_loss`, `max_orders_per_day`, `max_quantity`,
  `max_snapshot_age_seconds`, `max_signal_lifetime_seconds`.
- **Validation:** an order limit above the position limit, a fraction outside (0, 1], a
  float or a negative value are all rejected. An invalid config blocks every intent.
- **Kill switch:** it is engaged by `kill_switch.engaged: true` in the config, or by
  `trading-kill-switch engage` (which writes ignored `runtime/trading/kill-switch.json`
  atomically).
  - Config engagement cannot be released by the file.
  - An unreadable switch file counts as engaged.

## Risk checks (in order)

1. `kill_switch`
2. `inputs_valid`: a missing or invalid config, snapshot, signal or portfolio blocks.
3. `snapshot_fresh`: the stricter of the snapshot's and the config's max age; future data
   is blocked.
4. `signal_active`: blocks if expired, created in the future, or with a lifetime above
   the limit.
5. `symbol_allowed`
6. `symbol_consistent`
7. `order_type_allowed`
8. `quantity_limit`
9. `order_notional_limit`: quantity × last price for market orders, or × limit price for
   limit orders.
10. `position_notional_limit`
11. `position_fraction_limit`
12. `no_short_position`
13. `daily_loss_limit`: blocks once the loss reaches the limit, or if the portfolio
    belongs to another day.
14. `orders_per_day_limit`
15. `signal_not_reused`

If inputs are missing, the later checks are `skipped` and the outcome is `blocked`. The
same inputs always give the same decision and `decision_id`.

## Journal

- **Location:** `runtime/trading/journal/<run_id>/<sequence>-<event_id>.json`, which git
  ignores.
- **Writes:** each event is validated, credential-checked, fsynced and published with an
  exclusive hard link. Partial events never appear and no event is overwritten.
- **Rejected:**
  - duplicate event IDs, runs or sequence numbers;
  - recognized credentials, such as broker-style key/secret/token/password/account fields,
    `sk-`/`AKIA` key shapes and active secret environment values;
  - documents that don't match their hash.
- **Failures:** a write failure stops the run with `journal_write_failed`, and errors never
  include paths, input values or raw exceptions.
- **Reading:** `trading-journal RUN_ID` re-validates every event and shows a timeline of
  which agent saw what, why it acted, and which risk checks passed or failed with their
  limits.

## Demo scenarios (`examples/trading/`, all `SYNTHETIC FIXTURE`, symbol `SYNTH1`)

| Scenario | Expected result |
|---|---|
| `allowed` | `authorized_paper` (not executed) |
| `exposure-breach` | blocked: `position_exposure_exceeded` |
| `daily-loss` | blocked: `daily_loss_limit_reached` |
| `stale-data` | blocked: `snapshot_stale` |
| `invalid-money` | blocked: `invalid_or_missing_inputs`, `invalid_market_snapshot` (float price) |
| `duplicate-signal` | first `authorized_paper`, second blocked: `duplicate_signal` |
| `pending-exposure` | alone: `authorized_paper`. After `allowed` on the same account: blocked `position_exposure_exceeded` (pending reservation) |

Each scenario has its own signal ID. Because the state persists, running any scenario a
second time on the same account is blocked with `duplicate_signal`. Use
`trading-state init --account NAME` to start fresh.

Decisions use each scenario's simulated clock (`as_of`). All fixtures use the same
`as_of`, so they can share an account.
Journal `recorded_at` uses the real clock.

## Persistent paper state (Step 24)

### Paper intents are not trades

| Term | Meaning in ViceKrack today |
|---|---|
| **Authorized paper intent** | The risk engine allowed it, and a reservation is held. Nothing is sent anywhere. |
| **Submitted order** | Never happens. Every intent has `submitted: false` and the ledger shows `submitted_orders: 0`. |
| **Executed trade** | Never happens. `executed: false` and `executed_trades: 0`. |
| **Realized P&L** | Not tracked: `realized_pnl: null`. The portfolio's `day.pnl` is a synthetic fixture input. |

### Account state

Accounts live in `runtime/trading/accounts/acct-<name>/` (ignored by git).

| File | Purpose |
|---|---|
| `state.json` | `paper_account_state` 1.0, validated with a self-hash, revision, schema and consistency checks |
| `pending.json` | `paper_state_pending` 1.0 write-ahead note, present only while an operation is unfinished |
| `account.lock` | the cross-process OS lock |

`state.json` holds:
- `processed_signals`: every signal ID ever decided, with its outcome (`authorized_paper`,
  `blocked` or `rolled_back`).
- `intents`: authorized paper intents, each with a reservation that is `active`, or
  `released` together with its reason and note.
- `trading_day`: the timezone, `rollover: local_midnight`, the current date, counters and
  the last decision time. Earlier days are kept in `day_history`.
- `recoveries`: what each recovery decided.
- `ledger`: counts, with `submitted_orders` 0, `executed_trades` 0 and `realized_pnl` null.

Accounts must be created explicitly with `trading-state init` (`--account`, `--timezone`).

### One authorization = one locked operation

While holding the account lock, an authorization:
1. Loads and validates the state. It blocks if `pending.json` exists, or the state is
   missing, corrupt or incompatible.
2. Applies rollover.
3. Checks for a duplicate signal.
4. Runs every risk check, with the active reservations added to the position.
5. Writes `pending.json`, then the journal events, then atomically replaces
   `state.json`, then removes `pending.json`.

Blocked signals are recorded too, so they are never re-evaluated. Invalid signals have no
trustworthy ID, so they are blocked without touching state.

### Trading day and rollover

- **Timezone:** the default is `America/New_York` (daylight saving handled by `zoneinfo`
  and `tzdata`). The day rolls over at local midnight.
- **On the first decision of a new local date:** the previous day's counters move into
  `day_history`, and `authorized_count`, `cancelled_count` and `blocked_count` reset.
- **Never cleared by a day change:** processed signals (duplicate protection) and active
  reservations.
- **Clock going backwards:** a decision time more than 5 seconds before the last one, or
  an earlier local date, blocks with `clock_regression`.
- **Portfolio date:** the portfolio's `day.trading_date` must equal the account's local
  trading date.
- **Orders per day:** this counts authorizations on the account's trading day.
  Cancelling does not un-count one.

### Cancellation

```
python -m vicekrack trading-state cancel INTENT_ID --reason operator_request --note "Demo cancel"
```

- **Allowed for:** only `authorized_paper` intents with an active reservation, which
  always means unsubmitted.
- **Effect:** the reservation is released with its time, reason code and note, and a
  journal `intent_cancelled` event is written. The intent stays in history.
- **The signal stays processed,** so it cannot be authorized again.

### Recovery

`trading-state recover` takes the account lock and works through these steps:
1. **Clean up** orphan `*.tmp` files.
2. **Resolve the pending note**, by revision:
   - **committed:** `state.revision` equals the note's `revision_after` and the hash
     matches.
   - **rolled_back:** the revision equals `revision_before`. For an authorization, the
     signal is marked processed as `rolled_back`.
   - **discarded_unreadable:** the note itself is damaged.
   - **already_recovered:** a recovery record for this operation already exists, for
     example after a crash during recovery.
   - **conflict:** anything else gives `state_recovery_conflict` and nothing changes.
3. **Reconcile the journal.** Any `order_intent` recorded for this account whose intent is
   missing from state has its signal marked processed. A crash can never lead to the same
   signal being authorized twice.
4. **Record the recovery** in state and in the journal (`state_recovery`).

Corrupted or incompatible `state.json` is never repaired automatically.

## Limitations

- **Synthetic data only.** There are no market feeds, indicators, strategies or AI
  trading decisions; signals are hard-coded in fixtures.
- **Nothing is executed.** There is no broker, no order submission, no fills, no
  positions and no realized P&L. Reservations stay active until cancelled; there is no
  automatic expiry, which is the conservative choice. Portfolio positions and daily P&L
  still come from fixture inputs.
- **Limited scope.** Only USD, long-only, with market and limit order types. Each account
  holds at most 10,000 processed signals and authorized intents; beyond that it blocks
  with `state_capacity_reached` and you start a new account.
- **Local only.** The lock covers processes on one machine and one disk, not network
  shares. Recovery reads the whole local journal.
- **No dashboard yet.** The journal and account state are ready for one.
