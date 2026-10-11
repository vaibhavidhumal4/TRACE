"""Deterministic scoring of verdicts against ground truth. No LLM anywhere in this package."""

from __future__ import annotations

from datetime import datetime

CATEGORIES = [
    "authorization_failure",
    "warehouse_schema_mismatch",
    "resource_limit_exceeded",
    "dns_or_endpoint_unreachable",
]


def attribute(incident_started_at: datetime, faults: list[dict]) -> dict | None:
    """The fault that was active when the failed execution started: injected_at <= start <= reverted_at.

    A fault not yet reverted counts as still active. Returns None when no fault, or more than one, matches.
    """
    matches = [
        f
        for f in faults
        if f["injected_at"] <= incident_started_at
        and (f["reverted_at"] is None or incident_started_at <= f["reverted_at"])
    ]
    return matches[0] if len(matches) == 1 else None


def score(predicted: str | None, true_category: str) -> bool:
    """Exact match on the closed taxonomy. A missing verdict is wrong."""
    return predicted is not None and predicted == true_category


def accuracy(results: list[dict]) -> float | None:
    if not results:
        return None
    return sum(1 for r in results if r["is_correct"]) / len(results)


def confusion_matrix(results: list[dict]) -> dict[str, dict[str, int]]:
    """matrix[true][predicted]; a missing verdict is counted under 'no_verdict'."""
    columns = [*CATEGORIES, "no_verdict"]
    matrix = {true: dict.fromkeys(columns, 0) for true in CATEGORIES}
    for r in results:
        predicted = r["predicted_category"] if r["predicted_category"] in CATEGORIES else "no_verdict"
        matrix[r["true_category"]][predicted] += 1
    return matrix
