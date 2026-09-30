"""Research role, independent of provider implementation."""

from .errors import NetworkError
from .providers import ResearchProvider


def run_research(task: dict, provider: ResearchProvider, model: str | None) -> dict:
    notes = task.get("context", {}).get("design_notes")
    if not isinstance(notes, list) or not notes or not all(
        isinstance(note, str) and note.strip() for note in notes
    ):
        raise NetworkError(
            "missing_research_context",
            "Research requires context.design_notes as a nonempty list of nonblank strings.",
        )
    return provider.research(instructions=task["instructions"], notes=notes, model=model)
