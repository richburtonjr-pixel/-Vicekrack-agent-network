"""Trading errors reuse the shared core error type so CLIs print only fixed codes."""

from ..errors import NetworkError


class TradingError(NetworkError):
    """A fixed, sanitized trading error code. Messages never contain raw input or exceptions."""
