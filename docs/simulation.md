# Offline paper-execution simulation (Step 29)

> **SIMULATED.** The simulator replays stored historical bars and turns Step 27 research
> signals into *simulated* orders, fills and positions in its own in-run account.
> - It has no broker, live feed or live order.
> - It never touches Step 24 paper accounts, the paper risk engine or paper intents, and it
>   never reads Step 28 research-agent verdicts.
> - A research signal, or a "sufficient" agent verdict, grants no permission anywhere
>   else.
>
> Results are not predictions, advice or profitability claims.

## Commands

```powershell
.\.venv\Scripts\python.exe -m vicekrack market-import --adapter synthetic --fixture synth1-5m-reclaim
.\.venv\Scripts\python.exe -m vicekrack market-list
.\.venv\Scripts\python.exe -m vicekrack sim-run DATASET_ID
.\.venv\Scripts\python.exe -m vicekrack sim-run DATASET_ID --save
.\.venv\Scripts\python.exe -m vicekrack sim-list
.\.venv\Scripts\python.exe -m vicekrack sim-inspect RUN_ID --section orders
.\.venv\Scripts\python.exe -m vicekrack sim-inspect RUN_ID --section ledger
.\.venv\Scripts\python.exe -m vicekrack sim-kill-switch engage
.\.venv\Scripts\python.exe -m vicekrack sim-kill-switch release
```

On Linux/macOS use `.venv/bin/python`. The `--section` options are `orders`, `fills`,
`ledger` and `positions`. `sim-run` also accepts `--policy`, `--start`, `--end` and
`--step-seconds`. With the shipped policy and `synth1-5m-reclaim` you should see:
- an `ema-cross-3-5` entry on bar 7, filled at bar 8's open;
- an opposite-crossover exit decided on bar 8, filled at bar 9's open;
- a second entry on bar 10 that stays `pending_at_end_of_data`, because no later bar
  exists.

## Policy (`config/simulation.paper.json`, `simulation_policy` 1.0)

The simulator acts on research signals only through this explicit, validated and hashed
policy.

| Section | Meaning |
|---|---|
| `account.initial_cash` | The starting cash of the in-run simulation account (`simacct-…`), in USD |
| `entry.strategies` | The Step 27 research strategies whose signals may become simulated entries |
| `sizing` | `fixed_quantity` (whole shares), or `fixed_notional`: floor(notional ÷ reference price) shares; 0 shares is rejected (`sizing_zero_quantity`) |
| `exits.opposite_ema_crossover` | `{fast, slow}`, or `null` |
| `exits.max_holding_bars` | a whole number, or `null` |
| `costs` | `slippage_bps`, `fee_per_order`, `fee_bps` |
| `limits` | `max_order_notional`, `max_position_notional`, `max_orders` (accepted orders per run), `max_bars` |
| `kill_switch.engaged` | blocks new simulated entries; `sim-kill-switch` controls a separate local switch file, and an unreadable switch file counts as engaged |

Scope: long-only market orders, whole shares, one symbol per run, and at most one position
at a time.

## Timing: no future data, no intrabar prices

The simulator runs one bounded Step 25 replay. For each newly closed bar, in order:

1. **Fill pending orders at this bar's OPEN.** An order can fill only on a bar starting at
   or after its `not_before_utc`, which is the simulated time it was decided. A signal
   found on bar t therefore fills at the open of the next available bar, never earlier.
   The bar's high, low and close are never used for a fill.
2. **After the bar closes:**
   - count the bar for any open position;
   - check the exit rules on this closed bar;
   - turn research signals detected on this bar into entry orders, or reject them.
3. **At the end:** orders still waiting are `pending_at_end_of_data`. Open positions are
   **marked to the last close and never sold off.**

With the default step (one interval), the fill bar is always t + 1. With a coarser
`--step-seconds`, decisions are made later, so fills move later and signals may expire
first. That's expected.

## Prices, fees, cash and P&L

All figures are exact decimals:

| Item | Formula |
|---|---|
| Buy fill price | open × (1 + slippage_bps / 10,000), rounded half-even to 8 places |
| Sell fill price | open × (1 − slippage_bps / 10,000), rounded half-even to 8 places |
| Notional | quantity × fill price (exact) |
| Fee | fee_per_order + notional × fee_bps / 10,000, rounded half-even to cents |
| Cash change | buy: −(notional + fee); sell: +notional − fee |
| Cost basis | buy notional + buy fee (fees are capitalised) |
| Realized P&L | sale proceeds − sell fee − cost basis (whole position) |
| Unrealized P&L | quantity × last close − cost basis (no hypothetical exit costs) |
| Ending equity | cash + quantity × last close |

The cash ledger (`cash_ledger_entry` 1.0) records the initial cash and every buy, sell and
fee, each with its running balance.

## Order acceptance and rejection

Every order keeps a `history` of `accepted`, `rejected`, `filled` or
`pending_at_end_of_data`, with reason codes.

**When a signal arrives**, an entry is rejected for any of these reasons (all reasons are
listed):

| Reason | Meaning |
|---|---|
| `kill_switch_engaged` | the simulation kill switch is on |
| `signal_expired` | the signal expired before or when it was detected |
| `duplicate_signal` | the signal was already used |
| `position_already_open` | the one allowed position is already open |
| `pending_order_exists` | the pending-exposure check |
| `order_limit_reached` | `max_orders` accepted orders already |
| `sizing_zero_quantity` | sizing gives less than one share |
| `order_notional_limit` | estimated at the signal bar's close plus slippage |
| `position_exposure_limit` | estimated in the same way |
| `insufficient_cash_estimate` | estimated in the same way |

**When the order would fill**, it is checked again at the actual fill price:

| Reason | Meaning |
|---|---|
| `entry_gap` | a missing interval between the decision and the fill bar; entries never fill across gaps |
| `signal_expired_before_fill` | the fill bar starts at or after the signal's expiry |
| `order_notional_limit_at_fill` | the fill notional exceeds the order limit |
| `position_exposure_limit_at_fill` | the fill notional exceeds the position limit |
| `insufficient_cash` | cash is less than notional + fee |

A rejected order is **never resized**; its quantity stays as decided.

**Exits:**
- They sell the whole position and are never blocked by the kill switch or `max_orders`,
  because they reduce risk.
- **Opposite EMA crossover:** fast(t−1) ≥ slow(t−1) and fast(t) < slow(t), using published
  Step 26 values (gap policy `reset`). Both bars must be ready and consecutive.
- **Maximum holding:** the bars held, counting the fill bar, reach `max_holding_bars`.
- An exit fills at the next available open even after a gap; the fill records
  `gap_before_fill` with the number of missing intervals.

**Duplicates:** deterministic IDs (`srun-`, `sord-`, `sfil-` from the order ID) make a
second fill of the same order impossible. Each signal is used at most once, and saving the
same run twice gives `sim_run_exists`.

## Contracts (version 1.0, all `simulated: true`)

| Contract | Holds |
|---|---|
| `simulation_order` | `simulation_order` 1.0 records |
| `simulation_fill` | `simulation_fill` 1.0 records |
| `simulation_position` | `simulation_position` 1.0 records |
| `cash_ledger_entry` | `cash_ledger_entry` 1.0 records |
| `simulation_run` | the whole run, with `paper_account_access: false` and `broker: null` |

The run also contains:
- dataset provenance, the full policy and its hash, and the strategy hashes;
- the replay window and kill-switch state;
- the orders, fills, ledger and positions, and a summary (fees, realized and unrealized
  P&L, ending cash and equity, open quantity, last close);
- `results_sha256`.

Runs are saved atomically in ignored `runtime/trading/simulation/runs/`. On read they are
fully re-validated, including the fill arithmetic, ledger balances, cost basis, realized
P&L, one fill per order and fill-not-before-decision. Tampered runs give
`sim_run_corrupt`, and tampered input datasets give `dataset_corrupt`.

## Limitations

- **Narrow scope:** long-only market orders, whole shares, one symbol, one position at a
  time. There are no limit or stop orders, shorting, partial fills, partial exits or
  pyramiding.
- **A simple cost model:** fills at the next bar's open with a fixed slippage model and
  fees. There is no liquidity, volume, spread, market-impact or queue modelling, and
  volume is not checked.
- **No calendar:** overnight breaks are gaps, so entries are rejected across them while
  exits still fill.
- **Unrealized P&L ignores exit costs**, and open positions are not liquidated.
- **Offline only:** one dataset per run, with no strategy optimization, live feeds or
  brokers. Results describe a simulation, not expected performance.

To describe a saved run (trades, equity curve, drawdown, attribution), see
[Simulation analytics](analytics.md) (Step 30).
