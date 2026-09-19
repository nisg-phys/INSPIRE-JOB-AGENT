"""Common interface any LLM provider adapter implements.

Kept deliberately low-level (raw prompt in, raw text out) rather than
rewrite_query(text) -> JobQueryParams directly, so the prompt and JSON
parsing logic lives once in query_rewriter.py instead of being
duplicated per provider.
"""

from __future__ import annotations

from typing import Protocol


class LLMProviderError(RuntimeError):
    """Raised when a provider's completion call fails, regardless of provider."""


class LLMProvider(Protocol):
    def complete_json(self, system: str, user: str) -> str:
        """Return the model's raw text response to a system+user prompt pair,
        requesting JSON-formatted output where the provider supports it.

        Raises:
            LLMProviderError: If the underlying request fails.
        """
        ...
