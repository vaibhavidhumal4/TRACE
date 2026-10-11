"""Fixture-based unit tests for the four agents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from api.agents import postmortem, remediation, root_cause, triage
from api.schemas import Category, Severity
from api.tests.fakes import FakeLLM, FakeStore, make_deps, verdict_json

START = {"phase": "START", "status": "RUNNING"}


def end(status: str, ok: int | None = None) -> dict:
    return {"phase": "END", "status": status, "states_ok": ok, "states_total": 36}


@pytest.mark.parametrize(
    ("rows", "staleness", "expected", "status"),
    [
        ([START, end("FAILED", 0)], 0.5, Severity.HIGH, "FAILED"),
        ([START, end("FAILED", 0)], 7.0, Severity.CRITICAL, "FAILED"),
        ([START], 0.5, Severity.HIGH, "RUNNING_NO_END"),
        ([], 0.5, Severity.HIGH, "NO_RECORD"),
        ([], None, Severity.CRITICAL, "NO_RECORD"),
        ([START, end("PARTIAL", 20)], 0.5, Severity.MEDIUM, "PARTIAL"),
        ([START, end("PARTIAL", 20)], 9.0, Severity.HIGH, "PARTIAL"),
        ([START, end("SUCCESS", 36)], 0.5, Severity.LOW, "SUCCESS"),
    ],
)
def test_triage_rules(rows, staleness, expected, status):
    result = triage.rate(rows, {"staleness_hours": staleness})
    assert result.severity == expected
    assert result.run_status == status


def test_triage_writes_one_event_and_uses_no_llm():
    deps = make_deps(FakeStore(run_rows=[START]))
    incident_id = deps.store.open_incident("trace-target-etl-dev-abc12", "direct_call")
    result = triage.run(incident_id, deps)
    assert result["llm_calls"] == 0 and result["severity"] == "High"
    assert [e["stage"] for e in deps.store.events(incident_id)] == ["triage"]
    assert deps.llm.groq_calls == [] and deps.llm.gemini_calls == []


def test_taxonomy_is_exactly_the_four_categories():
    assert {c.value for c in Category} == {
        "authorization_failure",
        "warehouse_schema_mismatch",
        "resource_limit_exceeded",
        "dns_or_endpoint_unreachable",
    }


def test_root_cause_makes_one_call_and_records_the_verdict():
    deps = make_deps()
    incident_id = deps.store.open_incident("trace-target-etl-dev-abc12", "direct_call")
    result = root_cause.run(incident_id, deps)
    assert result["root_cause_category"] == "authorization_failure"
    assert result["llm_calls"] == 1 and len(deps.llm.groq_calls) == 1
    verdict = deps.store.verdicts[0]
    assert verdict["model"] == "openai/gpt-oss-120b"
    assert verdict["evidence"]["seen"]["run_logs"][0]["error_class"] == "Forbidden"
    assert "403 Access Denied" in deps.llm.groq_calls[0]["user"]


def test_root_cause_rejects_a_category_outside_the_taxonomy_then_repairs():
    llm = FakeLLM(groq_replies=[verdict_json("unknown"), verdict_json("resource_limit_exceeded")])
    deps = make_deps(llm=llm)
    incident_id = deps.store.open_incident("trace-target-etl-dev-abc12", "direct_call")
    result = root_cause.run(incident_id, deps)
    assert result["root_cause_category"] == "resource_limit_exceeded"
    assert result["llm_calls"] == 2
    assert "previous reply was rejected" in llm.groq_calls[1]["user"]


def test_root_cause_fails_loudly_when_the_repair_is_also_invalid():
    deps = make_deps(llm=FakeLLM(groq_replies=["not json", verdict_json("unknown")]))
    incident_id = deps.store.open_incident("trace-target-etl-dev-abc12", "direct_call")
    with pytest.raises(ValueError, match="invalid after a repair attempt"):
        root_cause.run(incident_id, deps)
    assert deps.store.verdicts == []


def test_parse_verdict_validates_confidence_range():
    with pytest.raises(ValidationError):
        root_cause.parse_verdict(verdict_json(confidence=1.4))


def test_evidence_text_stays_within_the_prompt_budget():
    evidence = {
        "execution": "x",
        "run_logs": [{"message": "m" * 400, "count": 1} for _ in range(60)],
        "recent_finished_runs": [{"status": "SUCCESS"}] * 5,
        "changes_in_30_min_before_detection": [{"detail": "d" * 500}] * 8,
    }
    assert len(root_cause.evidence_text(evidence)) <= root_cause.MAX_EVIDENCE_CHARS


def test_root_cause_prompt_does_not_carry_answers():
    prompt = root_cause.SYSTEM.lower()
    for word in ("iam_revoked", "bq_extra_column", "task_timeout_kill", "bad_endpoint_url", "inject", "fault"):
        assert word not in prompt


@pytest.mark.parametrize("category", list(Category))
def test_remediation_has_a_standard_fix_for_every_category(category):
    proposals = remediation.propose(category)
    assert proposals and all(p.action and p.rollback for p in proposals)


def test_remediation_is_a_ledger_write_only():
    deps = make_deps()
    incident_id = deps.store.open_incident("trace-target-etl-dev-abc12", "direct_call")
    root_cause.run(incident_id, deps)
    result = remediation.run(incident_id, deps)
    event = deps.store.events(incident_id)[-1]
    assert event["stage"] == "remediation" and event["payload"]["executed"] is False
    assert result["llm_calls"] == 0 and len(deps.llm.groq_calls) == 1  # only the root-cause call


def test_postmortem_uses_gemini_and_records_the_model():
    deps = make_deps()
    incident_id = deps.store.open_incident("trace-target-etl-dev-abc12", "direct_call")
    result = postmortem.run(incident_id, deps)
    assert result["model"] == "gemini-3.8-flash" and result["fallback_reason"] is None
    assert deps.store.verdicts[-1]["model"] == "gemini-3.8-flash"


def test_postmortem_falls_back_to_groq_when_gemini_quota_is_exhausted():
    deps = make_deps(llm=FakeLLM(gemini_quota_exhausted=True))
    incident_id = deps.store.open_incident("trace-target-etl-dev-abc12", "direct_call")
    result = postmortem.run(incident_id, deps)
    assert result["model"] == "openai/gpt-oss-120b"
    assert "429" in result["fallback_reason"]
    assert deps.store.events(incident_id)[-1]["payload"]["model"] == "openai/gpt-oss-120b"


def test_agent_code_never_mentions_ground_truth():
    """The agent service must have no path to truth: no dataset name, no catalog import."""
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text()
        assert "trace_truth" not in text.replace("reach trace_truth, by IAM", ""), path
        assert "injector" not in text, path
        assert "fault_ledger" not in text and "evaluation_results" not in text, path


def test_fakes_round_trip_json():
    assert json.loads(verdict_json())["confidence"] == 0.9


def test_clean_key_removes_paste_artifacts():
    from api.llm import clean_key

    assert clean_key("\x1b[200~abc_DEF-123\x1b[201~\n") == "abc_DEF-123"
    assert clean_key("  abc\x01def\x7f\r\n") == "abcdef"
    assert clean_key("abc_DEF-123") == "abc_DEF-123"
