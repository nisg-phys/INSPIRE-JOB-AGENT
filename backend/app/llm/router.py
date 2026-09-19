"""Routes between LLMProviders with fallback and basic circuit-breaking.

Tries providers in order; on failure, falls through to the next. A
provider that just failed is skipped (not retried) for a cooldown
window, so a consistently-failing provider isn't hit on every request.

LLMRouter itself satisfies the LLMProvider interface, so it's a drop-in
replacement anywhere a single provider was used.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from app.llm.base import LLMProvider, LLMProviderError

DEFAULT_COOLDOWN_SECONDS = 60.0


class AllProvidersFailedError(LLMProviderError):
    """Raised when every provider in the chain failed or was cooling down."""


@dataclass
class _Entry:
    name: str
    provider: LLMProvider
    cooldown_until: float = field(default=0.0)


class LLMRouter:
    def __init__(
        self,
        providers: list[tuple[str, LLMProvider]],
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("LLMRouter needs at least one provider")
        self._entries = [_Entry(name=name, provider=provider) for name, provider in providers]
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock

    def complete_json(self, system: str, user: str) -> str:
        errors: list[str] = []
        now = self._clock()

        for entry in self._entries:
            if entry.cooldown_until > now:
                errors.append(f"{entry.name}: skipped (cooling down)")
                continue
            try:
                return entry.provider.complete_json(system, user)
            except LLMProviderError as exc:
                entry.cooldown_until = self._clock() + self._cooldown_seconds
                errors.append(f"{entry.name}: {exc}")

        raise AllProvidersFailedError("All LLM providers unavailable: " + "; ".join(errors))
