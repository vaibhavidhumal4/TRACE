"""Injecting and reverting each fault.

Order of operations for every fault: build the revert plan from the current state (read-only), record it in the
ledger, and only then inject. A revert plan is self-contained, so the reverter can apply it without the injector.
"""

from __future__ import annotations

import re
import time

from google.cloud import bigquery as bq
from google.cloud import run_v2
from google.protobuf import duration_pb2

from injector.config import Settings

URL_ENV = "OPEN_METEO_URL"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class Clients:
    def __init__(self, settings: Settings) -> None:
        creds = settings.credentials()
        self.settings = settings
        self.bq = bq.Client(project=settings.project, location=settings.bq_location, credentials=creds)
        self.jobs = run_v2.JobsClient(credentials=creds)
        self.executions = run_v2.ExecutionsClient(credentials=creds)

    def query(self, sql: str, params: list | None = None) -> bq.QueryJob:
        config = bq.QueryJobConfig(maximum_bytes_billed=self.settings.max_bytes_billed, query_parameters=params or [])
        job = self.bq.query(sql, job_config=config)
        job.result()
        return job


def _check_identifier(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"not a safe SQL identifier: {name!r}")
    return name


def _aqi_table(s: Settings) -> str:
    return f"`{s.project}.{s.target_dataset}.aqi_hourly`"


def _job_env(job: run_v2.Job) -> dict[str, str]:
    return {e.name: e.value for e in job.template.template.containers[0].env}


def build_revert_plan(c: Clients, fault_type: str, params: dict) -> dict:
    """Read the current state and describe how to restore it. Changes nothing."""
    s = c.settings
    if fault_type == "iam_revoked":
        return {
            "kind": "grant_dataset_role",
            "dataset": s.target_dataset,
            "role": params["role"],
            "member": s.etl_member,
        }
    if fault_type == "bq_extra_column":
        return {"kind": "drop_column", "dataset": s.target_dataset, "table": "aqi_hourly", "column": params["column"]}
    job = c.jobs.get_job(name=s.target_job_path)
    if fault_type == "task_timeout_kill":
        return {"kind": "set_task_timeout", "job": s.target_job_path, "seconds": job.template.template.timeout.seconds}
    if fault_type == "bad_endpoint_url":
        return {"kind": "set_job_env", "job": s.target_job_path, "name": URL_ENV, "value": _job_env(job).get(URL_ENV)}
    raise ValueError(f"unknown fault type: {fault_type}")


def inject(c: Clients, fault_type: str, params: dict) -> None:
    s = c.settings
    if fault_type == "iam_revoked":
        c.query(f'REVOKE `{params["role"]}` ON SCHEMA `{s.project}.{s.target_dataset}` FROM "{s.etl_member}"')
    elif fault_type == "bq_extra_column":
        column = _check_identifier(params["column"])
        column_type = _check_identifier(params["type"])
        c.query(f"ALTER TABLE {_aqi_table(s)} ADD COLUMN IF NOT EXISTS {column} {column_type}")
    elif fault_type == "task_timeout_kill":
        _set_task_timeout(c, s.target_job_path, int(params["timeout_seconds"]))
    elif fault_type == "bad_endpoint_url":
        _set_job_env(c, s.target_job_path, URL_ENV, params["url"])
    else:
        raise ValueError(f"unknown fault type: {fault_type}")


def revert(c: Clients, plan: dict) -> None:
    """Apply a revert plan. Every branch is safe to apply twice."""
    s = c.settings
    kind = plan["kind"]
    if kind == "grant_dataset_role":
        c.query(f'GRANT `{plan["role"]}` ON SCHEMA `{s.project}.{plan["dataset"]}` TO "{plan["member"]}"')
    elif kind == "drop_column":
        column = _check_identifier(plan["column"])
        table = f"`{s.project}.{plan['dataset']}.{_check_identifier(plan['table'])}`"
        c.query(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
    elif kind == "set_task_timeout":
        _set_task_timeout(c, plan["job"], int(plan["seconds"]))
    elif kind == "set_job_env":
        _set_job_env(c, plan["job"], plan["name"], plan["value"])
    else:
        raise ValueError(f"unknown revert plan kind: {kind}")


def _set_task_timeout(c: Clients, job_path: str, seconds: int) -> None:
    job = c.jobs.get_job(name=job_path)
    job.template.template.timeout = duration_pb2.Duration(seconds=seconds)
    _update_job(c, job)


def _set_job_env(c: Clients, job_path: str, name: str, value: str | None) -> None:
    """Set an environment variable on the job, or remove it when value is None."""
    job = c.jobs.get_job(name=job_path)
    container = job.template.template.containers[0]
    kept = [e for e in container.env if e.name != name]
    if value is not None:
        kept.append(run_v2.EnvVar(name=name, value=value))
    del container.env[:]
    container.env.extend(kept)
    _update_job(c, job)


def _update_job(c: Clients, job: run_v2.Job, timeout_s: int = 180) -> None:
    """Send the update and wait until the job has settled.

    The wait polls the job itself. Polling the returned operation would need run.operations.get on the project,
    which the maintenance identity deliberately does not have (its rights are on the target jobs only).
    """
    c.jobs.update_job(job=job)
    deadline = time.monotonic() + timeout_s
    while True:
        time.sleep(3)
        current = c.jobs.get_job(name=job.name)
        if not current.reconciling:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job.name} was still reconciling after {timeout_s} s")
