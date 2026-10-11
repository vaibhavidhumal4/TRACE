"""Root-Cause: gather evidence with the read-only tools, then one LLM call that must pick one closed category."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from pydantic import ValidationError

from api.agents.triage import run_status
from api.schemas import Category, RootCauseVerdict

SYSTEM = """You are the root-cause analyst for a scheduled data pipeline. The pipeline is a Cloud Run job that \
fetches hourly air-quality data for 36 Indian states from an HTTP API and loads it into BigQuery through a staging \
table. It records each run in a run ledger table (a START row, then an END row).

You are given evidence about ONE failed run. Decide the single most likely root cause. Choose exactly one category:

- authorization_failure: the pipeline's own identity was refused access to something it needs (permission denied, \
access denied, HTTP 403 from the warehouse or another Google Cloud service).
- warehouse_schema_mismatch: the warehouse rejected the load because the data's shape did not match the table \
(column count or type mismatch, missing or unexpected column).
- resource_limit_exceeded: the run was stopped by a platform limit before it could finish (task timeout, memory \
limit, quota on the job itself).
- dns_or_endpoint_unreachable: the pipeline could not reach the upstream API at all (name resolution failure, \
connection refused, no route), as opposed to reaching it and getting a bad answer.

Rules:
- Base the answer only on the evidence given. Quote the specific lines or values that decide it.
- A recent configuration or permission change is context, not proof: say whether it explains the error you see.
- Reply with one JSON object and nothing else:
{"root_cause_category": "<one of the four>", "hypothesis": "<2-4 sentences>", "confidence": <0.0-1.0>, \
"evidence": ["<short quoted fact>", "..."]}"""

MAX_EVIDENCE_CHARS = 9000  # about 3K tokens with the instructions
CHANGE_WINDOW_HOURS = 0.5


def _as_utc(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.now(UTC)


def gather_evidence(incident: dict, deps) -> dict:
    execution = incident["source_execution"]
    runs = deps.tools.get_pipeline_runs(execution)
    return {
        "execution": execution,
        "run_ledger_status": run_status(runs["this_execution"]),
        "run_ledger_rows": runs["this_execution"],
        "recent_finished_runs": runs["recent_finished_runs"],
        "run_logs": deps.tools.get_run_logs(execution),
        "current_job_config": deps.tools.get_job_config(),
        # Only the half hour up to detection: incidents can follow each other closely, and older changes
        # belong to earlier incidents.
        "changes_in_30_min_before_detection": deps.tools.get_recent_changes(
            CHANGE_WINDOW_HOURS, before=_as_utc(incident.get("detected_at"))
        ),
        "data_freshness": deps.tools.get_data_freshness(),
    }


def evidence_text(evidence: dict) -> str:
    """Compact JSON for the prompt. If it is too long, the least decisive sections are trimmed first."""
    trimmed = dict(evidence)
    text = json.dumps(trimmed, default=str, separators=(",", ":"))
    for key in ("recent_finished_runs", "changes_in_30_min_before_detection", "run_logs"):
        if len(text) <= MAX_EVIDENCE_CHARS:
            break
        items = trimmed[key]
        trimmed[key] = items[: max(2, len(items) // 2)] if key != "run_logs" else items[-12:]
        text = json.dumps(trimmed, default=str, separators=(",", ":"))
    return text[:MAX_EVIDENCE_CHARS]


def parse_verdict(text: str) -> RootCauseVerdict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the model's reply")
    return RootCauseVerdict.model_validate(json.loads(text[start : end + 1]))


def run(incident_id: str, deps) -> dict:
    incident = deps.store.incident(incident_id)
    evidence = gather_evidence(incident, deps)
    prompt = "Evidence for the failed run:\n" + evidence_text(evidence)
    started = time.monotonic()
    calls = 0
    last_error = ""
    verdict = None
    model = None
    for attempt in (1, 2):  # one repair attempt if the reply is not valid against the schema
        user = (
            prompt
            if attempt == 1
            else (
                f"{prompt}\n\nYour previous reply was rejected: {last_error}\n"
                f"Reply again with only the JSON object. root_cause_category must be one of: "
                f"{', '.join(c.value for c in Category)}."
            )
        )
        completion = deps.llm.groq(SYSTEM, user, json_mode=True)
        calls += completion.calls
        model = completion.model
        try:
            verdict = parse_verdict(completion.text)
            break
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)[:300]
    if verdict is None:
        raise ValueError(f"root-cause reply invalid after a repair attempt: {last_error}")

    latency_ms = int((time.monotonic() - started) * 1000)
    payload = verdict.model_dump(mode="json")
    deps.store.write_verdict(
        incident_id,
        "root_cause",
        category=verdict.root_cause_category.value,
        hypothesis=verdict.hypothesis,
        confidence=verdict.confidence,
        evidence={"cited": verdict.evidence, "seen": evidence, "retrieved_neighbours": []},
        model=model,
        llm_calls=calls,
        latency_ms=latency_ms,
    )
    deps.store.append_event(incident_id, "root_cause", "root_cause", {**payload, "model": model, "llm_calls": calls})
    return {"llm_calls": calls, **payload}
