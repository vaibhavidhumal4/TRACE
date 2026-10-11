"""Runtime configuration, read from environment variables only."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

DEFAULT_OPEN_METEO_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


@dataclass(frozen=True)
class Config:
    project: str
    dataset: str
    table: str
    runs_table: str
    bq_location: str
    open_meteo_url: str
    http_timeout_s: float
    max_concurrency: int
    min_states_ok: int
    max_bytes_billed: int
    execution_name: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ) if env is None else env
        missing = [name for name in ("GCP_PROJECT", "BQ_DATASET") if not env.get(name)]
        if missing:
            raise ValueError(f"missing required environment variables: {', '.join(missing)}")
        return cls(
            project=env["GCP_PROJECT"],
            dataset=env["BQ_DATASET"],
            table=env.get("BQ_TABLE", "aqi_hourly"),
            runs_table=env.get("BQ_RUNS_TABLE", "pipeline_runs"),
            bq_location=env.get("BQ_LOCATION", "us-central1"),
            open_meteo_url=env.get("OPEN_METEO_URL", DEFAULT_OPEN_METEO_URL),
            http_timeout_s=float(env.get("HTTP_TIMEOUT_S", "20")),
            max_concurrency=int(env.get("MAX_CONCURRENCY", "3")),
            min_states_ok=int(env.get("MIN_STATES_OK", "30")),
            max_bytes_billed=int(env.get("BQ_MAX_BYTES_BILLED", str(100 * 1024 * 1024))),
            # Set by Cloud Run on every job execution; empty when run by hand.
            execution_name=env.get("CLOUD_RUN_EXECUTION", ""),
        )

    def public_dict(self) -> dict[str, object]:
        """Non-secret config recorded with each run. Nothing here is a credential."""
        return asdict(self)
