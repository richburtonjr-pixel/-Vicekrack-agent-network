# Vicekrack Agent Network

A small, provider-neutral agent network. The orchestrator validates and routes tasks
to a researcher and returns structured results. Step 4 supports OpenAI Responses and Anthropic Claude Messages
alongside the deterministic local mock. The default configuration remains offline.

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

`jsonschema` and the official `openai` and `anthropic` SDKs are the direct dependencies; pip installs
their dependencies. Tests use unittest and the SDK's HTTP transport dependency. Setup
needs package-index access. The mock demo and automated tests make no AI API calls.
No credentials are needed for either. Never commit real credentials.

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

## Run with OpenAI (explicit opt-in)

Use `config/agents.openai.json` through `--registry`. It sets the researcher's
`execution.adapter` to `openai` and `execution.model` to `gpt-4.1-mini`. Change that
model in the configuration if needed; it must support Responses and Structured Outputs
and be available to your API account. The model name is never selected by task content.
The [model documentation](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
lists these capabilities. This is an API integration, not ChatGPT app automation.

Set `OPENAI_API_KEY` in the process environment. `.env.example` documents the variable
names but the application does **not** automatically load `.env` files. An optional
`OPENAI_TIMEOUT_SECONDS` sets the SDK network-operation timeout (default 30 seconds;
greater than zero, at most 300). This is not a total wall-clock deadline.

Windows PowerShell (prompt avoids putting the key in shell history):

```powershell
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'OpenAI API key' -AsSecureString)).Password
$env:OPENAI_TIMEOUT_SECONDS = '30'
.\.venv\Scripts\python.exe -m vicekrack examples/research-task.json --registry config/agents.openai.json
Remove-Item Env:OPENAI_API_KEY
```

macOS / Linux, Bash:

```bash
read -r -s -p 'OpenAI API key: ' OPENAI_API_KEY
export OPENAI_API_KEY
export OPENAI_TIMEOUT_SECONDS=30
.venv/bin/python -m vicekrack examples/research-task.json --registry config/agents.openai.json
unset OPENAI_API_KEY
```

This explicit command sends the task instructions and supplied notes to OpenAI and
uses API credits. The adapter reads only the environment key, uses the official endpoint,
disables automatic retries, sets a 1200-token output limit, and sends `store=false`.
It does not enable browsing, tools, or conversation history. AI output can still be
incorrect; research remains limited to supplied notes.

The adapter requests a strict JSON summary using
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
validates the returned summary locally, then adds provider/model metadata. The unchanged
task schema validates the final child and parent envelopes. Successful responses appear
at `result.data.delegated_task.result`, including `data.provider: "openai"`.

## Offline provider tests and failures

Run the same unittest command from Setup. It includes the original orchestrator tests
and provider tests that exercise both actual SDKs through in-memory mock HTTP
transport. They use a fake test credential, block socket connections in provider tests,
and never require an API key or spend credits. A live request is not part of validation.

| Error code | Meaning |
| --- | --- |
| `missing_credentials` | `OPENAI_API_KEY` is missing or blank |
| `missing_model` | OpenAI selected without an execution model |
| `invalid_provider_configuration` | Invalid timeout setting |
| `adapter_unavailable` | Unknown or unbound provider; no silent fallback |
| `provider_timeout` / `provider_connection_error` | Network timeout or connection failure |
| `provider_authentication_error` / `provider_permission_error` | API rejected credentials or access |
| `provider_rate_limit` | Rate/quota limit; no automatic retry |
| `provider_api_error` | Other API failure, including invalid/inaccessible model |
| `provider_refusal` / `incomplete_provider_response` | Refused or unfinished generation |
| `invalid_provider_response` | Missing, malformed, or schema-invalid output |
| `provider_error` | Unexpected provider failure |

Accepted tasks receive a schema-valid failed outcome, and the CLI exits 1. Raw API
error bodies, credentials, and exception text are not included. Provider selection is
entirely configuration-based; adding another provider requires a protocol implementation
and registration in `default_providers()`, with no changes to routing or the researcher.

## Select OpenAI or Claude with the same task

| Registry | Adapter | Configured model |
| --- | --- | --- |
| `config/agents.json` (default) | `mock` | None; no API access |
| `config/agents.openai.json` | `openai` | `gpt-4.1-mini` |
| `config/agents.anthropic.json` | `anthropic` | `claude-sonnet-4-6` |

The agent's `execution.adapter` and `execution.model` control provider selection.
The same `examples/research-task.json` works for all three configurations. No task,
researcher, or orchestrator changes are needed. Use a model available to your account
with the provider's structured JSON support. Unknown adapters return `adapter_unavailable`;
there is no silent fallback or automatic retry.

After running Setup, set only the credential for the provider you intend to call.
`.env.example` contains exactly two blank key entries. It is a reference, not a file
loaded by the application. Never enter keys in task JSON or configuration files.

PowerShell, Claude:

```powershell
$env:ANTHROPIC_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Anthropic API key' -AsSecureString)).Password
.\.venv\Scripts\python.exe -m vicekrack examples/research-task.json --registry config/agents.anthropic.json
Remove-Item Env:ANTHROPIC_API_KEY
```

Bash, Claude:

```bash
read -r -s -p 'Anthropic API key: ' ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY
.venv/bin/python -m vicekrack examples/research-task.json --registry config/agents.anthropic.json
unset ANTHROPIC_API_KEY
```

The OpenAI commands above use the identical task with the OpenAI registry instead.
Explicit live commands send instructions and notes to the selected provider and consume
API credits. Neither credentials nor raw provider errors are printed by the application.
The Claude adapter uses the Messages API with
[structured JSON output](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
a 1200-token output cap, no tools, and zero retries. Only completed text JSON summaries
are accepted. Refusals, truncation, malformed blocks, and invalid JSON produce clear errors.
Both providers return `summary` and `data` containing provider/model metadata.

Optional `ANTHROPIC_TIMEOUT_SECONDS` and `OPENAI_TIMEOUT_SECONDS` environment variables
set each SDK's network-operation timeout (default 30, greater than 0 and at most 300).
They are not total wall-clock deadlines and are deliberately omitted from `.env.example`.
The error codes in the preceding table apply to both providers; credential/model messages
identify the relevant provider. Claude overload responses map to `provider_api_error`.

Offline validation (no API keys or credits):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q vicekrack tests
```

On macOS/Linux replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`.
Provider tests use the actual SDKs with in-memory HTTP transports and socket blocking.
The selection test runs the same task against both registry files and checks that each
request reaches the corresponding mocked endpoint with identical instructions and notes.
Credential tests use synthetic values or empty environments, never local credentials.
Live API access and generation quality are not asserted by these mocked tests.

## Files

```text
agents/orchestrator.md          Coordination role and implementation scope
agents/researcher.md            Research role and local limitations
config/agents.json             Default mock registry (unchanged)
config/agents.openai.json       Explicit OpenAI provider/model registry
config/agents.anthropic.json    Explicit Claude provider/model registry
docs/architecture.md           Contracts, routing, lifecycle, and extension points
schemas/task.schema.json       Unchanged Step 1 JSON Schema (Draft 2020-12)
examples/research-task.json     Successful orchestrator-to-researcher demo
examples/unsupported-task.json  Unsupported capability demo
vicekrack/__init__.py           Public Orchestrator import
vicekrack/__main__.py           JSON command-line interface
vicekrack/errors.py             Structured error type
vicekrack/orchestrator.py       Registry loading, validation, routing, lifecycle
vicekrack/researcher.py         Research input requirements and provider invocation
vicekrack/providers.py          Protocol, mock, and adapter registration
vicekrack/openai_provider.py    Responses adapter and safe error normalization
vicekrack/anthropic_provider.py Claude Messages adapter and safe error normalization
tests/test_orchestrator.py      Existing unit and CLI integration tests
tests/test_openai_provider.py   Offline OpenAI SDK and error tests
tests/test_anthropic_provider.py Offline Claude SDK and error tests
tests/test_provider_selection.py Same-task provider switching and CLI credential tests
requirements.txt               JSON Schema validator and official provider SDKs
.env.example                   Only blank OpenAI and Anthropic key entries
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
without replacing routing or the task envelope. Mock, OpenAI, and Anthropic adapters are shipped.

Task ID deduplication is in memory for one `Orchestrator` instance. Each CLI invocation
starts fresh; this is not a persistent queue or exactly-once processing service. Execution
is sequential and performs no automatic retries, parallel work, or recursive delegation.
Step 5, autonomous loops, additional agent conversations, tools, and databases are not implemented. The shared task schema, researcher interface,
capability routing, and lifecycle from Steps 1 and 2 are preserved.
