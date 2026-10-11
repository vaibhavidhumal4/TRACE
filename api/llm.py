"""The only LLM interface: two providers over plain HTTPS, with call counting.

Root-Cause uses Groq only (one model for every accuracy number). Postmortem uses Gemini and falls back to Groq when
Gemini's quota is exhausted. Keys come from environment variables populated from Secret Manager; they are never logged.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import httpx

from api.config import DEFAULT_POSTMORTEM_MODEL, ROOT_CAUSE_MODEL

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


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
        self._groq_key = clean_key(env.get("GROQ_API_KEY", ""))
        self._gemini_key = clean_key(env.get("GEMINI_API_KEY", ""))
        self.postmortem_model = env.get("POSTMORTEM_MODEL", "").strip() or DEFAULT_POSTMORTEM_MODEL
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
            if response.status_code in (500, 502, 503, 504) and attempt == 1:
                time.sleep(10)  # "high demand" and similar are usually brief
                continue
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

    def gemini(self, system: str, user: str, *, max_tokens: int = 6000) -> Completion:
        if not self._gemini_key:
            raise LLMError("gemini: no API key configured")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
        }
        data, calls = self._post(
            GEMINI_URL.format(model=self.postmortem_model), {"x-goog-api-key": self._gemini_key}, body, "gemini"
        )
        try:
            text = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("gemini: unexpected response shape") from exc
        if not text.strip():
            raise LLMError("gemini: empty response")
        return Completion(text, self.postmortem_model, calls)

    def gemini_model_status(self) -> dict:
        """Ask the provider's model list whether the configured postmortem model exists and can generate text.
        Makes no generation call and uses no generation quota."""
        if not self._gemini_key:
            raise LLMError("gemini: no API key configured")
        try:
            response = self._client.get(
                GEMINI_MODELS_URL, headers={"x-goog-api-key": self._gemini_key}, params={"pageSize": 1000}
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"gemini: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise LLMError(f"gemini: HTTP {response.status_code}: {response.text[:300]}")
        models = response.json().get("models", [])
        wanted = f"models/{self.postmortem_model}"
        match = next((m for m in models if m.get("name") == wanted), None)
        return {
            "postmortem_model": self.postmortem_model,
            "listed": match is not None,
            "supports_generate_content": bool(
                match and "generateContent" in match.get("supportedGenerationMethods", [])
            ),
            "listed_flash_models": sorted(m["name"].split("/", 1)[1] for m in models if "flash" in m.get("name", "")),
        }


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_key(raw: str) -> str:
    """Remove what a terminal paste can leave in a stored key: escape sequences (such as bracketed-paste
    markers), other control characters, and surrounding whitespace. A control character inside the
    Authorization header makes the provider reject the request with an empty HTTP 400."""
    return _CONTROL.sub("", _ANSI_ESCAPE.sub("", raw)).strip()


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
