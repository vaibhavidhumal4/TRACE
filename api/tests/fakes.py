"""In-memory stand-ins for BigQuery, the evidence tools and the LLM providers."""

from __future__ import annotations

import json

from api.llm import Completion, QuotaExhausted
from api.orchestrator.graph import Deps

FORBIDDEN_LOG = {
    "severity": "ERROR",
    "phase": "end",
    "error_class": "Forbidden",
    "message": "run failed: Forbidden: 403 Access Denied: Table p:trace_target_dev.pipeline_runs: User does not "
    "have permission to query table",
    "count": 1,
}


class FakeStore:
    def __init__(self, run_rows: list[dict] | None = None, calls_today: int = 0) -> None:
        self.incidents: dict[str, dict] = {}
        self.event_rows: list[dict] = []
        self.verdicts: list[dict] = []
        self._run_rows = run_rows or []
        self._calls_today = calls_today

    def open_incident(self, execution: str, detector: str) -> str:
        for incident in self.incidents.values():
            if incident["source_execution"] == execution:
                return incident["incident_id"]
        incident_id = f"inc-{len(self.incidents) + 1}"
        self.incidents[incident_id] = {
            "incident_id": incident_id,
            "detected_at": "2026-10-11T12:30:00Z",
            "detector": detector,
            "source_execution": execution,
            "status": "open",
        }
        return incident_id

    def incident(self, incident_id: str) -> dict:
        return self.incidents[incident_id]

    def events(self, incident_id: str) -> list[dict]:
        return [e for e in self.event_rows if e["incident_id"] == incident_id]

    def append_event(self, incident_id: str, stage: str, agent: str, payload: dict) -> None:
        if any(e["incident_id"] == incident_id and e["stage"] == stage for e in self.event_rows):
            return
        payload = json.loads(json.dumps(payload, default=str))
        self.event_rows.append({"incident_id": incident_id, "stage": stage, "agent": agent, "payload": payload})

    def write_verdict(self, incident_id: str, agent_name: str, **fields) -> None:
        if any(v["incident_id"] == incident_id and v["agent_name"] == agent_name for v in self.verdicts):
            return
        self.verdicts.append({"incident_id": incident_id, "agent_name": agent_name, **fields})

    def llm_calls_today(self) -> int:
        return self._calls_today + sum(v.get("llm_calls", 0) for v in self.verdicts)

    def run_rows(self, execution: str) -> list[dict]:
        return self._run_rows

    def recent_end_rows(self, n: int = 5) -> list[dict]:
        return [{"execution_name": "trace-target-etl-dev-prev1", "status": "SUCCESS", "states_ok": 36}]

    def freshness(self) -> dict:
        return {"newest_ts": "2026-10-11T16:00:00", "staleness_hours": 0.4}


class FakeTools:
    def __init__(self, store: FakeStore, logs: list[dict] | None = None) -> None:
        self.store = store
        self._logs = logs if logs is not None else [FORBIDDEN_LOG]

    def get_pipeline_runs(self, execution: str) -> dict:
        return {"this_execution": self.store.run_rows(execution), "recent_finished_runs": self.store.recent_end_rows()}

    def get_run_logs(self, execution: str) -> list[dict]:
        return self._logs

    def get_recent_changes(self, hours: float, before=None) -> list[dict]:
        return [{"method": "SetIamPolicy", "resource": "datasets/trace_target_dev", "by": "ops-automation@p"}]

    def get_job_config(self) -> dict:
        return {"job": "trace-target-etl-dev", "task_timeout_seconds": 900, "env": {}}

    def get_data_freshness(self) -> dict:
        return self.store.freshness()

    def get_execution_times(self, execution: str) -> dict:
        return {"execution_started_at": "2026-10-11T12:27:34+00:00", "execution_completed_at": None}


class FakeLLM:
    def __init__(self, groq_replies: list[str] | None = None, gemini_quota_exhausted: bool = False) -> None:
        self.groq_replies = list(groq_replies or [])
        self.gemini_quota_exhausted = gemini_quota_exhausted
        self.groq_calls: list[dict] = []
        self.gemini_calls: list[dict] = []

    def groq(self, system: str, user: str, *, json_mode: bool, max_tokens: int = 1200) -> Completion:
        self.groq_calls.append({"system": system, "user": user, "json_mode": json_mode})
        if json_mode:
            text = self.groq_replies.pop(0) if self.groq_replies else verdict_json()
        else:
            text = "## Summary\nWritten by the fallback model."
        return Completion(text, "openai/gpt-oss-120b", 1)

    def gemini(self, system: str, user: str, *, max_tokens: int = 2048) -> Completion:
        self.gemini_calls.append({"system": system, "user": user})
        if self.gemini_quota_exhausted:
            raise QuotaExhausted("gemini: HTTP 429")
        return Completion("## Summary\nThe run failed.", "gemini-2.5-flash", 1)


def verdict_json(category: str = "authorization_failure", confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "root_cause_category": category,
            "hypothesis": "The pipeline identity was denied access to the run ledger table.",
            "confidence": confidence,
            "evidence": ["403 Access Denied: Table p:trace_target_dev.pipeline_runs"],
        }
    )


def make_deps(store: FakeStore | None = None, llm: FakeLLM | None = None, **caps) -> Deps:
    store = store or FakeStore()
    return Deps(
        store=store,
        tools=FakeTools(store),
        llm=llm or FakeLLM(),
        incident_call_cap=caps.get("incident_call_cap", 12),
        daily_call_cap=caps.get("daily_call_cap", 60),
    )
