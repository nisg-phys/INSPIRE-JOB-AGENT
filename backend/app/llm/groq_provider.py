"""Groq adapter for the LLMProvider interface."""

from __future__ import annotations

import groq

from app.llm.base import LLMProviderError


class GroqProvider:
    MODEL = "openai/gpt-oss-120b"

    def __init__(self, api_key: str) -> None:
        self._client = groq.Groq(api_key=api_key)

    def complete_json(self, system: str, user: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
        except groq.APIError as exc:
            raise LLMProviderError(f"Groq request failed: {exc}") from exc
        return response.choices[0].message.content
