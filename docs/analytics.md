# Simulation analytics (Step 30)

> **SIMULATED, DESCRIPTIVE, READ-ONLY.** An analytics report describes what happened in
> one saved Step 29 offline simulation, on one historical dataset, under one policy.
> - It is **never annualized** and is **not a prediction** of future performance, a
>   profitability claim or advice.
> - It changes nothing: the simulation run, the dataset, Step 24 paper accounts and every
>   kill switch are left exactly as they were.
> - It has no live feed, broker, AI call, optimization or background work.

## Commands

```powershell
.\.venv\Scripts\python.exe -m vicekrack market-import --adapter synthetic --fixture synth1-5m-reclaim
.\.venv\Scripts\python.exe -m vicekrack sim-run DATASET_ID --save
.\.venv\Scripts\python.exe -m vicekrack analytics-generate RUN_ID
.\.venv\Scripts\python.exe -m vicekrack analytics-generate RUN_ID --save
.\.venv\Scripts\python.exe -m vicekrack analytics-list
.\.venv\Scripts\python.exe -m vicekrack analytics-inspect REPORT_ID
.\.venv\Scripts\python.exe -m vicekrack analytics-inspect REPORT_ID --section equity_curve
```

On Linux/macOS use `.venv/bin/python`. `analytics-generate` without `--save` only
previews. The `--section` options are `account`, `closed_trades`, `open_positions`,
`equity_curve`, `drawdown`, `holding`, `exposure`, `orders` and `attribution`.

Output is bounded by `config/analytics.json`:
- `max_cli_items` (50): the most items any list shows; long lists show their last items
  and say how many there are;
- `max_curve_points` (5,000) and `max_trades` (1,000): larger runs are **refused**
  (`analytics_too_many_points`, `analytics_too_many_trades`), never silently truncated.

## Inputs and tamper checks

1. The run is loaded from `runtime/trading/simulation/runs/` and fully re-validated by
   Step 29 (`sim_run_corrupt` if edited). `build_report` validates it again.
2. Its dataset is loaded and re-validated (`dataset_corrupt` if edited). The dataset ID,
   bars hash and source-file hash must equal the run's provenance
   (`analytics_dataset_mismatch`).
3. Every fill must match the bar it claims (bar start and open price), and the rebuilt
   ending cash, equity, unrealized P&L and bar count must equal the run's own summary
   (`analytics_inconsistent`).

The report records the run ID, run results hash, policy hash, dataset ID, bars hash and
analytics-config hash. `report_id` (`sarp-…`) is derived from those, so the same inputs
always give the same report (only `created_at` differs).

## Equity reconstruction (no future data)

Analytics runs its own bounded Step 25 replay over the run's replay window. For each newly
closed bar *k*, in order:

1. apply the validated fills executed at bar *k*'s open (cash += the fill's cash change;
   shares ± quantity);
2. equity_k = cash_k + shares_k × close_k.

Only bars already closed at that simulated time are used; a bar from the future raises
`future_bar_leak`. The curve starts with one point at the replay start holding the initial
cash (`sequence: null`).

## Formulas

All arithmetic is exact decimal. Ratios and percentages are rounded half-even to 8
decimal places; money values come from the run and are not re-rounded.

| Metric | Formula | Unavailable when |
|---|---|---|
| Gross P&L (trade) | exit notional − entry notional | — |
| Fees (trade) | entry fee + exit fee | — |
| Net P&L (trade) | gross − fees (equals Step 29 realized P&L) | — |
| Outcome | win if net > 0, loss if net < 0, else breakeven | — |
| Win rate % | wins ÷ closed trades × 100 | `no_closed_trades` |
| Average net / expectancy | Σ net ÷ closed trades (same value: average result per closed trade) | `no_closed_trades` |
| Average win | Σ net of wins ÷ wins | `no_closed_trades` / `no_winning_trades` |
| Average loss | Σ net of losses ÷ losses (negative) | `no_closed_trades` / `no_losing_trades` |
| Profit factor | Σ net of wins ÷ \|Σ net of losses\| (0 is real when there are losses and no wins) | `no_closed_trades` / `no_losing_trades` |
| Net return | ending equity − initial cash | — |
| Net return % | net return ÷ initial cash × 100, **not annualized** | — |
| Drawdown | running peak equity − equity (the peak includes the starting cash) | — |
| Drawdown % | drawdown ÷ running peak × 100 | — |
| Max drawdown | largest drawdown in $ and largest in %, each with the first time it occurred | — |
| Holding | closed trades' bars held (average, min, max) and average seconds from entry fill to exit fill | `no_closed_trades` |
| Exposure % | closed bars that ended with a position ÷ closed bars × 100 | `no_bars_processed` |

An undefined metric is `{"status": "unavailable", "value": null, "reason": …}`. It is never
replaced with zero or infinity. A real zero (for example a 0% win rate after one losing
trade) is reported as available.

**Costs are counted once.** Entry and exit fees are in every closed trade's net P&L; the
entry fee of an open position is in its cost basis. Unrealized P&L is quantity × last
close − cost basis, with no hypothetical exit costs. So net return = realized +
unrealized P&L.

**Open positions are separate.** They appear in `open_positions` with their mark and
unrealized P&L, and never count as wins, losses or closed trades.

## Orders

Counts of created, filled, rejected and `pending_at_end_of_data` orders, rejections by
reason code, and the list of orders still pending when the data ended.

## Strategy attribution

Each closed trade and open position is attributed to the strategy whose signal created its
entry order. Per strategy: closed trades, wins, losses, breakeven, net P&L, fees, win rate,
open positions, unrealized P&L, accepted and rejected entry orders, and
`blocked_by_shared_account` (rejected with `position_already_open` or
`pending_order_exists`).

**Shared accounts.** When a policy lists more than one strategy, they all traded one
simulation account with one cash balance and at most one position. One strategy's open
position or pending order can block another's signals, and they compete for cash. The
report sets `shared_account: true` and explains that per-strategy figures are each
strategy's share of one shared run, **not independent strategy tests**. To compare
strategies, run one simulation per strategy on the same dataset.

## Storage

Reports are saved atomically in ignored `runtime/trading/analytics/reports/` (temp file,
fsync, exclusive link). Saving the same report twice gives `report_exists`; nothing is
overwritten. Every read re-validates the schema, the results hash and internal
consistency, and gives `report_corrupt` if edited.

## Limitations

- **One run, one dataset, one policy.** A short synthetic run says nothing about other
  periods, symbols or market conditions.
- **No risk-adjusted ratios.** Sharpe, Sortino, CAGR and other annualized or volatility
  measures are deliberately left out, because short simulated runs can't support them.
- **It inherits Step 29's model:** next-open fills, a fixed slippage and fee model, no
  liquidity or market impact, long-only, one position at a time.
- **Unrealized P&L ignores exit costs**, and open positions are not liquidated.
- **Equity is sampled at bar closes only**; intrabar drawdown isn't measured.
- No optimization, live data or brokers. The Living HQ [trading results desk](hq.md#trading-results-desk-step-34)
  (Step 34) only displays saved reports; it never generates them.
