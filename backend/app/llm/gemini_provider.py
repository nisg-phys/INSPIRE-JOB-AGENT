"""Gemini adapter for the LLMProvider interface, via Google's OpenAI-compatible endpoint."""

from __future__ import annotations

import openai

from app.llm.base import LLMProviderError

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GeminiProvider:
    MODEL = "gemini-3.6-flash"

    def __init__(self, api_key: str) -> None:
        self._client = openai.OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL)

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
            raise LLMProviderError(f"Gemini request failed: {exc}") from exc
        return response.choices[0].message.content
