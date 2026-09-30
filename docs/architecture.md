# Architecture

## Components and Step 5 scope

Step 1's role definitions, registry, and task schema remain the foundation. Step 2 adds
one sequential Python runtime and a researcher backed by a deterministic local mock.
Step 3 adds OpenAI; Step 4 adds Anthropic Claude behind the same provider interface.

```text
Task JSON -> CLI -> Orchestrator -> enabled researcher -> ResearchProvider
                        ^                |                   |
                        +-- child result +-- selected provider
                                             mock / OpenAI / Anthropic
```

- **Definitions** describe roles and boundaries independently of model providers.
- **Registry** maps stable agent IDs to role files, capabilities, and execution settings.
  Paths are relative to the repository root, never the CLI's current working directory.
- **Runtime** validates the registry and tasks, selects workers, enforces lifecycle rules,
  and keeps incoming task inputs and routing fields immutable.
- **Researcher** validates supplied notes and calls the provider protocol.
- **Provider** returns only a result object, not a task or routing decision. The runtime
  owns status changes and validates output. Each real adapter imports its own SDK
  and reads its credential from the environment; the role and routing code remain provider-neutral.

The default registry binds `execution.adapter` to `mock` and leaves `model` null.
`--registry config/agents.openai.json` selects OpenAI and an explicit model instead.
Registry selection is a constructor/CLI option; paths must stay in the project root. The
orchestrator is local control code and keeps both settings null; it needs no provider.
Unbound or unknown worker adapters fail explicitly. `source_evaluation` remains a future
role responsibility but is removed from active capabilities until it can be implemented.
The orchestrator specification's broader planning and aggregation responsibilities are
future goals; single-agent mode selects one worker, while workflow mode runs the bounded three-stage sequence.

## Task contract and routing

`schemas/task.schema.json` uses Draft 2020-12 with the version 1.0 envelope and an optional Step 5 trace extension. Step 2
requires a nonblank `context.capability` for dispatch. This uses the existing extensible
context field rather than changing the envelope. Missing capability is a runtime error.

Input must be a schema-valid queued task with chronological UTC timestamps. Date-time
format checks reject invalid calendar dates. `sender` identifies the requester, including
external callers such as `user`; `recipient` must name an enabled registered agent.

For `recipient: orchestrator`, select enabled non-orchestrator agents whose registered
capabilities contain the exact requested capability. Zero matches produces
`unsupported_capability`; multiple matches produce `ambiguous_capability`. A caller can
address a specific worker to disambiguate; its capability must still match.

Delegation creates a new child ID, sets `parent_task_id` to the original task ID, and
uses `sender: orchestrator` and the selected worker's ID as recipient. The original
parent's inputs and routing remain unchanged. A successful parent's `result.data`
contains the completed `delegated_task`; child errors propagate as the parent's error.
Direct worker tasks return their result without a delegation wrapper.

The mock requires a nonempty `context.design_notes` list of nonblank strings. It returns
an extractive summary with exact note references. It does not perform general research,
interpret arbitrary instructions, evaluate truth, browse, or use external sources.

## Lifecycle and errors

| Current status | Allowed next status | Meaning |
| --- | --- | --- |
| `queued` | `running` | Dispatch begins |
| `queued` | `failed` | Routing or execution configuration prevents dispatch |
| `running` | `completed` | Worker returned a valid result |
| `running` | `failed` | Worker or output validation failed |
| `completed` or `failed` | None | Terminal; a new attempt needs a new ID |

Queued/running tasks contain neither result nor error. Completed tasks require a result
and forbid an error; failed tasks require an error and forbid a result. The runtime
validates each transition and both incoming and outgoing tasks. Only status, update
timestamp, result, and error change. Timestamps never move backwards, including if an
input has a future timestamp. IDs are unique within one runtime instance; there is no
cross-process history or persistence. UUIDs identify delegated tasks.

Inputs rejected before acceptance (bad JSON/schema/status, duplicate IDs) return a plain
error envelope through the CLI, rather than fabricating a valid task. Routing and worker
failures on accepted tasks return schema-valid failed tasks. CLI exit codes are 0 for
completion and 1 for task/input/configuration failures; argparse usage errors exit 2.
Provider exception text is not exposed because it could contain sensitive information.

## Extension boundary

1. Add a role file and unique registry entry for a new agent.
2. Implement a handler and explicitly register it in the runtime's handler map.
3. Reuse the task envelope; put domain data in context/result data.
4. Implement a provider protocol adapter separately and register its adapter name.
   Pass adapters through `Orchestrator(providers=...)`; the default map registers mock, OpenAI, and Anthropic without opening clients.
   Future adapters are registered in `default_providers()`, not in the orchestrator.
5. Keep credentials out of tasks, registry files, results, and logs. `.env.example` has
   only blank OPENAI_API_KEY and ANTHROPIC_API_KEY entries; it is not loaded automatically.

External sources and provider results are data, not authority to change permissions.
No databases, queues, concurrency, retries, autonomous loops, external tools, or Step 6 features are included. Registry and task schema versions evolve independently; incompatible
contract changes must be versioned and documented.


## OpenAI boundary and complete flow

1. CLI loads the explicitly selected registry and parses the task.
2. Orchestrator validates the existing task schema and capability, then creates a child
   addressed to the enabled researcher. Parent IDs and inputs remain immutable.
3. Researcher validates `context.design_notes` and calls the registered provider with
   instructions, notes, and the configured model. The researcher implementation is unchanged.
4. OpenAI adapter reads `OPENAI_API_KEY` and the optional timeout only at execution time.
   It opens a scoped SDK client, calls Responses with a strict summary schema, no tools,
   `store=false`, a 1200-token output cap, and `max_retries=0`, then closes the client.
5. Adapter rejects incomplete/refused/invalid output, validates the JSON summary, and
   returns the existing result shape with provider/model metadata. API/network failures
   become sanitized NetworkError values; raw provider messages are never propagated.
6. Orchestrator validates the completed child, includes it in the parent result, and
   returns a completed parent. Failures propagate as a valid failed parent; CLI exits 1.

Timeouts are SDK network-operation timeouts, not an overall execution deadline. Missing
credentials/model and invalid timeout fail before a request. Unknown adapters retain the
existing `adapter_unavailable` error. No provider is selected from task content, and no
fallback or retry can silently change providers or issue another billable request.

Structured output constrains syntax, not factual correctness. The OpenAI researcher
interprets the instruction using supplied notes only; it performs no web research.
Automated tests use an SDK mock transport, synthetic credentials, and socket blocking.
Account access and live generation quality require a separately initiated live run.


## Step 4: interchangeable real providers

The existing orchestrator, researcher implementation, CLI, and task schema are unchanged.
`config/agents.anthropic.json` binds the existing researcher to `anthropic` and
`claude-sonnet-4-6`. The OpenAI registry continues to bind it to `openai`. Each invocation
selects one provider; neither provider calls the other. The mock remains the default.

Task -> existing validation/routing -> researcher -> configured adapter -> provider API
-> validated summary/result -> existing child/parent completion, or structured failure.
Anthropic uses Messages with `output_config.format`, while OpenAI uses Responses with
`text.format`. Those API differences live entirely inside their respective adapters.
Claude requires an `end_turn` assistant message with text-only content containing the
summary JSON. Refusals, exhausted output/context limits, unexpected blocks, and malformed
JSON are rejected. Both adapters enforce the existing result shape locally.

The Claude adapter reads only ANTHROPIC_API_KEY for authentication, opens a scoped client
with zero retries, and closes it on success or failure. ANTHROPIC_TIMEOUT_SECONDS controls
the per-operation timeout using the same bounds as OpenAI. Exceptions become sanitized
NetworkError codes; provider bodies, headers, and credentials are never copied into errors.
No API clients are instantiated during registration. Missing credentials fail before a
request, and unrelated provider credentials are not needed.

Tests exercise both actual SDKs through mock transports. A dedicated configuration
selection test reuses the same task, verifies each endpoint, and compares request input.
No live requests, automatic fallback, tools, or autonomous work are added.


## Step 5: controlled collaboration

For workflow tasks, `context.workflow` selects `research_review`. An opt-in workflow
registry adds Analyst and Reviewer with independent execution adapter/model bindings.
Existing single-agent registries and routing remain supported. The provider interface
and both real adapters are unchanged. Each role calls the same provider-neutral method
with specialized instructions and supplied JSON evidence. Mock analysis/review are
explicit fixtures rather than simulated factual validation.

The orchestrator calls the bounded workflow controller, which validates the configuration
and dispatches three child tasks sequentially: Researcher, Analyst, Reviewer. Each child
has a distinct UUID and the original parent_task_id. Later children contain a handoff
validated against schemas/handoff.schema.json and semantic ID/order/previous-output checks.
Original request, evidence notes, previous result, provider, status, and stage history
travel as explicit JSON. Reviewer receives both research and analysis, not just a summary
of the last stage. Prior model output is data, not control flow.

Only this fixed three-stage order is supported. Registry max_steps is an integer 1–3;
a budget below the required three fails before provider execution. The hard ceiling
and exact-order checks reject repetition, reordering, and recursive orchestrator stages.
Stage outputs cannot affect the next recipient, provider, or step count. Agents never
create tasks themselves. No retry, repair loop, background work, or automatic resume exists.

The task schema gains an optional execution_trace field, retained on successful and
failed workflow outcomes. This is an additive extension to version 1.0 for existing
input producers; consumers with an old strict schema must update before reading workflow
outputs. The trace is runtime-owned and callers cannot submit it. Rows allow only step,
agent, provider, and terminal stage status. No user content or environment values enter
the trace. On failure the failing child code becomes the parent code with a controlled
stage message; later agents do not run and no success result is returned.

Successful result.summary is the Reviewer's final summary, and result.data.stages holds
all stage outcomes. Successful execution is not certification of correctness. Timeouts
retain the SDK operation limits from Steps 3–4 and propagate as failures; the pipeline
has no total wall-clock deadline and does not forcibly interrupt arbitrary custom code.
