"""Settings for the dashboard service, from environment variables only."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

BYTE_CAP = 100 * 1024 * 1024  # hard ceiling for maximum_bytes_billed; the environment can only lower it
MIN_CACHE_TTL_S = 60  # the environment can only raise it
_DATASET = re.compile(r"^[A-Za-z0-9_]{1,1024}$")


@dataclass(frozen=True)
class Settings:
    project: str
    bq_location: str
    results_dataset: str
    max_bytes_billed: int
    cache_ttl_s: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ) if env is None else env
        if not env.get("GCP_PROJECT"):
            raise ValueError("missing required environment variable: GCP_PROJECT")
        dataset = env.get("RESULTS_DATASET", "trace_results")
        if not _DATASET.match(dataset):
            raise ValueError(f"bad RESULTS_DATASET: {dataset!r}")
        return cls(
            project=env["GCP_PROJECT"],
            bq_location=env.get("BQ_LOCATION", "us-central1"),
            results_dataset=dataset,
            max_bytes_billed=min(int(env.get("BQ_MAX_BYTES_BILLED", str(BYTE_CAP))), BYTE_CAP),
            cache_ttl_s=max(int(env.get("CACHE_TTL_S", str(MIN_CACHE_TTL_S))), MIN_CACHE_TTL_S),
        )
