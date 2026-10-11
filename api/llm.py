"""The only LLM interface: two providers over plain HTTPS, with call counting.

Root-Cause uses Groq only (one model for every accuracy number). Postmortem uses Gemini and falls back to Groq when
Gemini's quota is exhausted. Keys come from environment variables populated from Secret Manager; they are never logged.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from api.config import POSTMORTEM_MODEL, ROOT_CAUSE_MODEL

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(Exception):
    """A provider call failed for a reason other than quota."""


class QuotaExhausted(LLMError):
    """The provider refused the call because a rate or daily limit was reached."""


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    calls: int  # HTTP calls actually made, including a retry


class LLM:
    def __init__(self, env: dict[str, str] | None = None, client: httpx.Client | None = None) -> None:
        env = dict(os.environ) if env is None else env
        # A secret saved with a trailing newline or space would make an invalid header.
        self._groq_key = env.get("GROQ_API_KEY", "").strip()
        self._gemini_key = env.get("GEMINI_API_KEY", "").strip()
        self._client = client or httpx.Client(timeout=90)

    def _post(self, url: str, headers: dict, body: dict, provider: str) -> tuple[dict, int]:
        """One call, plus one retry after a short provider-requested wait on a rate limit."""
        calls = 0
        for attempt in (1, 2):
            calls += 1
            try:
                response = self._client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                raise LLMError(f"{provider}: {type(exc).__name__}") from exc
            if response.status_code == 429:
                wait = _retry_after(response)
                if attempt == 1 and wait is not None and wait <= 45:
                    time.sleep(wait)
                    continue
                raise QuotaExhausted(f"{provider}: HTTP 429")
            if response.status_code >= 400:
                detail = (
                    f"{response.headers.get('content-type', 'no content-type')}, {len(response.content)} bytes, "
                    f"server {response.headers.get('server', '?')}"
                )
                raise LLMError(f"{provider}: HTTP {response.status_code} ({detail}): {response.text[:300]}")
            return response.json(), calls
        raise QuotaExhausted(f"{provider}: HTTP 429")

    def groq(self, system: str, user: str, *, json_mode: bool, max_tokens: int = 1200) -> Completion:
        if not self._groq_key:
            raise LLMError("groq: no API key configured")
        body: dict = {
            "model": ROOT_CAUSE_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "reasoning_effort": "low",
            "max_completion_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data, calls = self._post(GROQ_URL, {"Authorization": f"Bearer {self._groq_key}"}, body, "groq")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("groq: unexpected response shape") from exc
        return Completion(text, ROOT_CAUSE_MODEL, calls)

    def gemini(self, system: str, user: str, *, max_tokens: int = 2048) -> Completion:
        if not self._gemini_key:
            raise LLMError("gemini: no API key configured")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_tokens,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        data, calls = self._post(
            GEMINI_URL.format(model=POSTMORTEM_MODEL), {"x-goog-api-key": self._gemini_key}, body, "gemini"
        )
        try:
            text = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("gemini: unexpected response shape") from exc
        if not text.strip():
            raise LLMError("gemini: empty response")
        return Completion(text, POSTMORTEM_MODEL, calls)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
