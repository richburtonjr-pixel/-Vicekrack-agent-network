# 24/7 watch loop (Trading PR 2)

> Free Kraken public prices (no keys, no account), the rule-based agents, and the practice
> account's $80 shutdown check. Since PR 3, approved ideas become **practice trades with
> simulated money** through the [trade desk](trade-desk.md).

If the practice account is the fuse box, this is the **night watchman**: every 5 minutes it
walks the floor, writes down the BTC and ETH prices, asks the four research agents what they
see, and checks the fuse. Then it hands the floor to the [trade desk](trade-desk.md).

## What happens every 5 minutes

| Step | What | Reuses |
|---|---|---|
| 1 | Fetch the newest closed 5-minute bars for BTC (Kraken `XBTUSD`) and ETH | Kraken public adapter (PR #67) |
| 2 | Save them as `runtime/trading/watch/bars/<COIN>.csv` and load them through the normal importer, labelled `delayed`, source `kraken-<PAIR>-5m` | local CSV importer and dataset validator (Step 25) |
| 3 | Market Scout -> Trend -> Strategy -> Risk Review at the last bar close | research agents (Step 28) |
| 4 | Hand the latest closes to the practice account | practice account and $80 guard (PR #68) |
| 5 | Trade desk: Position Monitor checks open positions; approved ideas become practice trades | trade desk (PR 3) |
| 6 | Save `runtime/trading/watch/status.json` and add a line to `cycles.jsonl` | new |

- Cycles start about 20 seconds after each 5-minute bar closes.
- One coin failing (internet hiccup, Kraken busy) is reported and skipped. Its price gets older,
  and after 15 minutes the guard blocks new buys. The other coin and the guard keep going.
- A bad cycle never ends the loop. Only one loop can run at a time.
- `cycles.jsonl` rolls over at 5 MB, keeping one older file.

## Running it on the PC

**The easy way (one click, or zero):** **`Start ViceKrack (PC + Phone).bat`** now starts the Trading
Watch too, in the background, alongside the HQ and the phone app. If the watch ever stops, it's
started again by itself (5 seconds later, or 2 minutes later if it stopped right away). Its lines go
to `runtime\trading\watch\watch.log`. Run **`Turn on auto-start.bat`** once and all of it starts
whenever you sign in to Windows, so the agents run 24/7 while the PC is on.

- While ViceKrack runs, Windows is asked **not to sleep** (the screen can still turn off).
- Close the ViceKrack window to stop everything. The PC can sleep again.
- To start ViceKrack without the watch: `python -m vicekrack mobile-start --no-watch`.

**The old way still works:** **`Start Trading Watch.bat`** runs the watch in its own window. Only one
watch can run at a time, so if both are open the background one just waits and takes over when the
window is closed.

Commands, if you ever want them:

```powershell
.\.venv\Scripts\python.exe -m vicekrack watch-once      # one cycle now
.\.venv\Scripts\python.exe -m vicekrack watch-run       # keep going
.\.venv\Scripts\python.exe -m vicekrack watch-status    # last cycle
```

## Limits

- Prices are closed 5-minute bars, so they are up to about 5 minutes behind the market.
- Kraken data is free and public, not verified or licensed; treat it as practice data.
- The guard checks once per cycle. A fast crash between cycles can land the account below $80.
