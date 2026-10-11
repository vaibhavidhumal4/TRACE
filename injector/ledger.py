"""Reads and writes of the ground-truth tables (trace_truth). Only the maintenance and evaluation identities can."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from google.cloud import bigquery as bq

from injector.actions import Clients


def _table(c: Clients, name: str) -> str:
    return f"`{c.settings.project}.{c.settings.truth_dataset}.{name}`"


def record_fault(
    c: Clients,
    *,
    fault_type: str,
    expected_category: str,
    params: dict,
    revert_plan: dict,
    revert_at: datetime,
    seed: int | None,
    campaign_id: str,
    split: str = "heldout",
    plan_position: int | None = None,
) -> str:
    """Write the ledger row. Called BEFORE the fault is injected, so the reverter can always undo it."""
    fault_id = str(uuid.uuid4())
    c.query(
        f"""
        INSERT INTO {_table(c, "fault_ledger")}
          (fault_id, fault_type, expected_category, injected_at, revert_at, reverted_at, params, seed,
           campaign_id, revert_plan, split, plan_position)
        VALUES (@fault_id, @fault_type, @expected_category, CURRENT_TIMESTAMP(), @revert_at, NULL,
                PARSE_JSON(@params), @seed, @campaign_id, PARSE_JSON(@revert_plan), @split, @plan_position)""",
        [
            bq.ScalarQueryParameter("fault_id", "STRING", fault_id),
            bq.ScalarQueryParameter("fault_type", "STRING", fault_type),
            bq.ScalarQueryParameter("expected_category", "STRING", expected_category),
            bq.ScalarQueryParameter("revert_at", "TIMESTAMP", revert_at),
            bq.ScalarQueryParameter("params", "STRING", json.dumps(params)),
            bq.ScalarQueryParameter("seed", "INT64", seed),
            bq.ScalarQueryParameter("campaign_id", "STRING", campaign_id),
            bq.ScalarQueryParameter("revert_plan", "STRING", json.dumps(revert_plan)),
            bq.ScalarQueryParameter("split", "STRING", split),
            bq.ScalarQueryParameter("plan_position", "INT64", plan_position),
        ],
    )
    return fault_id


def mark_reverted(c: Clients, fault_id: str) -> None:
    c.query(
        f"""
        UPDATE {_table(c, "fault_ledger")} SET reverted_at = CURRENT_TIMESTAMP()
        WHERE fault_id = @fault_id AND reverted_at IS NULL
          AND DATE(injected_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)""",
        [bq.ScalarQueryParameter("fault_id", "STRING", fault_id)],
    )


def open_faults(c: Clients, *, due_only: bool, fault_id: str | None = None) -> list[dict]:
    """Faults not yet reverted. With due_only, only those whose revert_at has passed."""
    conditions = ["reverted_at IS NULL", "DATE(injected_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"]
    params = []
    if due_only:
        conditions.append("revert_at <= CURRENT_TIMESTAMP()")
    if fault_id:
        conditions.append("fault_id = @fault_id")
        params.append(bq.ScalarQueryParameter("fault_id", "STRING", fault_id))
    job = c.query(
        f"""
        SELECT fault_id, fault_type, TO_JSON_STRING(revert_plan) AS revert_plan, revert_at
        FROM {_table(c, "fault_ledger")} WHERE {" AND ".join(conditions)} ORDER BY injected_at""",
        params,
    )
    return [
        {"fault_id": r["fault_id"], "fault_type": r["fault_type"], "revert_plan": json.loads(r["revert_plan"])}
        for r in job.result()
    ]


def record_plan(c: Clients, campaign_id: str, seed: int, items: list[dict]) -> None:
    """Write the whole ordered plan in one statement, before anything is injected."""
    rows = [
        {
            "campaign_id": campaign_id,
            "seed": seed,
            "position": i,
            "fault_type": item["fault_type"],
            "expected_category": item["expected_category"],
            "params": json.dumps(item["params"]),
        }
        for i, item in enumerate(items, start=1)
    ]
    c.query(
        f"""
        INSERT INTO {_table(c, "campaign_plan")}
          (campaign_id, seed, position, fault_type, expected_category, params, planned_at)
        SELECT JSON_VALUE(r.campaign_id), CAST(JSON_VALUE(r.seed) AS INT64), CAST(JSON_VALUE(r.position) AS INT64),
               JSON_VALUE(r.fault_type), JSON_VALUE(r.expected_category), PARSE_JSON(JSON_VALUE(r.params)),
               CURRENT_TIMESTAMP()
        FROM UNNEST(JSON_QUERY_ARRAY(PARSE_JSON(@rows))) AS r""",
        [bq.ScalarQueryParameter("rows", "STRING", json.dumps(rows))],
    )


def set_execution(c: Clients, fault_id: str, execution: str) -> None:
    c.query(
        f"""
        UPDATE {_table(c, "fault_ledger")} SET execution_name = @execution
        WHERE fault_id = @fault_id AND DATE(injected_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)""",
        [
            bq.ScalarQueryParameter("execution", "STRING", execution),
            bq.ScalarQueryParameter("fault_id", "STRING", fault_id),
        ],
    )


def stored_plan(c: Clients, campaign_id: str) -> list[dict]:
    job = c.query(
        f"""
        SELECT position, seed, fault_type, expected_category, TO_JSON_STRING(params) AS params
        FROM {_table(c, "campaign_plan")}
        WHERE DATE(planned_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND campaign_id = @campaign
        ORDER BY position""",
        [bq.ScalarQueryParameter("campaign", "STRING", campaign_id)],
    )
    return [
        {
            "position": r["position"],
            "seed": r["seed"],
            "fault_type": r["fault_type"],
            "expected_category": r["expected_category"],
            "params": json.loads(r["params"]),
        }
        for r in job.result()
    ]


def campaign_progress(c: Clients, campaign_id: str) -> dict[int, dict]:
    """For each plan position already started: its fault row and whether its incident has been scored."""
    job = c.query(
        f"""
        SELECT f.plan_position, f.fault_id, f.execution_name, f.reverted_at IS NOT NULL AS reverted,
               EXISTS (
                 SELECT 1 FROM {_table(c, "evaluation_results")} e
                 WHERE DATE(e.evaluated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND e.fault_id = f.fault_id
               ) AS scored
        FROM {_table(c, "fault_ledger")} f
        WHERE DATE(f.injected_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND f.campaign_id = @campaign
          AND f.plan_position IS NOT NULL""",
        [bq.ScalarQueryParameter("campaign", "STRING", campaign_id)],
    )
    return {r["plan_position"]: dict(r.items()) for r in job.result()}
