"""Unit tests for the deterministic scoring."""

from __future__ import annotations

from datetime import UTC, datetime

from evaluator.scoring import CATEGORIES, accuracy, attribute, confusion_matrix, score


def t(minute: int) -> datetime:
    return datetime(2026, 10, 12, 6, minute, tzinfo=UTC)


FAULTS = [
    {"fault_id": "f1", "expected_category": "authorization_failure", "injected_at": t(0), "reverted_at": t(5)},
    {"fault_id": "f2", "expected_category": "resource_limit_exceeded", "injected_at": t(10), "reverted_at": t(15)},
    {"fault_id": "f3", "expected_category": "warehouse_schema_mismatch", "injected_at": t(20), "reverted_at": None},
]


def test_attribution_by_time_window():
    assert attribute(t(2), FAULTS)["fault_id"] == "f1"
    assert attribute(t(12), FAULTS)["fault_id"] == "f2"
    assert attribute(t(30), FAULTS)["fault_id"] == "f3"  # not yet reverted: still active
    assert attribute(t(7), FAULTS) is None  # between faults: not attributable


def test_overlapping_faults_are_not_attributed():
    overlapping = [*FAULTS, {"fault_id": "f4", "expected_category": "x", "injected_at": t(1), "reverted_at": t(3)}]
    assert attribute(t(2), overlapping) is None


def test_score_is_exact_match_and_missing_verdict_is_wrong():
    assert score("authorization_failure", "authorization_failure") is True
    assert score("resource_limit_exceeded", "authorization_failure") is False
    assert score(None, "authorization_failure") is False


def test_accuracy_and_confusion_matrix():
    results = [
        {"true_category": "authorization_failure", "predicted_category": "authorization_failure", "is_correct": True},
        {
            "true_category": "authorization_failure",
            "predicted_category": "resource_limit_exceeded",
            "is_correct": False,
        },
        {"true_category": "dns_or_endpoint_unreachable", "predicted_category": None, "is_correct": False},
        {
            "true_category": "warehouse_schema_mismatch",
            "predicted_category": "warehouse_schema_mismatch",
            "is_correct": True,
        },
    ]
    assert accuracy(results) == 0.5
    assert accuracy([]) is None
    matrix = confusion_matrix(results)
    assert set(matrix) == set(CATEGORIES)
    assert matrix["authorization_failure"]["authorization_failure"] == 1
    assert matrix["authorization_failure"]["resource_limit_exceeded"] == 1
    assert matrix["dns_or_endpoint_unreachable"]["no_verdict"] == 1
    assert sum(sum(row.values()) for row in matrix.values()) == 4
