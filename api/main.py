"""HTTP entry point of trace-api. Not public: Cloud Run IAM decides who may call it.

POST /incidents takes the name of a failed execution, opens the incident for it (or finds the existing one) and runs
the orchestrator to completion inside the request.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException

from api.config import Settings, Target
from api.llm import LLM
from api.orchestrator.graph import Deps, run_incident
from api.schemas import IncidentRequest, IncidentResponse
from api.store import Store
from api.tools import Tools

app = FastAPI(title="trace-api", docs_url=None, redoc_url=None, openapi_url=None)


def log(severity: str, message: str, **fields: object) -> None:
    entry = {"severity": severity, "message": message, "time": datetime.now(UTC).isoformat(), **fields}
    sys.stdout.write(json.dumps(entry, default=str) + "\n")
    sys.stdout.flush()


def build_deps(target: Target) -> Deps:
    settings = Settings.from_env()
    store = Store(settings, target)
    return Deps(
        store=store,
        tools=Tools(settings, target, store),
        llm=LLM(),
        incident_call_cap=settings.llm_incident_call_cap,
        daily_call_cap=settings.llm_daily_call_cap,
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/incidents", response_model=IncidentResponse)
def handle_incident(request: IncidentRequest) -> IncidentResponse:
    target = Target(request.env)
    if not request.execution.startswith(f"{target.job}-"):
        raise HTTPException(status_code=422, detail=f"execution does not belong to job {target.job}")
    deps = build_deps(target)
    incident_id = deps.store.open_incident(request.execution, detector="direct_call")
    # Detection facts, recorded once: when the failed execution ran. The evaluator attributes by this time.
    deps.store.append_event(
        incident_id, "execution_window", "detector", deps.tools.get_execution_times(request.execution)
    )
    log("INFO", "incident accepted", incident_id=incident_id, execution=request.execution, env=request.env)
    state = run_incident(incident_id, deps)
    stages = {e["stage"]: e["payload"] for e in deps.store.events(incident_id)}
    verdict = stages.get("root_cause", {})
    log(
        "INFO",
        "incident processed",
        incident_id=incident_id,
        status=state["status"],
        completed_stages=state["completed_stages"],
        llm_calls=state["llm_calls"],
        degraded_reason=state.get("degraded_reason"),
    )
    return IncidentResponse(
        incident_id=incident_id,
        status=state["status"],
        completed_stages=state["completed_stages"],
        llm_calls=state["llm_calls"],
        root_cause_category=verdict.get("root_cause_category"),
        confidence=verdict.get("confidence"),
        degraded_reason=state.get("degraded_reason"),
    )
