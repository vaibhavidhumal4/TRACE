"""Triage: severity from run status, blast radius and staleness. Rules only; no LLM."""

from __future__ import annotations

from api.schemas import Severity, TriageResult

STALE_HOURS = 6.0


def run_status(run_rows: list[dict]) -> str:
    """Status of the execution as its ledger rows show it."""
    end = [r for r in run_rows if r.get("phase") == "END"]
    if end:
        return str(end[-1].get("status"))
    if run_rows:
        return "RUNNING_NO_END"  # started, never finished: the run was cut short
    return "NO_RECORD"  # the run could not even record that it started


def rate(run_rows: list[dict], freshness: dict) -> TriageResult:
    status = run_status(run_rows)
    end = next((r for r in reversed(run_rows) if r.get("phase") == "END"), {})
    states_ok, states_total = end.get("states_ok"), end.get("states_total")
    staleness = freshness.get("staleness_hours")
    staleness = float(staleness) if staleness is not None else None
    stale = staleness is None or staleness > STALE_HOURS

    reasons = [f"run status {status}"]
    if states_total:
        reasons.append(f"{states_ok or 0}/{states_total} states fetched")
    reasons.append("no data in the table" if staleness is None else f"last ingestion {staleness:.1f} h ago")

    if status in ("FAILED", "RUNNING_NO_END", "NO_RECORD"):
        severity = Severity.CRITICAL if stale else Severity.HIGH
        reasons.append("no rows loaded by this run")
    elif status == "PARTIAL":
        severity = Severity.HIGH if stale else Severity.MEDIUM
    else:
        severity = Severity.MEDIUM if stale else Severity.LOW
    return TriageResult(
        severity=severity,
        run_status=status,
        states_ok=states_ok,
        states_total=states_total,
        staleness_hours=staleness,
        reasons=reasons,
    )


def run(incident_id: str, deps) -> dict:
    incident = deps.store.incident(incident_id)
    result = rate(deps.store.run_rows(incident["source_execution"]), deps.tools.get_data_freshness())
    payload = result.model_dump(mode="json")
    deps.store.write_verdict(incident_id, "triage", evidence=payload)
    deps.store.append_event(incident_id, "triage", "triage", payload)
    return {"llm_calls": 0, **payload}
