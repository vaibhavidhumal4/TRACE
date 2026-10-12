"""Postmortem: a short Markdown report from the incident timeline. Gemini, with Groq as the fallback when Gemini's
quota is exhausted or Gemini fails repeatedly. The model that actually wrote the text is recorded with it, along
with the reason for any fallback."""

from __future__ import annotations

import json
import time

from api.llm import LLMError

SYSTEM = """You write incident postmortems for a data engineering team. Write clear, factual Markdown, 250 to 400 \
words, with exactly these sections: "## Summary", "## Impact", "## Timeline", "## Root cause", "## Evidence", \
"## Proposed remediation", "## Follow-ups". Use only the facts provided. The remediation steps are proposals that \
have NOT been executed; say so. Do not invent times, names or numbers."""


def build_prompt(incident: dict, stages: dict[str, dict]) -> str:
    facts = {
        "incident_id": incident["incident_id"],
        "detected_at": incident["detected_at"],
        "failed_execution": incident["source_execution"],
        "triage": stages.get("triage", {}),
        "root_cause": {
            k: stages.get("root_cause", {}).get(k)
            for k in ("root_cause_category", "hypothesis", "confidence", "evidence")
        },
        "proposed_remediation": stages.get("remediation", {}).get("proposals", []),
    }
    return "Incident facts:\n" + json.dumps(facts, default=str, indent=1)[:9000]


def run(incident_id: str, deps) -> dict:
    incident = deps.store.incident(incident_id)
    stages = {e["stage"]: e["payload"] for e in deps.store.events(incident_id)}
    prompt = build_prompt(incident, stages)
    started = time.monotonic()
    calls = 0
    fallback_reason = None
    try:
        completion = deps.llm.gemini(SYSTEM, prompt)
    except LLMError as exc:
        # Gemini's quota is exhausted, or it failed again after the client's own retry (5xx or timeout).
        calls += 1
        fallback_reason = str(exc)
        completion = deps.llm.groq(SYSTEM, prompt, json_mode=False, max_tokens=1500)
    calls += completion.calls
    latency_ms = int((time.monotonic() - started) * 1000)
    payload = {
        "markdown": completion.text.strip(),
        "model": completion.model,
        "fallback_reason": fallback_reason,
        "llm_calls": calls,
    }
    deps.store.write_verdict(
        incident_id,
        "postmortem",
        hypothesis=payload["markdown"],
        model=completion.model,
        llm_calls=calls,
        latency_ms=latency_ms,
    )
    deps.store.append_event(incident_id, "postmortem", "postmortem", payload)
    return payload
