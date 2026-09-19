"""OpenAI adapter for the LLMProvider interface."""

from __future__ import annotations

import openai
import opik

from app.llm.base import LLMProviderError


class OpenAIProvider:
    MODEL = "gpt-5.4-mini"

    def __init__(self, api_key: str) -> None:
        self._client = openai.OpenAI(api_key=api_key)

    @opik.track(type="llm", name="openai.complete_json")
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
        except openai.APIError as exc:
            raise LLMProviderError(f"OpenAI request failed: {exc}") from exc
        return response.choices[0].message.content
