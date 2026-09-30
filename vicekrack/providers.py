"""Provider-neutral interface, mock implementation, and adapter registration."""

from typing import Protocol

from .errors import NetworkError


def default_providers():
    """Register adapters without creating clients or accessing credentials."""
    from .openai_provider import OpenAIResearchProvider
    from .anthropic_provider import AnthropicResearchProvider

    return {"mock": MockResearchProvider(), "openai": OpenAIResearchProvider(),
            "anthropic": AnthropicResearchProvider()}


class ResearchProvider(Protocol):
    def research(self, *, instructions: str, notes: list[str], model: str | None) -> dict:
        """Return a result containing summary and optional data, not a task envelope."""
        ...


class MockResearchProvider:
    """Deterministic extractive summary; not general-purpose AI research."""

    def research(self, *, instructions: str, notes: list[str], model: str | None) -> dict:
        if model is not None:
            raise NetworkError("unsupported_model", "The mock provider requires a null model.")
        if instructions.startswith(("Role: analyst.", "Role: reviewer.")):
            role = "analyst" if instructions.startswith("Role: analyst.") else "reviewer"
            return {"summary": f"Local mock {role}: received the structured prior work. "
                               "This deterministic fixture does not assess truth or approve claims; "
                               "evidence quality and unresolved gaps require real analysis.",
                    "data": {"provider": "mock", "role": role}}
        findings = [
            {"text": note.strip(), "source": f"context.design_notes[{index}]"}
            for index, note in enumerate(notes)
        ]
        return {
            "summary": "Local mock research (supplied notes only): "
            + " ".join(finding["text"] for finding in findings),
            "data": {
                "provider": "mock",
                "findings": findings,
                "limitations": "Extractive summary only; instructions are not interpreted by an AI model.",
            },
        }
