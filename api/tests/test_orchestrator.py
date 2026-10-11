"""Orchestrator tests: the full flow, repeat delivery (TC-26) and the degraded route (TC-27)."""

from __future__ import annotations

from api.llm import LLMError
from api.orchestrator.graph import run_incident
from api.tests.fakes import FakeLLM, FakeStore, make_deps

EXECUTION = "trace-target-etl-dev-abc12"


def test_full_flow_runs_the_four_stages_in_order():
    deps = make_deps()
    incident_id = deps.store.open_incident(EXECUTION, "direct_call")
    state = run_incident(incident_id, deps)
    assert state["status"] == "complete"
    assert state["completed_stages"] == ["triage", "root_cause", "remediation", "postmortem"]
    assert state["llm_calls"] == 2
    assert [e["stage"] for e in deps.store.events(incident_id)] == state["completed_stages"]
    assert sorted(v["agent_name"] for v in deps.store.verdicts) == ["postmortem", "remediation", "root_cause", "triage"]
    assert state["memory_enabled"] is False


def test_tc26_same_request_twice_makes_no_duplicate_verdicts_and_no_second_llm_call():
    deps = make_deps()
    first_id = deps.store.open_incident(EXECUTION, "direct_call")
    run_incident(first_id, deps)
    verdicts, groq, gemini = len(deps.store.verdicts), len(deps.llm.groq_calls), len(deps.llm.gemini_calls)

    second_id = deps.store.open_incident(EXECUTION, "direct_call")
    state = run_incident(second_id, deps)
    assert second_id == first_id
    assert state["status"] == "complete"
    assert len(deps.store.verdicts) == verdicts == 4
    assert (len(deps.llm.groq_calls), len(deps.llm.gemini_calls)) == (groq, gemini) == (1, 1)
    assert len(deps.store.events(first_id)) == 4


def test_resume_after_a_crash_runs_only_the_missing_stages():
    deps = make_deps()
    incident_id = deps.store.open_incident(EXECUTION, "direct_call")
    deps.store.append_event(incident_id, "triage", "triage", {"severity": "High"})
    deps.store.append_event(
        incident_id,
        "root_cause",
        "root_cause",
        {"root_cause_category": "authorization_failure", "confidence": 0.9, "llm_calls": 1},
    )
    state = run_incident(incident_id, deps)
    assert state["status"] == "complete"
    assert deps.llm.groq_calls == [] and len(deps.llm.gemini_calls) == 1
    assert state["llm_calls"] == 2


def test_tc27_daily_cap_reached_ends_in_degraded_without_raising():
    deps = make_deps(FakeStore(calls_today=60), daily_call_cap=60)
    incident_id = deps.store.open_incident(EXECUTION, "direct_call")
    state = run_incident(incident_id, deps)
    assert state["status"] == "degraded"
    assert state["completed_stages"] == ["triage"]
    assert deps.llm.groq_calls == [] and deps.llm.gemini_calls == []
    event = deps.store.events(incident_id)[-1]
    assert event["stage"] == "llm_cap_reached" and "daily cap" in event["payload"]["reason"]
    assert event["payload"]["stage"] == "root_cause"


def test_tc27_per_incident_cap_reached_ends_in_degraded():
    deps = make_deps(incident_call_cap=1)
    incident_id = deps.store.open_incident(EXECUTION, "direct_call")
    state = run_incident(incident_id, deps)
    assert state["status"] == "degraded"
    assert state["completed_stages"] == ["triage", "root_cause", "remediation"]
    assert deps.store.events(incident_id)[-1]["payload"]["stage"] == "postmortem"


def test_provider_failure_in_root_cause_degrades_and_is_not_diagnosed_by_another_model():
    class DownLLM(FakeLLM):
        def groq(self, *args, **kwargs):
            raise LLMError("groq: HTTP 503")

    deps = make_deps(llm=DownLLM())
    incident_id = deps.store.open_incident(EXECUTION, "direct_call")
    state = run_incident(incident_id, deps)
    assert state["status"] == "degraded" and state["completed_stages"] == ["triage"]
    assert deps.llm.gemini_calls == []
    assert not any(v["agent_name"] == "root_cause" for v in deps.store.verdicts)
