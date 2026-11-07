# Practice account and the $80 shutdown (Trading PR 1)

> **Simulated money only.** No broker, no real orders, no leverage, no shorting. The
> $80 shutdown is a trigger, not a guaranteed maximum loss.

Think of this as the **fuse box** for the trading department. The agents (next PRs) are the
appliances; they can ask for power, but they can't open the fuse box. When the account's value
drops to $80, the fuse trips: every position is closed and the account stops until a person
resets it.

## What it is

| Part | File | Job |
|---|---|---|
| Settings | `config/practice-account.json` | $100 start, $80 floor, BTCUSD + ETHUSD only, $25 max per buy, 2 open positions max, prices older than 15 minutes don't count |
| Guard | `vicekrack/trading/practice/guard.py` | equity = cash + (each position x its latest price). At or below $80 = shutdown |
| Account | `vicekrack/trading/practice/account.py` | cash, positions, buys and sells; runs the guard before every change |
| Storage | `vicekrack/trading/practice/store.py` | saves `runtime/trading/practice/` with one lock so the watch loop, phone and commands never collide |

Fill prices, fees and fractional quantities reuse the offline simulator's rules
(10 bps slippage, 25 bps fee), so practice results match simulation results.

## Rules enforced by code

- **Floor:** equity at or below $80 closes every position at the latest price and stops the
  account. Prices after that don't restart it.
- **Restart is a person's decision:** `practice-restart --confirm restart-practice`. The stopped
  account is kept in `runtime/trading/practice/history/`.
- **No leverage:** cash can never go below zero.
- **No guessing:** if an open position has no price, or its price is over 15 minutes old,
  equity can't be verified, so new buys are blocked. Nothing is closed on a guess.
- **Costs count:** a buy is refused if its own slippage and fee would put equity at the floor.
- **Settings can't be loosened quietly:** leverage, shorting, a floor at or above the start, or
  any shutdown action other than `close_all_and_halt` are refused. Changing the settings while an
  account runs locks it until a person restarts.
- **Independent from AI:** a test checks the guard, account and storage import nothing from the
  agents, AI providers, brokers or the network.

Why $80 isn't a guaranteed floor: the guard checks whenever a new price arrives. If the price
jumps down between two checks, the account closes below $80, and closing costs slippage and fees.
The ledger records both the equity that triggered the stop and the cash after closing.

## Commands (for checking by hand; the watch loop will use the same code)

```powershell
.\.venv\Scripts\python.exe -m vicekrack practice-start
.\.venv\Scripts\python.exe -m vicekrack practice-status
.\.venv\Scripts\python.exe -m vicekrack practice-price BTCUSD 90000.00
.\.venv\Scripts\python.exe -m vicekrack practice-buy BTCUSD 25.00 --price 90000.00
.\.venv\Scripts\python.exe -m vicekrack practice-sell BTCUSD --price 91000.00
.\.venv\Scripts\python.exe -m vicekrack practice-ledger
.\.venv\Scripts\python.exe -m vicekrack practice-restart --confirm restart-practice
```

## Not in this PR

Live prices, the 24/7 watch loop, the agents placing practice trades, and HQ/phone screens.
Those are the next PRs.
