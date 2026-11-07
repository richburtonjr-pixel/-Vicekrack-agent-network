# Trade desk: eight agents checking each other (Trading PR 3)

> **Simulated money only.** Every practice trade must be approved by every agent, and the
> practice account's own $80 guard still runs last and can still say no.

Think of a **pre-flight checklist with a co-pilot**. The Strategy agent spots a possible trade,
but it can't buy anything. The idea goes into a **case file** that is passed down the line. Each
agent signs it, and any one of them can stop it.

## The eight agents

| # | Agent | Job | Built in |
|---|---|---|---|
| 1 | Market Scout | Is there fresh, complete price data? | Step 28 |
| 2 | Trend Agent | Which way is the price moving? | Step 28 |
| 3 | Strategy Agent | Did a trading rule fire on the newest bar? | Step 28 |
| 4 | Verifier | Redoes the Strategy agent's math from raw prices. Disagree = veto | **PR 3** |
| 5 | Risk Review | Research complete? Then the account check: guard ok, at least $10 above the floor, not already holding, daily and losing-streak limits | Step 28 + **PR 3** |
| 6 | Executor | The only code that buys or sells, and only when every signature says approve | **PR 3** |
| 7 | Position Monitor | Every 5 minutes, for each open position: stop-loss, held too long, or trend turned down -> close | **PR 3** |
| 8 | Performance Reviewer | Closed trades, wins, losses, P&L | **PR 3** |

The $80 shutdown guard is not an agent. It's the fuse box underneath all of them (PR 1).

## A case file, start to finish

```
Entry case  ETHUSD  @ 3012.40
  market_scout     approve   data_available
  trend_agent      approve   trend_assessed
  strategy_agent   approve   active_research_signal   (breakout-3 on the last bar)
  risk_review      approve   sufficient_for_future_paper_evaluation
  verifier         approve   recomputed breakout-3 from raw prices
  risk_review      approve   account check: guard ok, $19.91 above floor
  executor         executed  simulated buy of $25
```

Exit cases are shorter: the Position Monitor asks, the Executor closes. Exits need no other
approval because they reduce risk.

## Settings (`config/trading-desk.json`)

| Setting | Default | Meaning |
|---|---|---|
| `auto_practice_trades` | `true` | `false` = agents still review and sign, but nothing is bought |
| `order_dollars` | `$25` | Size of each practice buy (the account caps it at $25 too) |
| `min_dollars_above_floor` | `$10` | No new buys when equity is under $90 |
| `risk.max_entries_per_day` | 6 | |
| `risk.reentry_wait_minutes` | 15 | Wait after closing a coin before buying it again |
| `risk.pause_after_losses` / `pause_minutes` | 3 / 120 | After 3 losing trades in a row, no buys for 2 hours |
| `monitor.stop_loss_pct` | 2% | Close if the price falls 2% below what was paid |
| `monitor.max_hold_minutes` | 60 | Close after an hour no matter what |
| `verifier.max_last_bar_move_pct` | 3% | Don't buy right after a sudden spike |

## Seeing it

- `python -m vicekrack desk-cases` lists recent case files: who approved, who vetoed, and why.
- `watch-status` shows this cycle's cases and the Performance Reviewer's numbers.
- Files: `runtime/trading/desk/cases-YYYY-MM-DD.jsonl`.

## Honest expectations

On a random-walk test market the desk lost 9 out of 9 trades, about $0.19 each. That's mostly
fees and slippage (about 0.7% for a round trip). The rules are short-term and simple, so costs
are a real hurdle. That's what practice mode is for: watching how the rules do on real prices
before changing anything.
