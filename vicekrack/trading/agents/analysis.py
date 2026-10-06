"""Interface for a future, optional AI analysis layer (Step 28). Nothing here calls a provider.

A future layer may read a completed stage's evidence slice and deterministic output and
return short advisory commentary. Contract for any implementation:

- `name` identifies the layer; the only layer that exists today is "none".
- `commentary(role, evidence_slice, deterministic_output) -> str | None`.
- Commentary is advisory only. The controller never lets it change a stage status,
  conclusion, findings, reason codes, the final verdict, `research_only` or
  `authorization_possible`.
- A real implementation must receive its credentials from environment variables, require
  explicit consent before paid calls, and must never put prompts, credentials or
  environment data into run records.

Today the controller accepts only NoAnalysisLayer; any other layer is refused with
`analysis_layer_unavailable`, so enabling one requires a reviewed future step.
"""


class AnalysisLayer:
    name = "abstract"

    def commentary(self, role, evidence_slice, deterministic_output):  # pragma: no cover - interface
        raise NotImplementedError


class NoAnalysisLayer(AnalysisLayer):
    """The default: no commentary, no provider, no network."""
    name = "none"

    def commentary(self, role, evidence_slice, deterministic_output):
        return None
