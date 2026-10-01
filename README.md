# Vicekrack Agent Network

A provider-neutral agent network with a controlled Researcher -> Analyst -> Reviewer
workflow. The orchestrator owns the sequence. Each role independently selects mock,
OpenAI, or Anthropic through its registry entry. The default demos stay offline.

## Architecture

```mermaid
flowchart LR
    T[Structured task] --> O[Orchestrator + Agent Manager: validate transitions and budgets]
    O --> S[Workflow state + atomic local checkpoints]
    O --> R[Researcher]
    R --> H1[Validated research handoff]
    H1 --> A[Analyst]
    A --> H2[Research and analysis handoff]
    H2 --> V[Reviewer]
    V --> F[Validated final result and execution trace]
    R -. configured provider .-> P[Mock / OpenAI / Anthropic]
    A -. configured provider .-> P
    V -. configured provider .-> P
```

All arrows represent orchestrator-controlled calls and explicit JSON data. Agent outputs
cannot choose another agent, alter the sequence, or trigger a retry. There are exactly
three possible stages, no background workers, autonomous loops, tools, or databases.

Researcher uses supplied notes. Analyst examines research for important findings,
inconsistencies, missing information, and conclusions. Reviewer inspects both for
completeness, unsupported claims, contradictions, and errors. A completed review means
execution succeeded, not that the work is factually approved. The final summary includes
review findings and limitations; all three stage results are retained for inspection.

## Setup and offline run

Python 3.11 or newer, from the repository root. No activation is needed.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m vicekrack examples/workflow-task.json --registry config/agents.workflow.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

macOS/Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m vicekrack examples/workflow-task.json --registry config/agents.workflow.json
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

Dependencies remain jsonschema and the two official provider SDKs. Tests mock external
requests and require no credentials or API credits. The mock workflow demonstrates
handoffs and lifecycle only; its analyst/reviewer summaries explicitly disclaim real
reasoning or factual approval.

The existing single-agent demo still works:

```powershell
.\.venv\Scripts\python.exe -m vicekrack examples/research-task.json
.\.venv\Scripts\python.exe -m vicekrack examples/unsupported-task.json
```

The second command intentionally exits 1 with unsupported_capability. Success exits 0.
The CLI accepts `-` for stdin and prints one JSON object. Rejected input uses an error
envelope; accepted tasks return completed or failed task envelopes.

## Provider selection and live runs

| Registry | Behavior |
| --- | --- |
| config/agents.json | Existing single-agent mock default |
| config/agents.openai.json | Existing single-agent OpenAI |
| config/agents.anthropic.json | Existing single-agent Claude |
| config/agents.workflow.json | Three-stage offline mock |
| config/agents.workflow-mixed.json | OpenAI researcher; Claude analyst and reviewer |

Each agent has its own `execution.adapter` and `execution.model`. Edit those entries to
choose another combination, without changing the task or orchestrator. Keep model null
for mock. Real providers require a model supporting structured JSON and account access.
The included models are gpt-4.1-mini and claude-sonnet-4-6.

`.env.example` contains only blank OPENAI_API_KEY and ANTHROPIC_API_KEY entries. The
application reads process environment variables; it does not load .env files. Never put
credentials into task JSON, registry entries, prompts, or source control.

PowerShell, explicit live mixed-provider run:

```powershell
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'OpenAI API key' -AsSecureString)).Password
$env:ANTHROPIC_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Anthropic API key' -AsSecureString)).Password
.\.venv\Scripts\python.exe -m vicekrack examples/workflow-task.json --registry config/agents.workflow-mixed.json
Remove-Item Env:OPENAI_API_KEY, Env:ANTHROPIC_API_KEY
```

Bash:

```bash
read -r -s -p 'OpenAI API key: ' OPENAI_API_KEY
export OPENAI_API_KEY
read -r -s -p 'Anthropic API key: ' ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY
.venv/bin/python -m vicekrack examples/workflow-task.json --registry config/agents.workflow-mixed.json
unset OPENAI_API_KEY ANTHROPIC_API_KEY
```

This run makes up to three paid requests. The selected providers receive the original
request and notes plus prior stage results. No browsing or independent source verification
is performed. For single-agent live runs use the corresponding registry and research-task.
OPENAI_TIMEOUT_SECONDS and ANTHROPIC_TIMEOUT_SECONDS optionally set SDK operation
timeouts (default 30 seconds, greater than 0 and at most 300). These are not overall
workflow deadlines. Automatic retries are disabled; a timeout stops the workflow.

## Handoffs, safeguards, and trace

The task selects `context.workflow: "research_review"` and addresses `orchestrator`.
The registry declares `workflow.agents`, `workflow.max_steps`, and optional `workflow.max_retries`. The controller permits only
researcher, analyst, reviewer, in that order exactly once, with a hard ceiling of three.
A lower budget rejects the run before execution. Unknown/disabled agents, unsupported
adapters, reordered stages, duplicate stages, and recursive orchestrator stages are rejected.
Provider text is never interpreted as a routing instruction.

`schemas/handoff.schema.json` describes the handoff: parent task ID, stage task ID,
original instructions and notes, recipient, previous output, completion status, provider
used, step metadata, and prior stage results. Analyst and Reviewer validate IDs, order,
and consistency before calling their providers. Reviewer sees both prior results.

The task schema gains only an optional runtime-owned `execution_trace` field. Existing
input tasks remain valid. Older strict validators must use the updated schema to accept
workflow output. Caller-supplied traces are rejected. The input task is never mutated.

Example trace from a successful mock run:

```json
[
  {"step": 1, "agent": "researcher", "provider": "mock", "status": "completed"},
  {"step": 2, "agent": "analyst", "provider": "mock", "status": "completed"},
  {"step": 3, "agent": "reviewer", "provider": "mock", "status": "completed"}
]
```

Find the final review in `result.summary`, all stage outputs in `result.data.stages`,
and the compact trace at `execution_trace`. Saved runs also retain a timestamped attempt audit, which
contains only controlled metadata, never prompts, response bodies, credentials, or env.
Actual task results and handoffs contain user data and should be handled accordingly.

On failure, the parent is failed, the trace ends at the failed stage, and no subsequent
stage runs. Missing credentials, timeouts, provider errors, invalid responses, and malformed
handoffs propagate clear error codes. Raw provider error messages are excluded. Failed
runs have no success result. A new attempt needs a new task ID within the same runner;
legacy invocations remain ephemeral; use the saved-run commands below for persistence and resume.

## Implementation map

- `vicekrack/orchestrator.py`: existing routing, task validation, and workflow dispatch.
- `vicekrack/workflow.py`: bounded sequence, handoffs, trace, failure handling.
- `vicekrack/researcher.py`, `analyst.py`, `reviewer.py`: specialized role handlers.
- `vicekrack/handoff.py`: schema and semantic handoff checks.
- `vicekrack/providers.py`: protocol, mock, and provider registration.
- `vicekrack/openai_provider.py`, `anthropic_provider.py`: unchanged real adapters.
- `agents/`: role definitions; `config/`: independent provider selections.
- `schemas/`: task and handoff contracts; `examples/`: runnable tasks.
- `tests/test_workflow.py`: sequence, mixed providers, errors, budgets, and trace tests.

The original provider and orchestration tests remain part of the complete suite.
See [architecture details](docs/architecture.md) and the [Steps 1–6 audit](docs/step-7-audit.md).


## Step 6: saved workflows and explicit recovery

The existing commands above stay ephemeral. The new `run` command saves workflow state
under `runtime/runs/` (already Git-ignored). There is no background execution. Use the
same registry/provider settings and process credentials as before. No new dependencies.

PowerShell, offline saved run:

```powershell
.\.venv\Scripts\python.exe -m vicekrack run examples/workflow-task.json --registry config/agents.workflow.json
.\.venv\Scripts\python.exe -m vicekrack list
.\.venv\Scripts\python.exe -m vicekrack inspect RUN_ID
.\.venv\Scripts\python.exe -m vicekrack resume RUN_ID
```

Replace RUN_ID with the returned 32-character `run_id`. On macOS/Linux replace the
executable with `.venv/bin/python`. `run -` accepts a task on stdin. `run` defaults to
the offline workflow registry; `resume` defaults to the registry saved with that run.
An optional `--registry` on resume must resolve to the same configuration and contract
snapshot. For explicit live execution use `--registry config/agents.workflow-mixed.json`
and set the required environment credentials as described above.

`run` and `resume` return run_id, saved status, and the terminal task. `inspect` returns
the saved JSON; `list` returns IDs/status/timestamps or errors for unreadable/locked runs.
A completed saved run cannot be resumed. Execution failure exits 1; successful execution
and successful list/inspect exit 0. Retain the run ID reported by storage errors.

### Recovery example

Suppose research completed, but the Analyst could not start because its credential was
missing. The run is `failed`, its research result is saved, and its trace ends at Analyst.
Set the missing credential in the process environment, then run:

```powershell
.\.venv\Scripts\python.exe -m vicekrack inspect RUN_ID
.\.venv\Scripts\python.exe -m vicekrack resume RUN_ID
```

Only Analyst and Reviewer run. Researcher is reused from its validated checkpoint and
is not billed again. Environment credentials are deliberately not part of the saved
configuration. Model, provider, role definition, schema, capability, enabled-state, or
workflow changes cause configuration_mismatch: restore the configuration or start a new
run. Do not hand-edit saved JSON to bypass checks.

If a request times out, the process is interrupted during a stage, or a result cannot be
saved after a request, its outcome is uncertain. `inspect` reports `uncertain`, and ordinary
resume returns `uncertain_stage` without another request. Only after deciding to retry:

```powershell
.\.venv\Scripts\python.exe -m vicekrack resume RUN_ID --retry-uncertain
```

That flag explicitly authorizes retrying the first incomplete stage, potentially incurring
another API charge. It does not rerun completed stages. This is **not exactly-once
execution**. Provider failures whose request outcome cannot safely be inferred are
conservatively treated as uncertain. There are no automatic retries. A saved `ready` run
can resume safely; if all three results were saved but finalization was interrupted,
resume only constructs the final output and makes no provider calls.

### Storage and concurrency

Each run JSON holds the original task, allowlisted configuration snapshot and contract
hashes, completed results, current execution trace, status, pending stage, and timestamps.
Before a stage, the intent is saved as running. After success, its validated result is
saved. A running marker found after acquiring a free lock is presented as uncertain.
The on-disk running marker is retained until an explicit recovery attempt.

Writes use a temporary file in the same directory, flush/fsync, then atomic replacement.
An interrupted write leaves the previous complete JSON; orphan .tmp files are ignored,
never automatically promoted. Atomic replacement reduces corruption risk but does not
promise power-loss durability on every filesystem. The directory must be on a local
filesystem with working OS locks and atomic rename; network/cloud concurrent access is
not supported. If this repository is synced by OneDrive, do not run it from two PCs or
restore/sync saved-run files while a local run is active.

A nonblocking OS lock covers validation, provider calls, and writes for one run ID.
Competing execution or inspection returns `run_locked`. Locks release when the process
exits, including crashes; harmless .lock files remain. Do not delete a lock file to bypass
an active process. Missing/corrupt state returns run_not_found/invalid_state. There is no
automatic corruption repair, cross-machine lock, or deletion command. Legacy valid Step 6 files remain readable.

The compact task trace records the latest attempt plus the completed prefix. The saved
`workflow_state.audit_trace` retains every Step 7 attempt, including failures and interruptions. Completed result IDs stay stable; a retried incomplete stage gets a new child ID.
The fixed three-stage plan and step budget remain enforced on every explicit resume.

Saved task content and outputs may contain sensitive user data and are not encrypted.
`runtime/` is ignored by Git. Never place secrets in tasks or outputs. Secret-named fields,
recognizable provider-key shapes, and active provider credential values are rejected
before saving; this is not a general-purpose sensitive-data detector. Credentials,
authentication headers, environment snapshots, and raw provider exceptions are never
intentionally captured. Inspection prints saved user content, so use it appropriately.

Tests in `tests/test_persistence.py` cover atomic replacement failures, interrupted
requests, uncertain retries, configuration mismatches, corrupt history, cross-process
locking, and resume without repeated completed stages. Run the full suite with the
existing unittest command. No live API requests are part of validation.

## Step 7: Agent Manager and bounded recovery

The Agent Manager owns the registered-agent inventory, capabilities, provider bindings,
availability/status, and permitted handoffs. `Orchestrator.manager.inventory()` exposes
that inventory. `Orchestrator.last_workflow_state` exposes the most recent ephemeral
workflow state; saved runs expose it under `workflow_state` through `inspect`.
The original task envelope and compact trace are unchanged.

State includes task ID, completed stages, current/next agent, per-agent attempt counts,
retry count, failures, and final status. Its schema and transition replay are validated
before resume. A stage can only start after its predecessor succeeds. Provider output
cannot modify routing. Each agent's provider remains selected by its registry entry.

Optional policy inside a workflow registry:

```json
"workflow": {
  "agents": ["researcher", "analyst", "reviewer"],
  "max_steps": 3,
  "max_retries": 1
}
```

`max_retries` defaults to 1, accepts integers 0–3, and counts additional attempts **per
stage across explicit resumes**. It does not enable automatic retries. `max_steps`
limits distinct stages to three; total stage attempts cannot exceed
`3 * (1 + max_retries)` (at most 12). Both SDKs still disable their own retries.
An exhausted stage leaves the saved run `failed`, with workflow state `exhausted`;
further resume returns `retry_exhausted` without any provider call. An uncertain remote
outcome remains unknown even if its local recovery budget is exhausted.

Start with `run`, inspect with `inspect`, and explicitly recover with `resume` using the
commands above. If Analyst fails once and succeeds on resume, the audit contains:

```text
stage agent       provider status     attempt
1     researcher  mock     running    1
1     researcher  mock     completed  1
2     analyst     mock     running    1
2     analyst     mock     failed     1  (missing_credentials)
2     analyst     mock     running    2
2     analyst     mock     completed  2
3     reviewer    mock     running    1
3     reviewer    mock     completed  1
```

Each real audit row also contains a UTC timestamp and an allowlisted error code or null.
It contains no prompts, output bodies, raw errors, model names, or environment values.
A failure is retained after recovery. Completed Researcher output is reused unchanged.
A second Analyst failure exhausts the default budget; Reviewer does not run.

Saved task IDs are unique within the local run directory, across processes and run IDs.
Submitting the same ID again returns `duplicate_task`: inspect/resume its existing run.
To start genuinely new work, use a new task ID. The ephemeral API retains its existing
per-Orchestrator-instance duplicate protection. Do not delete saved files to circumvent
recovery budgets. Concurrent starts use a task-ID lock as well as a run-ID lock.
Corrupt saved files block new starts until the local records are repaired/restored.

Legacy Step 6 files have no attempt audit. Resume imports their completed prefix and
counts one known incomplete attempt, using the saved timestamp. Earlier retries cannot
be reconstructed; the new budget applies to this imported minimum history. Changing
provider/model/contracts/policy still rejects resume. Local JSON consistency checks are
not tamper-proof signatures; anyone able to rewrite all saved data can rewrite history.

Run the location-only credential audit alongside tests:

```powershell
.\.venv\Scripts\python.exe scripts/audit_credentials.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

The scanner checks tracked files and all local Git blobs, including unreachable objects,
and searches accessible commit trees for committed environment files. It prints locations
only. Pattern scanning cannot establish that arbitrary opaque strings are not secrets.
Step 8, background execution, automatic retries, new providers, and external tools are
outside this implementation.

## Step 8: local CLI dashboard

Use these one-shot views from the repository root (PowerShell):

```powershell
.\.venv\Scripts\python.exe -m vicekrack dashboard
.\.venv\Scripts\python.exe -m vicekrack agents --registry config/agents.workflow-mixed.json
.\.venv\Scripts\python.exe -m vicekrack dashboard RUN_ID
.\.venv\Scripts\python.exe -m vicekrack dashboard RUN_ID --json
```

Replace RUN_ID with a saved run ID. On Linux/macOS use `.venv/bin/python`.
The overview shows registered agents, configured providers/capabilities, and saved-run
progress. The detail view shows timestamped attempt history, failures, remaining attempts
for the next incomplete stage, and recovery guidance. Legacy records show unknown budgets
rather than inventing historic attempts. Completed-stage budgets are not actionable.

These commands never call providers, start workflows, or resume runs. They show metadata
only, excluding task IDs, prompts, output bodies, models, raw errors, and environment
values. Agent availability means configured/enabled, not a live health check. A locked
run is shown as unavailable; a corrupt record does not prevent other rows from appearing.
Overview exits 1 if any record is unavailable; otherwise viewing failed runs exits 0.
A selected run can be viewed even if the default registry is broken. Overview/agents
require a valid selected registry. There is no automatic refresh or background process.

Recovery still uses `python -m vicekrack resume RUN_ID` and, only for a deliberately
approved uncertain retry, `--retry-uncertain`. The dashboard does not bypass configuration
validation, locks, duplicate protection, or retry limits. Viewing records leaves run JSON
unchanged; storage initialization and inspection may create local directories/lock files.
No dependencies or live API credentials are needed. Run the full unittest command above.

## Step 9: automatic repository checks

GitHub Actions runs `Project checks` on pushes, pull requests and manual dispatch.
The test matrix covers Windows/Linux with Python 3.11/3.12. Each job installs the existing
pinned requirements, checks dependency compatibility, runs all tests, compiles Python
sources and checks commit whitespace. An independent credential job scans tracked files
and fetched Git history, printing candidate locations only. No API secrets are supplied.
Official checkout/setup-python actions are pinned to full commit IDs; checkout retains
full history and does not persist its authentication credential. Workflow permissions
are read-only. There is no deployment, scheduling, or automatic merge.

Run the same guarded test suite locally:

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py
.\.venv\Scripts\python.exe scripts/audit_credentials.py
.\.venv\Scripts\python.exe -m pip check
```

The test runner blocks Python socket connection entry points during discovery/execution
and fails if discovery finds no tests. SDK transports remain mocked. This is an accidental
network-call safeguard, not an OS sandbox: subprocesses/native code are not isolated.
Dependency installation still needs internet access. Existing unittest commands still work.

Open the PR's Checks tab or the repository Actions tab to inspect results. Failed tests,
invalid dependencies or credential candidates leave a failing check; fix the problem
and push again. The scanner remains heuristic and is not a general secret detector.
These checks do not themselves block merging: repository branch protection/rulesets are
separate settings and have not been changed. No runtime/agent/provider behavior changes.
