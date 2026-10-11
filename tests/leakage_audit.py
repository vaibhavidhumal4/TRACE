# ruff: noqa: E501
"""Leakage and write-denial audit (TC-20, TC-32, TC-33), run with the agent identity's own credentials.

    GCP_PROJECT=... AUDIT_IMPERSONATE=trace-agents@<project>.iam.gserviceaccount.com python tests/leakage_audit.py

Every check prints PASS or FAIL. Exit code 1 if any check fails.
"""

from __future__ import annotations

import json
import os
import sys

import google.auth
from google.api_core import exceptions as gexc
from google.auth import impersonated_credentials
from google.cloud import bigquery as bq
from google.cloud import logging_v2, run_v2

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
P = os.environ["GCP_PROJECT"]
REGION = os.environ.get("REGION", "us-central1")
VIEW = f"projects/{P}/locations/{REGION}/buckets/trace-target-logs/views/target-only"
# Words that must never appear in anything the agents can read.
FORBIDDEN = [
    "iam_revoked",
    "bq_extra_column",
    "task_timeout_kill",
    "bad_endpoint_url",
    "fault",
    "inject",
    "chaos",
    "trace_truth",
    "trace_results",
    "campaign",
    "signature-check",
    "deadman",
    "expected_category",
]
DENIED = (gexc.Forbidden, gexc.PermissionDenied)

source, _ = google.auth.default(scopes=SCOPES)
creds = impersonated_credentials.Credentials(
    source_credentials=source, target_principal=os.environ["AUDIT_IMPERSONATE"], target_scopes=SCOPES
)
client = bq.Client(project=P, location="us-central1", credentials=creds)
jobs = run_v2.JobsClient(credentials=creds)
services = run_v2.ServicesClient(credentials=creds)
logs = logging_v2.Client(project=P, credentials=creds)
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name} | {detail[:150]}", flush=True)


def expect_denied(name: str, fn) -> None:
    try:
        out = fn()
        record(name, False, f"NOT denied: {str(out)[:100]}")
    except DENIED as exc:
        record(name, True, f"{type(exc).__name__} {exc.code}")
    except Exception as exc:
        record(name, False, f"unexpected {type(exc).__name__}: {exc}")


def expect_allowed(name: str, fn) -> None:
    try:
        record(name, True, f"allowed: {str(fn())[:100]}")
    except Exception as exc:
        record(name, False, f"{type(exc).__name__}: {exc}")


def q(sql: str):
    config = bq.QueryJobConfig(maximum_bytes_billed=100 * 1024 * 1024)
    return [dict(r.items()) for r in client.query(sql, job_config=config).result()]


def entries(resource: str, log_filter: str, limit: int = 1000) -> list[dict]:
    pager = logs.list_entries(
        resource_names=[resource], filter_=log_filter, max_results=limit, page_size=min(limit, 1000)
    )
    return [e.to_api_repr() for e in pager]


def scan(name: str, text: str) -> None:
    lowered = text.lower()
    hits = sorted({w for w in FORBIDDEN if w in lowered})
    record(name, not hits, f"forbidden words found: {hits}" if hits else f"clean ({len(text)} chars scanned)")


# ---- TC-32: ground truth and evaluation results are unreadable -------------------------------------------------
for dataset in ("trace_truth", "trace_truth_dev", "trace_results", "trace_results_dev"):
    expect_denied(f"TC-32 get dataset {dataset}", lambda d=dataset: client.get_dataset(f"{P}.{d}"))
    expect_denied(f"TC-32 list tables {dataset}", lambda d=dataset: list(client.list_tables(f"{P}.{d}")))
for dataset in ("trace_truth", "trace_truth_dev"):
    for table in ("fault_ledger", "evaluation_results", "campaign_plan"):
        expect_denied(
            f"TC-32 query {dataset}.{table}", lambda d=dataset, t=table: q(f"SELECT * FROM `{P}.{d}.{t}` LIMIT 1")
        )
        expect_denied(f"TC-32 get table {dataset}.{table}", lambda d=dataset, t=table: client.get_table(f"{P}.{d}.{t}"))

# ---- TC-32: the maintenance tooling's logs are unreadable ------------------------------------------------------
# A project-scoped read is not refused outright: Cloud Logging answers it from the views the caller may access. So the
# check is on content: nothing from the maintenance tooling, the truth datasets or any non-target resource comes back.
ALLOWED_JOBS = {"trace-target-etl", "trace-target-etl-dev"}
ALLOWED_DATASETS = {"trace_target", "trace_target_dev"}


def outside_target(entry: dict) -> bool:
    resource = entry.get("resource", {})
    labels = resource.get("labels", {})
    kind = resource.get("type")
    if kind == "cloud_run_job":
        return labels.get("job_name") not in ALLOWED_JOBS
    if kind == "bigquery_dataset":
        return labels.get("dataset_id") not in ALLOWED_DATASETS
    if kind == "service_account":
        return not labels.get("email_id", "").startswith("trace-etl@")
    return True


for label, log_filter in [
    ("maintenance-runner logs", 'resource.labels.job_name="trace-maintenance-runner"'),
    (
        "truth and results dataset audit entries",
        'resource.labels.dataset_id=("trace_truth" OR "trace_truth_dev" OR "trace_results" OR "trace_results_dev")',
    ),
    ("trace-api service logs", 'resource.type="cloud_run_revision"'),
]:
    try:
        found = entries(f"projects/{P}", log_filter, 5)
        record(f"TC-32 project-scoped read: {label}", not found, f"{len(found)} entries")
    except DENIED as exc:
        record(f"TC-32 project-scoped read: {label}", True, f"{type(exc).__name__} {exc.code}")
try:
    everything = entries(f"projects/{P}", 'timestamp>="2026-10-11T00:00:00Z"', 3000)
    strays = [e for e in everything if outside_target(e)]
    record(
        "TC-32 project-scoped read returns only target entries",
        not strays,
        f"{len(everything)} entries, {len(strays)} outside the target job, dataset and service account",
    )
except DENIED as exc:
    record("TC-32 project-scoped read returns only target entries", True, f"{type(exc).__name__} {exc.code}")
expect_denied(
    "TC-32 _Default bucket log read",
    lambda: entries(
        f"projects/{P}/locations/global/buckets/_Default/views/_AllLogs", 'resource.type="cloud_run_job"', 5
    ),
)
try:
    leaked = entries(VIEW, 'resource.labels.job_name="trace-maintenance-runner"', 5)
    record("TC-32 maintenance-runner logs in the restricted view", not leaked, f"{len(leaked)} entries")
    truth_audit = entries(VIEW, 'resource.labels.dataset_id=("trace_truth" OR "trace_truth_dev")', 5)
    record("TC-32 truth-dataset audit entries in the restricted view", not truth_audit, f"{len(truth_audit)} entries")
except Exception as exc:
    record("TC-32 restricted view readable", False, f"{type(exc).__name__}: {exc}")

# ---- TC-20: the agent identity cannot change the target system -------------------------------------------------
target = f"{P}.trace_target_dev"
expect_denied(
    "TC-20 INSERT into target table",
    lambda: q(
        f"INSERT INTO `{target}.pipeline_runs` (run_id, phase, status, ts) VALUES ('audit', 'START', 'RUNNING', CURRENT_TIMESTAMP())"
    ),
)
expect_denied(
    "TC-20 ALTER target table", lambda: q(f"ALTER TABLE `{target}.aqi_hourly` SET OPTIONS (description='audit')")
)
expect_denied(
    "TC-20 DELETE from target table", lambda: q(f"DELETE FROM `{target}.pipeline_runs` WHERE run_id = 'audit'")
)
expect_denied(
    "TC-20 grant on target dataset",
    lambda: q(
        f'GRANT `roles/bigquery.dataViewer` ON SCHEMA `{target}` TO "serviceAccount:trace-agents@{P}.iam.gserviceaccount.com"'
    ),
)
job_path = f"projects/{P}/locations/{REGION}/jobs/trace-target-etl-dev"


def update_job():
    job = jobs.get_job(name=job_path)
    job.template.template.max_retries = 0
    return jobs.update_job(job=job)


expect_denied("TC-20 update target job", update_job)
expect_denied("TC-20 run target job", lambda: jobs.run_job(name=job_path))

# ---- positive controls: the allowed signals are visible --------------------------------------------------------
expect_allowed(
    "control: read target run ledger",
    lambda: q(
        f"SELECT COUNT(*) AS n FROM `{target}.pipeline_runs` WHERE DATE(ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)"
    ),
)
expect_allowed(
    "control: read restricted log view", lambda: f"{len(entries(VIEW, 'resource.type="cloud_run_job"', 5))} entries"
)

# ---- TC-33: nothing readable says what it is for ---------------------------------------------------------------
config_text = []
for job in jobs.list_jobs(parent=f"projects/{P}/locations/{REGION}"):
    c0 = job.template.template.containers[0]
    config_text.append(
        json.dumps(
            {
                "name": job.name,
                "labels": dict(job.labels),
                "annotations": dict(job.annotations),
                "image": c0.image.split("@")[0].split(":")[0],
                "env": {e.name: e.value for e in c0.env},
                "args": list(c0.args),
                "command": list(c0.command),
                "sa": job.template.template.service_account,
            }
        )
    )
for svc in services.list_services(parent=f"projects/{P}/locations/{REGION}"):
    c0 = svc.template.containers[0]
    config_text.append(
        json.dumps(
            {
                "name": svc.name,
                "labels": dict(svc.labels),
                "annotations": dict(svc.annotations),
                "description": svc.description,
                "image": c0.image.split("@")[0].split(":")[0],
                "env": {e.name: e.value for e in c0.env},
                "sa": svc.template.service_account,
            }
        )
    )
scan(f"TC-33 Cloud Run config visible through run.viewer ({len(config_text)} resources)", "\n".join(config_text))

view_entries = entries(VIEW, 'timestamp>="2026-10-11T00:00:00Z"', 3000)
scan(
    f"TC-33 every entry in the restricted log view ({len(view_entries)} entries)", json.dumps(view_entries, default=str)
)

ledger_rows = q(
    f"""SELECT TO_JSON_STRING(t) AS row FROM `{P}.trace_ledger_dev.incident_events` t
        WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)"""
)
scan(
    f"TC-33 everything stored in the dev incident ledger ({len(ledger_rows)} events, includes all evidence shown to the LLM)",
    "\n".join(r["row"] for r in ledger_rows),
)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
