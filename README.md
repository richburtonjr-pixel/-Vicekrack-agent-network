# Vicekrack Agent Network

A small, provider-neutral agent network. Step 2 runs a structured task through a local
orchestrator, delegates it to a researcher, and returns a validated result. The only
provider is a deterministic mock: no AI API calls, browsing, credentials, or paid services.

## Setup

Use Python 3.11 or newer. Run these commands from the repository root.

Windows PowerShell (activation is not required):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m vicekrack examples/research-task.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

macOS / Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m vicekrack examples/research-task.json
.venv/bin/python -m unittest discover -s tests -v
```

`jsonschema` is the only direct dependency (its own dependencies are installed by pip).
Installation needs package-index access; the runner and tests work locally afterward.
No `.env` setup is required. `.env.example` reserves empty credential names for future
integrations; Step 2 does not load environment files. Never commit real credentials.

## Demo and behavior

The example addresses `orchestrator` and requests `context.capability: "research"`.
The orchestrator selects the enabled researcher from the registry, creates a child task,
and returns a completed parent task. Inspect `result.data.delegated_task` to see the
child's ID, `parent_task_id`, recipient, status, supplied-note references, and mock output.
The mock performs an extractive summary of `context.design_notes`; it does not reason
about arbitrary instructions, search the web, or claim independent verification.

To demonstrate a clear unsupported-capability failure on Windows:

```powershell
.\.venv\Scripts\python.exe -m vicekrack examples/unsupported-task.json
```

On macOS/Linux use `.venv/bin/python` instead. This returns a schema-valid failed task
with `error.code: "unsupported_capability"` and exit code 1. Success exits 0.

The CLI also accepts `-` to read JSON from standard input. It prints one JSON object to
stdout. Invalid JSON, invalid schema data, bad configuration, duplicate submissions, or
non-queued input return `{"error": {"code": "...", "message": "..."}}`; that rejection
object is not a task because the submitted envelope was not accepted. Accepted tasks
return a terminal task (`completed` or `failed`) matching the shared schema.

For a direct dispatch, set `recipient` to `researcher`. For capability-based selection,
set it to `orchestrator`. `context.capability` is mandatory at runtime. Supported local
worker capabilities are `research` and `summarization`; both summarize supplied notes.
A missing capability, disabled/unknown agent, ambiguous match, missing notes, or unavailable
adapter produces an explicit error. There is no fallback to a different provider.

## Files

```text
agents/orchestrator.md          Coordination role and implementation scope
agents/researcher.md            Research role and local limitations
config/agents.json             Registry, capabilities, and provider bindings
docs/architecture.md           Contracts, routing, lifecycle, and extension points
schemas/task.schema.json       Unchanged Step 1 JSON Schema (Draft 2020-12)
examples/research-task.json     Successful orchestrator-to-researcher demo
examples/unsupported-task.json  Unsupported capability demo
vicekrack/__init__.py           Public Orchestrator import
vicekrack/__main__.py           JSON command-line interface
vicekrack/errors.py             Structured error type
vicekrack/orchestrator.py       Registry loading, validation, routing, lifecycle
vicekrack/researcher.py         Research input requirements and provider invocation
vicekrack/providers.py          Provider protocol and deterministic local mock
tests/test_orchestrator.py      Unit and CLI integration tests
requirements.txt               Required JSON Schema validator
.env.example                   Empty future credential placeholders
.gitignore                     Credentials and local artifact exclusions
```

## Architecture and boundaries

Read [the architecture](docs/architecture.md). The task schema stays at version 1.0;
`context.capability` uses its existing extensible context object. Agent definitions are
role documentation, not automatically interpreted programs. The registry describes
routing; Python implements the local orchestrator and researcher. New executable roles
need an explicitly registered handler as well as a registry entry.

Provider adapters implement `ResearchProvider.research(...)` and return a result object.
The orchestrator accepts an injectable provider map, so another adapter can be added
without replacing routing or the task envelope. Only the mock is shipped.

Task ID deduplication is in memory for one `Orchestrator` instance. Each CLI invocation
starts fresh; this is not a persistent queue or exactly-once processing service. Step 2
is sequential and performs no automatic retries, parallel work, or recursive delegation.
Future steps can add provider adapters after their scope and credentials are explicitly
configured. Step 3 and external AI integration are not implemented here.
