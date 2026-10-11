"""The orchestrator: one LangGraph StateGraph that runs the four stages for one incident.

load_state -> triage -> root_cause -> remediation -> postmortem -> END

Each node wraps an agent function unchanged. A stage whose `{incident_id}:{stage}` event already exists is skipped,
which is how a repeated request or a restart resumes. Before a stage that calls an LLM, the per-incident and daily
call caps are checked; if either is reached the graph goes to `degraded`, records why, and ends without raising.
No checkpointer: durability is the append-only incident_events table.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from api.agents import postmortem, remediation, root_cause, triage
from api.llm import LLMError

STAGES: list[tuple[str, Callable[[str, Any], dict], bool]] = [
    ("triage", triage.run, False),
    ("root_cause", root_cause.run, True),
    ("remediation", remediation.run, False),
    ("postmortem", postmortem.run, True),
]


class State(TypedDict):
    incident_id: str
    memory_enabled: bool
    completed_stages: list[str]
    llm_calls: int
    status: str


@dataclass
class Deps:
    store: Any
    tools: Any
    llm: Any
    incident_call_cap: int
    daily_call_cap: int


def build_graph(deps: Deps):
    def load_state(state: State) -> dict:
        events = deps.store.events(state["incident_id"])
        done = [e["stage"] for e in events if e["stage"] in {name for name, _, _ in STAGES}]
        calls = sum(int(e["payload"].get("llm_calls", 0) or 0) for e in events)
        return {"completed_stages": done, "llm_calls": calls, "status": "running"}

    def make_node(name: str, fn: Callable[[str, Any], dict], uses_llm: bool):
        def node(state: State) -> dict:
            if name in state["completed_stages"] or state["status"] == "degraded":
                return {}
            if uses_llm:
                reason = None
                if state["llm_calls"] >= deps.incident_call_cap:
                    reason = f"per-incident cap of {deps.incident_call_cap} LLM calls reached"
                elif deps.store.llm_calls_today() >= deps.daily_call_cap:
                    reason = f"daily cap of {deps.daily_call_cap} LLM calls reached"
                if reason:
                    return {"status": "degraded", "degraded_reason": reason, "degraded_stage": name}
            try:
                result = fn(state["incident_id"], deps)
            except LLMError as exc:
                return {"status": "degraded", "degraded_reason": f"{type(exc).__name__}: {exc}", "degraded_stage": name}
            return {
                "completed_stages": [*state["completed_stages"], name],
                "llm_calls": state["llm_calls"] + int(result.get("llm_calls", 0) or 0),
            }

        return node

    def degraded(state: dict) -> dict:
        deps.store.append_event(
            state["incident_id"],
            "llm_cap_reached",
            "orchestrator",
            {"stage": state.get("degraded_stage"), "reason": state.get("degraded_reason")},
        )
        return {"status": "degraded"}

    def finish(state: State) -> dict:
        return {"status": "complete"}

    class FullState(State, total=False):
        degraded_reason: str
        degraded_stage: str

    graph = StateGraph(FullState)
    graph.add_node("load_state", load_state)
    graph.add_node("degraded", degraded)
    graph.add_node("finish", finish)
    graph.add_edge(START, "load_state")
    previous = "load_state"
    for name, fn, uses_llm in STAGES:
        graph.add_node(name, make_node(name, fn, uses_llm))
        if previous == "load_state":
            graph.add_edge(previous, name)
        else:
            graph.add_conditional_edges(
                previous, lambda s, nxt=name: "degraded" if s["status"] == "degraded" else nxt, [name, "degraded"]
            )
        previous = name
    graph.add_conditional_edges(
        previous, lambda s: "degraded" if s["status"] == "degraded" else "finish", ["finish", "degraded"]
    )
    graph.add_edge("degraded", END)
    graph.add_edge("finish", END)
    return graph.compile()


def run_incident(incident_id: str, deps: Deps) -> dict:
    initial: State = {
        "incident_id": incident_id,
        "memory_enabled": False,
        "completed_stages": [],
        "llm_calls": 0,
        "status": "new",
    }
    return build_graph(deps).invoke(initial)
