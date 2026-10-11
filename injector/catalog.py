"""The fault catalog: what can be injected, the category it should be diagnosed as, and parameter variants.

Parameter values are chosen to look like ordinary operational changes; nothing an agent can observe names a fault.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fault:
    fault_type: str
    expected_category: str
    variants: tuple[dict, ...]


CATALOG: dict[str, Fault] = {
    f.fault_type: f
    for f in (
        Fault(
            "iam_revoked",
            "authorization_failure",
            ({"role": "roles/bigquery.dataEditor"},) * 3,
        ),
        Fault(
            "bq_extra_column",
            "warehouse_schema_mismatch",
            (
                {"column": "data_source", "type": "STRING"},
                {"column": "station_count", "type": "INT64"},
                {"column": "quality_flag", "type": "BOOL"},
            ),
        ),
        Fault(
            "task_timeout_kill",
            "resource_limit_exceeded",
            ({"timeout_seconds": 3}, {"timeout_seconds": 4}, {"timeout_seconds": 5}),
        ),
        Fault(
            "bad_endpoint_url",
            "dns_or_endpoint_unreachable",
            (
                {"url": "https://aq-gateway.internal/v1/air-quality"},
                {"url": "https://air-quality-cache.internal/v1/air-quality"},
                {"url": "https://openmeteo-proxy.internal/v1/air-quality"},
            ),
        ),
    )
}

CATEGORIES: tuple[str, ...] = tuple(f.expected_category for f in CATALOG.values())
