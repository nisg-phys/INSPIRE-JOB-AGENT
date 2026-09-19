"""Routes between LLMProviders with fallback and basic circuit-breaking.

Tries providers in order; on failure, falls through to the next. A
provider that just failed is skipped (not retried) for a cooldown
window, so a consistently-failing provider isn't hit on every request.

LLMRouter itself satisfies the LLMProvider interface, so it's a drop-in
replacement anywhere a single provider was used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import opik

from app.llm.base import LLMProvider, LLMProviderError

logger = logging.getLogger("app.llm.router")

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

    @opik.track(name="llm_router.complete_json")
    def complete_json(self, system: str, user: str) -> str:
        errors: list[str] = []
        now = self._clock()

        for entry in self._entries:
            if entry.cooldown_until > now:
                logger.info("llm_provider_skipped", extra={"provider": entry.name, "reason": "cooling_down"})
                errors.append(f"{entry.name}: skipped (cooling down)")
                continue
            try:
                start = time.monotonic()
                result = entry.provider.complete_json(system, user)
                logger.info(
                    "llm_provider_succeeded",
                    extra={
                        "provider": entry.name,
                        "duration_ms": round((time.monotonic() - start) * 1000, 1),
                    },
                )
                return result
            except LLMProviderError as exc:
                entry.cooldown_until = self._clock() + self._cooldown_seconds
                logger.warning(
                    "llm_provider_failed",
                    extra={"provider": entry.name, "error": str(exc), "cooldown_seconds": self._cooldown_seconds},
                )
                errors.append(f"{entry.name}: {exc}")

        logger.error("llm_all_providers_failed", extra={"errors": errors})
        raise AllProvidersFailedError("All LLM providers unavailable: " + "; ".join(errors))
