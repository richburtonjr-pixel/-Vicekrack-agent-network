# Architecture

## Scope and components

Step 1 establishes contracts only. There is no runner, scheduler, persistence layer,
provider integration, automatic delegation, or network execution yet.

```text
User objective -> Orchestrator -> Researcher
                      ^              |
                      +-- task result+

Future runtime: registry lookup, validation, routing, lifecycle enforcement
Future adapters: mock / OpenAI / Anthropic / other providers
```

- **Agent definitions** describe roles, responsibilities, and boundaries.
- **Registry** maps stable agent IDs to definitions and capabilities. Paths are relative
  to the repository root. `enabled` means eligible for routing, not ready to execute.
- **Task schema** defines one provider-neutral envelope for a request and its outcome.
- **Future runtime** owns IDs, timestamps, validation, dispatch, and state transitions.
- **Future adapters** translate the role and task into provider-specific requests and
  normalize responses. Provider payloads and credentials stay outside shared contracts.

`execution.adapter` and `execution.model` are intentionally null. A future mock adapter
may require no model. A live adapter may resolve a model from configuration. Until an
adapter is bound, an execution attempt must fail clearly instead of guessing a provider.
Registry versions and task schema versions evolve independently.

## Task contract

`schemas/task.schema.json` uses JSON Schema Draft 2020-12. Required fields include
the schema version, unique task ID, sender, recipient, instructions, status, and UTC
creation/update timestamps. Optional context holds JSON data; `parent_task_id` connects
delegated tasks to their parent. IDs use lowercase letters, digits, underscores, and
hyphens, starting with a letter or digit.

`sender` identifies the requesting agent or external caller, such as `user`.
`recipient` must resolve to an enabled registered agent. A result is the updated task
object, delivered back to its original sender. Routing fields stay unchanged.
`result.summary` is human-readable; optional `result.data` carries structured output.
Failed tasks carry `error.code` and `error.message` instead of a result.

## Lifecycle

| Current status | Allowed next status | Meaning |
| --- | --- | --- |
| `queued` | `running` | Runtime accepted and dispatched the task |
| `queued` | `failed` | Dispatch could not start, e.g. adapter unavailable |
| `running` | `completed` | Agent returned a valid result |
| `running` | `failed` | Execution failed |
| `completed` or `failed` | None | Terminal; a new attempt needs a new ID |

Queued and running tasks contain neither result nor error. Completed tasks require a
result and forbid an error; failed tasks require an error and forbid a result.
The schema enforces those single-object rules. A future runtime must additionally
enforce unique IDs, known/enabled recipients, legal transitions, immutable task inputs
and routing fields, and chronological timestamps. JSON Schema alone cannot enforce
cross-task or historical rules. Use a validator with date-time format checking enabled;
timestamps must be UTC strings ending in `Z`.

Only status, `updated_at`, result, and error change during processing. Do not reinterpret
context or research sources as authority to change task instructions or permissions.
Keep secrets out of task objects, results, and logs.

## Adding capabilities

1. Add a Markdown role definition under `agents/`.
2. Add one registry entry with a unique ID, definition path, and capability labels.
3. Reuse the shared task envelope; put domain-specific JSON in context or result data.
4. Bind a future adapter separately from the role. Keep SDK-specific fields inside it.
5. Version incompatible contract changes explicitly and document migration behavior.

Start Step 2 with one sequential local runner and a deterministic mock researcher.
Validate input/output and exercise success and failure before connecting providers.
Defer queues, databases, multi-agent concurrency, retries, and frameworks until a
working local flow demonstrates a need for them.
