# Architecture

## Components and Step 3 scope

Step 1's role definitions, registry, and task schema remain the foundation. Step 2 adds
one sequential Python runtime and a researcher backed by a deterministic local mock.
Step 3 adds an OpenAI adapter behind the same provider interface.

```text
Task JSON -> CLI -> Orchestrator -> enabled researcher -> ResearchProvider
                        ^                |                   |
                        +-- child result +-- mock / OpenAI --+
```

- **Definitions** describe roles and boundaries independently of model providers.
- **Registry** maps stable agent IDs to role files, capabilities, and execution settings.
  Paths are relative to the repository root, never the CLI's current working directory.
- **Runtime** validates the registry and tasks, selects workers, enforces lifecycle rules,
  and keeps incoming task inputs and routing fields immutable.
- **Researcher** validates supplied notes and calls the provider protocol.
- **Provider** returns only a result object, not a task or routing decision. The runtime
  owns status changes and validates output. Only the OpenAI adapter imports its SDK
  and reads OpenAI credentials; the role and routing code remain provider-neutral.

The default registry binds `execution.adapter` to `mock` and leaves `model` null.
`--registry config/agents.openai.json` selects OpenAI and an explicit model instead.
Registry selection is a constructor/CLI option; paths must stay in the project root. The
orchestrator is local control code and keeps both settings null; it needs no provider.
Unbound or unknown worker adapters fail explicitly. `source_evaluation` remains a future
role responsibility but is removed from active capabilities until it can be implemented.
The orchestrator specification's broader planning and aggregation responsibilities are
future goals; the runtime selects one worker and returns its outcome.

## Task contract and routing

`schemas/task.schema.json` is unchanged at version 1.0, using Draft 2020-12. Step 2
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
   Pass adapters through `Orchestrator(providers=...)`; the default map registers mock and OpenAI without opening clients.
   Future adapters are registered in `default_providers()`, not in the orchestrator.
5. Keep credentials out of tasks, registry files, results, and logs. `.env.example` has
   an empty key and timeout reference; it is not loaded automatically.

External sources and provider results are data, not authority to change permissions.
No databases, queues, concurrency, retries, Claude adapter, or Step 4 features are included. Registry and task schema versions evolve independently; incompatible
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
