# Vicekrack Agent Network

A simple, modular foundation for agents that coordinate through a shared task contract.
Agent roles are independent of AI providers, models, SDKs, and transport mechanisms.
Future adapters may connect OpenAI, Anthropic Claude, or other services.

## Step 1: foundation

This repository currently contains definitions and documentation, not an executable
agent system. No installation, API keys, or dependencies are required.

```text
agents/
  orchestrator.md             Coordination responsibilities and boundaries
  researcher.md              Research responsibilities and boundaries
config/
  agents.json                Agent registry and unbound execution settings
docs/
  architecture.md            Components, task lifecycle, and extension guide
examples/
  research-task.json         Example task sent to the researcher
schemas/
  task.schema.json           Shared JSON Schema (Draft 2020-12)
.gitignore
README.md
```

Start with [the architecture](docs/architecture.md), then inspect
[the registry](config/agents.json) and [the task example](examples/research-task.json).
Both agents exchange task objects conforming to [the task schema](schemas/task.schema.json).

## Step 2: local execution without API access

Choose a runtime and implement a small command-line runner that loads the registry,
validates tasks, and routes a task from the orchestrator to a deterministic mock
researcher. Have the mock return a completed task with a result, and demonstrate a
failed task with an error. Validate both incoming and outgoing objects, reject unknown
agents, and enforce the lifecycle described in the architecture.

Step 2 is complete when one local command runs the example end to end without network
access or credentials, with tests for successful routing, invalid input, unknown
agents, and failed execution. Add real provider adapters only after that contract works.

## Extending the network

Add a role definition under `agents/` and register its stable ID and capabilities in
`config/agents.json`. Keep provider-specific execution code in future adapters rather
than embedding SDK details into role definitions or the shared task format.

Never commit credentials. Future integrations should read secrets from environment
variables or a secret manager; registry values must not contain secrets.
