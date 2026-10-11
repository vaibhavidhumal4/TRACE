"""Shared steps for one injected incident: record, inject, run the target job, wait, restore."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from google.cloud import bigquery as bq
from google.cloud import run_v2

from injector import ledger
from injector.actions import Clients, build_revert_plan, inject, revert
from injector.catalog import CATALOG

# How long after injection the scheduled reverter restores state by itself if nothing else has.
DEFAULT_TTL = timedelta(minutes=30)
# Dataset permission changes take a little while to reach BigQuery's enforcement path.
SETTLE_SECONDS = {"iam_revoked": 60}


def snapshot(c: Clients) -> dict:
    """The parts of the target that faults touch. Used to prove that a revert restored everything."""
    s = c.settings
    dataset = c.bq.get_dataset(f"{s.project}.{s.target_dataset}")
    etl_email = s.etl_member.split(":", 1)[1]
    table = c.bq.get_table(f"{s.project}.{s.target_dataset}.aqi_hourly")
    job = c.jobs.get_job(name=s.target_job_path)
    container = job.template.template.containers[0]
    return {
        "etl_dataset_roles": sorted(e.role for e in dataset.access_entries if e.entity_id == etl_email),
        "aqi_hourly_columns": [f.name for f in table.schema],
        "task_timeout_seconds": job.template.template.timeout.seconds,
        "job_env": {e.name: e.value for e in container.env},
    }


def start_fault(c: Clients, fault_type: str, params: dict, *, campaign_id: str, seed: int | None) -> str:
    """Record the fault and its revert plan, then inject. Returns the fault_id."""
    plan = build_revert_plan(c, fault_type, params)
    fault_id = ledger.record_fault(
        c,
        fault_type=fault_type,
        expected_category=CATALOG[fault_type].expected_category,
        params=params,
        revert_plan=plan,
        revert_at=datetime.now(UTC) + DEFAULT_TTL,
        seed=seed,
        campaign_id=campaign_id,
    )
    inject(c, fault_type, params)
    time.sleep(SETTLE_SECONDS.get(fault_type, 5))
    return fault_id


def end_fault(c: Clients, fault_id: str) -> None:
    """Restore state now and mark the ledger row. The scheduled reverter covers the case where this never runs."""
    faults = ledger.open_faults(c, due_only=False, fault_id=fault_id)
    for fault in faults:
        revert(c, fault["revert_plan"])
        ledger.mark_reverted(c, fault["fault_id"])


def execute_and_wait(c: Clients, timeout_s: int = 1500) -> dict:
    """Start one execution of the target job and wait for it to finish, whatever the outcome."""
    operation = c.jobs.run_job(request=run_v2.RunJobRequest(name=c.settings.target_job_path))
    name = operation.metadata.name
    deadline = time.monotonic() + timeout_s
    while True:
        execution = c.executions.get_execution(name=name)
        if execution.completion_time or execution.succeeded_count or execution.failed_count:
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"execution {name} did not finish within {timeout_s} s")
        time.sleep(5)
    return {
        "execution": name.rsplit("/", 1)[1],
        "succeeded": execution.succeeded_count > 0 and execution.failed_count == 0,
        "started_at": execution.start_time.isoformat() if execution.start_time else None,
        "completed_at": execution.completion_time.isoformat() if execution.completion_time else None,
    }


def run_rows(c: Clients, execution: str) -> list[dict]:
    """The pipeline_runs rows the execution wrote, oldest first."""
    s = c.settings
    job = c.query(
        f"""
        SELECT phase, status, states_ok, states_total, error_type, error_message
        FROM `{s.project}.{s.target_dataset}.pipeline_runs`
        WHERE DATE(ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AND execution_name = @execution
        ORDER BY ts""",
        [bq.ScalarQueryParameter("execution", "STRING", execution)],
    )
    return [dict(r.items()) for r in job.result()]
