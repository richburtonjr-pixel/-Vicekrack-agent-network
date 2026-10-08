# Trading research sessions (Step 39)

> **OFFLINE and SIMULATED.** A session runs the existing trading components on one stored
> historical dataset in a fixed order:
> - it has no live feed, broker, real or paper-account order, AI call, scheduling or
>   optimization;
> - research conclusions are labelled with their simulated as-of time and **never** reach
>   the simulator, which decides only with its own validated policy;
> - nothing here authorizes anything.
>
> Results are not predictions, advice or profitability claims.

## Commands

```powershell
# 1. Import the synthetic demo dataset (prints its dataset ID, mds-...)
.\.venv\Scripts\python.exe -m vicekrack market-import --adapter synthetic --fixture synth1-5m-reclaim

# 2. Start a session (add --record-events to record the research and simulation timelines)
.\.venv\Scripts\python.exe -m vicekrack trading-session-start DATASET_ID --record-events

# 3. List, inspect and (only if a stage failed or the process stopped) resume
.\.venv\Scripts\python.exe -m vicekrack trading-session-list
.\.venv\Scripts\python.exe -m vicekrack trading-session-inspect SESSION_ID
.\.venv\Scripts\python.exe -m vicekrack trading-session-inspect SESSION_ID --stage research_analysis
.\.venv\Scripts\python.exe -m vicekrack trading-session-resume SESSION_ID --record-events

# 4. View it (read-only): open the printed address, then the Sessions view (or press S)
.\.venv\Scripts\python.exe -m vicekrack hq-serve
```

On Linux/macOS use `.venv/bin/python`. `trading-session-start` also accepts
`--config config/<file>.json`. Session IDs look like `tss-` followed by 24 hex characters.

With the shipped configuration and `synth1-5m-reclaim` you should see:
- the research verdict `sufficient_for_future_paper_evaluation` as of the last bar close;
- the same three simulated orders as a standalone `sim-run` (an entry filled, an exit
  filled and one entry `pending_at_end_of_data`);
- one closed trade in the analytics.

The Living HQ also has a synthetic **demo session** (always listed first, clearly
labelled), so the Sessions view can be explored without saving anything.

## The five stages (fixed order, never extended)

| # | Stage | Uses | Artifact | Also saved in |
|---|---|---|---|---|
| 1 | `dataset_validation` | Step 25 `MarketStore.load` (re-validates bars and hashes) | `trading_session_dataset_check` | session only |
| 2 | `research_analysis` | Step 28 `run_workflow` at the configured as-of time | `research_agent_run` | `runtime/trading/agents/` |
| 3 | `simulation` | Step 29 `run_simulation` with the session's policy | `simulation_run` | `runtime/trading/simulation/runs/` |
| 4 | `performance_analytics` | Step 30 `build_report` | `simulation_analytics_report` | `runtime/trading/analytics/reports/` |
| 5 | `hq_summary` | the session manifest | `trading_session_manifest` | session only |

Stage 1 checks that the dataset still matches the session record. It also checks that it
fits the policy's `max_bars`, that an explicit `research.as_of` lies inside the data, and
that an explicit simulation window lies inside the data.

## Configuration (`config/trading-session.json`, `trading_session_config` 1.0)

| Field | Meaning |
|---|---|
| `research.as_of` | `dataset_end` (the last bar close) or an exact UTC time. Research uses only bars closed by then. |
| `research.strategies` | `null` (the research-agent config's list) or an explicit list. |
| `simulation.policy` | The simulation policy file (default `config/simulation.paper.json`). |
| `simulation.start` / `end` / `step_seconds` | Optional replay window, as in `sim-run`. |
| `limits.max_attempts_per_stage` | 1–5 attempts per stage, counting explicit resumes. |

At start, the session copies every configuration it uses into its record, with their
SHA-256 hashes: this file, market data, indicators, research signals, research agents, the
simulation policy and analytics. It also records the component versions: contract
versions from the shipped schemas and the research handler identities.

The session ID is derived from the dataset, those hashes and those versions. Starting the
same thing twice is refused (`session_exists`); a changed configuration is a new session.

## Timing and integrity

The summary keeps four labelled time domains:

| Domain | What it is |
|---|---|
| Historical data | The stored dataset's first bar start and last bar close (not live, not verified as authentic). |
| Historical research | The exact simulated as-of time of the research conclusions, and how it relates to the simulation window: `before_simulation_start`, `during_simulation_window` or `at_or_after_simulation_end`. |
| Simulated execution | The simulator's replay window in simulated market time. |
| Wall clock | Real time on this computer: when the session was created and when each stage attempt started and ended. |

- **No future data.** Research and the simulator use the existing closed-bar replay code
  for indicators and signals. Research sees only bars closed by its as-of time, and the
  simulator sees only bars closed by each simulated moment.
- **Research never reaches the simulator.** With the default `dataset_end`, research
  conclusions come from the end of the data. The session passes nothing from stage 2 to
  stage 3, so they are never used for earlier simulated decisions. The simulator receives
  only the dataset, its policy, the shared indicator and signal configuration and its own
  kill switch, exactly like `sim-run`. Its run ID and results hash equal a standalone run's.
  Tests check this, and also that later bars cannot change earlier research or decisions.
- **Hashes recorded:** the dataset (bars and source file), every configuration snapshot,
  every artifact's file SHA-256 and results hash, and each checkpoint revision (chained to
  the previous one).

## Session control

- **Fixed stage limit:** five stages, at most once per invocation, plus a per-stage attempt
  limit.
- **Explicit failures:** a failed stage records its fixed error code and stops the
  session (`failed`). Later stages stay `pending`, and nothing is saved for the failed
  stage. A research workflow that ends `failed` is a stage failure
  (`research_workflow_failed`).
- **No automatic retries.** Only `trading-session-resume` tries again. It starts from the
  first incomplete stage, with attempt reason `retry_after_failure` or
  `retry_after_interruption`.
- **Checks before resume:** resume first re-validates the session record and the
  checkpoint chain. It then checks every completed artifact (file hash, contract, results
  hash, links to the dataset and earlier stages), the dataset itself, every configuration
  file and the component versions. Any difference stops it before anything runs:
  - `session_artifact_tampered`;
  - `session_artifact_unexpected`;
  - `session_dataset_changed`;
  - `session_config_changed` (which names the changed inputs);
  - `component_version_changed`.
- **One process per session.** An OS lock (`session.lock`) is held for the whole start or
  resume, and a second process gets `session_busy`. The OS releases the lock if the
  process dies.
- **Duplicate prevention:**
  - Completed stages never run again.
  - Artifacts are published with an exclusive link and never overwritten.
  - Records already in the shared stores are kept as they are when their results hash
    matches (`already_present`). A different record is a conflict
    (`session_store_conflict`).
- **Crash between artifact and checkpoint.** Each stage first checkpoints its intent
  (`running`), then publishes its artifact, then checkpoints `completed`.
  - If the process dies after publishing, resume finds the artifact, validates it and
    adopts it (attempt outcome `recovered`). The stage is not run again.
  - If it died before publishing, the attempt is marked `interrupted` and the resume runs
    the stage again as a new attempt.
- **Storage:** checkpoints are written to a temporary file, fsynced, then atomically
  replaced. Sessions live in `runtime/trading/sessions/` (ignored by Git). Source datasets,
  paper accounts and earlier results are never modified.

Status shown by `trading-session-list` and the HQ:

| Status | Meaning |
|---|---|
| `running` | A process holds the lock (same machine). |
| `completed` | All five stages are done. |
| `failed` | A stage failed. |
| `interrupted` | The checkpoint says a stage was running, but no process holds the lock. |

## Events

With `--record-events` the research and simulation stages record their normal Step 31
timelines, with the session's correlation ID, so the HQ groups their attempts.
- **A recording failure** fails that stage (`event_persistence_failed`). Earlier stages
  stay committed, nothing is saved for the failed stage, and a resume makes a new attempt
  with a new timeline.
- **No events:** dataset validation, analytics and the summary are not instrumented.

## Living HQ: Sessions view

The Sessions view is read-only. It uses `GET /api/sessions` and `GET /api/session?id=`.
- **Choose a session.** The selector lists the demo session first, then saved sessions.
- **Summary:** status, the data source of the summary, an integrity check, the time
  domains, the results, and the five stages with every attempt's wall-clock times and
  verification.
- **Links:** three links open views that already exist:
  - **Research rooms**: the research timeline in the four upstairs rooms;
  - **Simulator station**: the simulation timeline at the Simulator operations station;
  - **Analytics desk**: the same simulation timeline in the trading results desk
    (completed-run summary).

  A link uses the completed attempt's recorded timeline when it is complete. Otherwise it
  uses the saved run's reconstructed timeline. Each link is loaded and checked against the
  session's own run ID before it is offered; otherwise it is shown as unavailable with a
  reason.
- **Data sources:** completed sessions are summarized from their verified manifest.
  Incomplete ones are summarized from the verified checkpoint and their completed artifacts
  only. If any artifact fails validation, the view shows the integrity problem and no
  results or links.
- **No merged history.** Each stage's timeline keeps its own event order and timestamps,
  and gaps between stages are not filled in.
- **Read-only.** The view has no start, resume or retry control; it shows the terminal
  commands as text. It never takes the session lock and never writes.

## Limitations

- **One dataset, one configuration.** There is no comparison across sessions, no
  multi-symbol data, and no parameter sweeps or strategy optimization.
- **Fixed research as-of time.** Research runs once, at one as-of time. Research at every
  simulated decision is not offered, and its conclusions would still not feed the
  simulator.
- **Timestamps:** wall-clock times are whole seconds, from this computer's clock.
- **Liveness:** it is same-machine only, through the OS lock.
- **Manual recovery:** a session whose files fail validation cannot be resumed, and starting
  the same dataset and configuration again is refused because the session ID already
  exists. Restore the files, or move the session folder aside yourself and start again.
