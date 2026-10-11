"""Settings for the agent service, from environment variables only."""

from __future__ import annotations

import os
from dataclasses import dataclass

ROOT_CAUSE_MODEL = "openai/gpt-oss-120b"
POSTMORTEM_MODEL = "gemini-2.5-flash"


@dataclass(frozen=True)
class Settings:
    project: str
    region: str
    bq_location: str
    max_bytes_billed: int
    log_bucket: str
    log_view: str
    llm_incident_call_cap: int
    llm_daily_call_cap: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ) if env is None else env
        if not env.get("GCP_PROJECT"):
            raise ValueError("missing required environment variable: GCP_PROJECT")
        return cls(
            project=env["GCP_PROJECT"],
            region=env.get("REGION", "us-central1"),
            bq_location=env.get("BQ_LOCATION", "us-central1"),
            max_bytes_billed=int(env.get("BQ_MAX_BYTES_BILLED", str(100 * 1024 * 1024))),
            log_bucket=env.get("LOG_BUCKET", "trace-target-logs"),
            log_view=env.get("LOG_VIEW", "target-only"),
            llm_incident_call_cap=int(env.get("LLM_INCIDENT_CALL_CAP", "12")),
            llm_daily_call_cap=int(env.get("LLM_DAILY_CALL_CAP", "60")),
        )

    @property
    def log_view_path(self) -> str:
        return f"projects/{self.project}/locations/{self.region}/buckets/{self.log_bucket}/views/{self.log_view}"


@dataclass(frozen=True)
class Target:
    """Which copy of the pipeline an incident belongs to. Chosen per request: dev or prod."""

    env: str

    def __post_init__(self) -> None:
        if self.env not in ("dev", "prod"):
            raise ValueError(f"env must be dev or prod, got {self.env!r}")

    @property
    def suffix(self) -> str:
        return "_dev" if self.env == "dev" else ""

    @property
    def target_dataset(self) -> str:
        return f"trace_target{self.suffix}"

    @property
    def ledger_dataset(self) -> str:
        return f"trace_ledger{self.suffix}"

    @property
    def job(self) -> str:
        return "trace-target-etl-dev" if self.env == "dev" else "trace-target-etl"
