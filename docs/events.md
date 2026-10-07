# Execution events and timeline replay (Step 31)

> **Display data only.** Events describe what a command did. Reading, listing or replaying
> a timeline never reruns an agent, a simulation or a production, never places an order,
> and never writes anything. There is no dashboard yet, no background worker and no
> automatic retry.

## Two kinds of timeline

| Origin | Where it comes from | Times |
|---|---|---|
| `recorded` (`tl-…`) | Captured live while `agent-run` or `sim-run` ran with `--record-events` | Every event has `recorded_at`, the wall-clock time it was emitted (`time_basis: recorded_at_emission`) |
| `reconstructed` (`rtl-…`) | Rebuilt on demand from a saved record: a research-agent run (`rar-…`), a simulation run (`srun-…`) or a content production (`prod-…`) | Trading runs store no per-stage times, so `recorded_at` stays **null** (`simulated_only`). Content productions keep a stage trace with the times the pipeline itself saved (`source_recorded`). |

A reconstructed timeline **never invents** a start time. Its `source.saved_at` is when the
saved record was written, and it is labelled as that.

## Commands

```powershell
.\.venv\Scripts\python.exe -m vicekrack sim-run DATASET_ID --save --record-events
.\.venv\Scripts\python.exe -m vicekrack agent-run DATASET_ID --record-events
.\.venv\Scripts\python.exe -m vicekrack events-list
.\.venv\Scripts\python.exe -m vicekrack events-list --department content --origin reconstructed
.\.venv\Scripts\python.exe -m vicekrack events-inspect TIMELINE_OR_RUN_ID
.\.venv\Scripts\python.exe -m vicekrack events-inspect RUN_ID --component trading.research.trend_agent
.\.venv\Scripts\python.exe -m vicekrack events-replay TIMELINE_OR_RUN_ID --delay-ms 300
```

On Linux/macOS use `.venv/bin/python`.
- **`events-inspect`** accepts `tl-`, `rar-`, `srun-` or `prod-` IDs. It shows at most
  `max_cli_items` (50) events; use `--from-sequence N` to page.
- **`events-replay`** prints one JSON line per event, then a summary line. It waits
  `--delay-ms` between lines (default 200, capped at 2,000) and shows at most
  `--max-events` events (capped at 500).
  - Each line's `state_at_event` is the **historical** state right after that event, not a
    claim about now.
  - The summary line repeats the timeline's completeness and its current display states.

Without `--record-events`, `agent-run` and `sim-run` behave exactly as before: same
output and same result hashes, and nothing is written under `runtime/events/`.

## Event contract (`execution_event` 1.0)

| Field | Meaning |
|---|---|
| `event_id` | `evt-` + hash of (timeline ID, sequence) |
| `origin` | `recorded` or `reconstructed` |
| `timeline_id`, `correlation_id`, `run_id` | the timeline, a grouping ID, and the domain run (`rar-`, `srun-`, `prod-`), when known |
| `department` | `trading` or `content`; a component must belong to its own department |
| `component` | e.g. `trading.research.trend_agent`, `trading.simulation.engine`, `content.production.creator` |
| `stage`, `sequence` | the stage name; sequence numbers run 1..N without gaps |
| `event_type`, `status` | see below; each type allows only its own statuses |
| `sim_time_utc` | the simulated time, where one applies |
| `recorded_at` | wall-clock time for recorded events (see the table above) |
| `reason_codes` | at most 10 fixed codes |
| `refs` | at most 6 references by **ID only**: dataset, run, order, fill, research signal, production |
| `details` | a closed set of fields: purpose, side, quantity, strategy, rule, bar sequence, conclusion, verdict, position |

**Never included:** prompts, source or article text, credentials, environment values, raw
exception messages, or file paths. A strict schema (no extra fields, code and ID patterns)
plus the shared secret filter rejects them (`invalid_event`).

| Event type | Statuses | Emitted by |
|---|---|---|
| `stage_started` | started | the agent controller and each stage; the simulator engine |
| `stage_completed` | completed | the same |
| `stage_failed` | failed | a stage, or the controller or engine, that failed |
| `stage_blocked` | blocked | agent stages not run because an earlier stage failed |
| `stage_interrupted` | interrupted, uncertain | content stages whose outcome is not known |
| `order_decision` | accepted, rejected, pending_at_end_of_data | the simulator, with the policy's reason codes |
| `simulated_fill` | filled | the simulator, once per fill |

## Display states and transitions

States: `idle`, `working`, `blocked`, `completed`, `failed`, `unknown`.

| Event | Allowed from | To |
|---|---|---|
| `stage_started` | idle, failed, unknown (a resumed stage) | working |
| `stage_completed` | working | completed |
| `stage_failed` | working | failed |
| `stage_blocked` | idle | blocked |
| `stage_interrupted` | working | unknown |
| `order_decision`, `simulated_fill` | working | working |

`completed` and `blocked` are terminal. Anything else is `invalid_event_transition`:
- the recorder refuses it and stops;
- a stored timeline containing one is reported `partial`.

**What a viewer shows:**
- `working` is shown **only** for a recorded timeline whose writer process still holds
  its lock (`live: true`).
- An old, finished, interrupted or reconstructed timeline never shows `working`. A
  last-known `working` becomes `unknown` (`not_live_last_known_working`).
- In a partial timeline, every state except `completed` and `blocked` is shown as
  `unknown` (`timeline_partial`).

## Completeness

| Value | Meaning |
|---|---|
| `complete` | a close marker exists, events 1..N are all present and valid, and there are no issues |
| `partial` | events are missing or invalid (`missing_events`, `corrupt_event`, `event_count_mismatch`, `interrupted_write_debris`, `invalid_transition`), or recording failed (`persistence_failed`, `event_limit_reached`), or a content trace may have been truncated |
| `open` | no end recorded yet: a recorded timeline whose writer is alive, or a content production that has not finished (its liveness is not checked) |
| `interrupted` | a recorded timeline without a close marker whose writer is gone |

`outcome` is the close marker's result: `completed`, `failed`, `aborted`,
`persistence_failed`, `event_limit_reached`, or null if there is none.

## What is recorded

**Research agents (`agent-run --record-events`):**
- The controller: `stage_started`, then `stage_completed` with the verdict, or
  `stage_failed`. It also fails if evidence can't be built.
- Each of the four stages:
  - `stage_started`, then `stage_completed` with its conclusion, or `stage_failed` with
    its fixed code;
  - stages skipped after a failure: `stage_blocked` (`earlier_stage_failed`).

**Simulator (`sim-run --record-events`):**
- The engine: `stage_started` (with `kill_switch_engaged` if it was engaged), then
  `stage_completed` or `stage_failed`.
- One `order_decision` for:
  - each accepted or rejected entry or exit order, at its decision;
  - each rejection at fill time;
  - each order still pending when the data ended.
- One `simulated_fill` per fill, carrying the fill bar's start time.

**Content:** content production is **not** instrumented in this step. The read-only
adapter rebuilds its timeline from the saved state's stage trace:
- `started`, `completed` and `failed` map to the matching events;
- `uncertain` and `interrupted` map to `stage_interrupted`.

A trace with 100 entries is marked `trace_may_be_truncated`, because the pipeline keeps only
its newest 100. A stage saved as running is shown `unknown`, not `working`. The adapter
reads `state.json` directly: it takes no lock and creates no files.

**Reconstruction order:**
- Research stages follow their fixed order.
- Simulation events are sorted by simulated time; at equal times, decisions come before
  fill-time outcomes, then end-of-data outcomes, then the record order.
- With the default replay step this matches the recorded order exactly (a test checks
  it). With coarse `--step-seconds` it can differ from the original emission order.

## Storage, ordering, duplicates, concurrency

Recorded timelines live in ignored `runtime/events/<department>/timelines/<tl-id>/`:
`manifest.json`, `events/000001.json`…, `closed.json` and `writer.lock`. Content and
trading never share a folder.

- **Atomic and never overwritten:** every file is written as temp file → fsync →
  exclusive link. A second file for the same sequence is `event_duplicate`.
- **Duplicates:** the recorder also refuses a repeated order decision (same order and
  status) or a repeated fill.
- **Ordering:** the sequence number is the order of emission inside one process.
- **Concurrency:** timeline IDs are random. The writer holds an OS lock for the timeline's
  lifetime, and opening an existing timeline fails (`event_timeline_exists`). Different
  timelines never share files, so concurrent commands don't interfere.
- **Interrupted writes:** a crash leaves at most a `*.tmp` file, which readers ignore and
  report.

**Retention:**
- at most `max_events_per_timeline` (5,000) events per timeline (`event_limit_reached`);
- at most `max_timelines_per_department` (200) timelines. Opening a new timeline deletes
  the oldest *closed* timelines first. Open, interrupted or unreadable timelines are never
  deleted; if nothing can be deleted, recording refuses to start (`event_storage_full`).

Both limits are in `config/events.json`.

## When recording fails

If `--record-events` is on and an event can't be saved or would break the rules:
- recording stops immediately, with no retry;
- the timeline is marked `persistence_failed`, `event_limit_reached` or `aborted` if the
  marker can still be written; if not, the timeline reads `interrupted`;
- the command exits 1 with that error code, **and the run is not saved.**

The output's `events` block reports `completeness: partial` and the failure. A timeline
with missing events is never reported as `complete`. If the command itself fails for
another reason, the timeline is closed as `failed` (when a component recorded a failure) or
`aborted`.

## Boundaries

- `vicekrack/events/contract.py`, `sink.py` and `store.py` import neither content nor
  trading code. A test enforces this.
- Department adapters live with their department: `vicekrack/trading/timeline.py` for
  trading and `vicekrack/production_timeline.py` for content.
- `vicekrack/events/cli.py` composes them lazily.
- Content modules still never import trading.

## Limitations

- Only `agent-run` and `sim-run` record live events. Content productions, the paper
  journal and the other commands are covered only by reconstruction, or not at all.
- **Liveness** is the writer's OS lock, so it only applies on the same machine.
  Reconstructed content timelines are never checked for liveness.
- **Reconstructed trading timelines** have no wall-clock times, and with coarse replay
  steps their order may differ from the original emission order.
- **Content traces** keep only the newest 100 entries, so long histories show as
  `partial`.
- Replay prints JSON lines in a terminal. There is no visual dashboard.
