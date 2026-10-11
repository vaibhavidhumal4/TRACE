"""Settings for the maintenance tooling. Environment variable names are neutral on purpose (OPS_*)."""

from __future__ import annotations

import os
from dataclasses import dataclass

import google.auth
from google.auth import impersonated_credentials
from google.auth.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


@dataclass(frozen=True)
class Settings:
    project: str
    region: str
    env: str  # dev | prod
    bq_location: str
    max_bytes_billed: int
    impersonate: str  # service account to act as; empty means use the ambient credentials

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ) if env is None else env
        if not env.get("GCP_PROJECT"):
            raise ValueError("missing required environment variable: GCP_PROJECT")
        trace_env = env.get("TRACE_ENV", "dev")
        if trace_env not in ("dev", "prod"):
            raise ValueError(f"TRACE_ENV must be dev or prod, got {trace_env!r}")
        return cls(
            project=env["GCP_PROJECT"],
            region=env.get("REGION", "us-central1"),
            env=trace_env,
            bq_location=env.get("BQ_LOCATION", "us-central1"),
            max_bytes_billed=int(env.get("BQ_MAX_BYTES_BILLED", str(100 * 1024 * 1024))),
            impersonate=env.get("OPS_IMPERSONATE", ""),
        )

    @property
    def suffix(self) -> str:
        return "_dev" if self.env == "dev" else ""

    @property
    def target_dataset(self) -> str:
        return f"trace_target{self.suffix}"

    @property
    def truth_dataset(self) -> str:
        return f"trace_truth{self.suffix}"

    @property
    def target_job(self) -> str:
        return "trace-target-etl-dev" if self.env == "dev" else "trace-target-etl"

    @property
    def target_job_path(self) -> str:
        return f"projects/{self.project}/locations/{self.region}/jobs/{self.target_job}"

    @property
    def etl_member(self) -> str:
        return f"serviceAccount:trace-etl@{self.project}.iam.gserviceaccount.com"

    def credentials(self) -> Credentials:
        """Ambient credentials inside Cloud Run; a short-lived impersonated token when run by a person."""
        source, _ = google.auth.default(scopes=SCOPES)
        if not self.impersonate:
            return source
        return impersonated_credentials.Credentials(
            source_credentials=source, target_principal=self.impersonate, target_scopes=SCOPES, lifetime=3600
        )
