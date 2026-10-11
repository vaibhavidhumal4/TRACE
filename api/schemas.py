"""Validated shapes of what each component produces."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Category(StrEnum):
    """The closed root-cause taxonomy. Root-Cause must answer with exactly one of these."""

    AUTHORIZATION_FAILURE = "authorization_failure"
    WAREHOUSE_SCHEMA_MISMATCH = "warehouse_schema_mismatch"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    DNS_OR_ENDPOINT_UNREACHABLE = "dns_or_endpoint_unreachable"


class Severity(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class TriageResult(BaseModel):
    severity: Severity
    run_status: str
    states_ok: int | None
    states_total: int | None
    staleness_hours: float | None
    reasons: list[str]


class RootCauseVerdict(BaseModel):
    root_cause_category: Category
    hypothesis: str = Field(min_length=1, max_length=1500)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(min_length=1, max_length=8)


class Proposal(BaseModel):
    action: str
    risk: str
    blast_radius: str
    rollback: str
    confidence: float = Field(ge=0.0, le=1.0)


class IncidentRequest(BaseModel):
    execution: str = Field(pattern=r"^[a-z0-9-]{1,80}$")
    env: str = Field(default="dev", pattern=r"^(dev|prod)$")


class IncidentResponse(BaseModel):
    incident_id: str
    status: str
    completed_stages: list[str]
    llm_calls: int
    root_cause_category: str | None = None
    confidence: float | None = None
