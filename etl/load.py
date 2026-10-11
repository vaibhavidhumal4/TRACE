"""BigQuery writes: the run ledger, and the staged idempotent insert into aqi_hourly."""

from __future__ import annotations

import json
import uuid
import warnings
from datetime import UTC, datetime, timedelta

import pandas as pd
from google.cloud import bigquery as bq

from etl.config import Config
from etl.logs import log

# The client warns on stderr about a future pandas-gbq requirement. It is not a JSON log line and says nothing
# about the run.
warnings.filterwarnings("ignore", category=FutureWarning, module="google.cloud.bigquery")

AQI_SCHEMA = [
    bq.SchemaField("state", "STRING"),
    bq.SchemaField("ts", "DATETIME"),
    bq.SchemaField("pm25", "FLOAT"),
    bq.SchemaField("pm10", "FLOAT"),
    bq.SchemaField("no2", "FLOAT"),
    bq.SchemaField("so2", "FLOAT"),
    bq.SchemaField("co", "FLOAT"),
    bq.SchemaField("o3", "FLOAT"),
    bq.SchemaField("us_aqi", "FLOAT"),
    bq.SchemaField("aqi_category", "STRING"),
    bq.SchemaField("ingested_at_ist", "DATETIME"),
]


def make_client(cfg: Config) -> bq.Client:
    """Application Default Credentials: the Cloud Run job's own service account."""
    return bq.Client(project=cfg.project, location=cfg.bq_location)


def _query(client: bq.Client, cfg: Config, sql: str, params: list | None = None) -> bq.QueryJob:
    job_config = bq.QueryJobConfig(maximum_bytes_billed=cfg.max_bytes_billed, query_parameters=params or [])
    job = client.query(sql, job_config=job_config)
    job.result()
    return job


def write_run_row(
    client: bq.Client,
    cfg: Config,
    *,
    run_id: str,
    phase: str,
    status: str,
    states_total: int | None = None,
    states_ok: int | None = None,
    rows_inserted: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    """Append one row to pipeline_runs (START with RUNNING, END with the final status)."""
    sql = f"""
        INSERT INTO `{cfg.project}.{cfg.dataset}.{cfg.runs_table}`
          (run_id, execution_name, phase, status, ts, states_total, states_ok, rows_inserted,
           error_type, error_message, config)
        VALUES (@run_id, @execution_name, @phase, @status, CURRENT_TIMESTAMP(), @states_total, @states_ok,
                @rows_inserted, @error_type, @error_message, PARSE_JSON(@config))"""
    params = [
        bq.ScalarQueryParameter("run_id", "STRING", run_id),
        bq.ScalarQueryParameter("execution_name", "STRING", cfg.execution_name),
        bq.ScalarQueryParameter("phase", "STRING", phase),
        bq.ScalarQueryParameter("status", "STRING", status),
        bq.ScalarQueryParameter("states_total", "INT64", states_total),
        bq.ScalarQueryParameter("states_ok", "INT64", states_ok),
        bq.ScalarQueryParameter("rows_inserted", "INT64", rows_inserted),
        bq.ScalarQueryParameter("error_type", "STRING", error_type),
        bq.ScalarQueryParameter("error_message", "STRING", (error_message or "")[:2000] or None),
        bq.ScalarQueryParameter("config", "STRING", json.dumps(cfg.public_dict())),
    ]
    _query(client, cfg, sql, params)


def load(client: bq.Client, cfg: Config, df: pd.DataFrame, run_id: str) -> int:
    """Stage this run's rows, insert only the (state, ts) pairs not already present, drop the staging table.

    Returns the number of rows inserted. The tables themselves come from sql/; nothing is created here except
    the staging table.
    """
    main = f"{cfg.project}.{cfg.dataset}.{cfg.table}"
    staging = f"{main}_staging_{uuid.UUID(run_id).hex[:8]}"
    try:
        load_config = bq.LoadJobConfig(schema=AQI_SCHEMA, write_disposition="WRITE_TRUNCATE")
        client.load_table_from_dataframe(df[[f.name for f in AQI_SCHEMA]], staging, job_config=load_config).result()
        # If this run is killed before the cleanup below, the staging table removes itself.
        table = client.get_table(staging)
        table.expires = datetime.now(UTC) + timedelta(hours=1)
        client.update_table(table, ["expires"])
        log("INFO", f"staged {len(df)} rows", phase="load")
        # SELECT s.* is deliberate: the insert depends on the target table's column list matching the staging table's.
        job = _query(
            client,
            cfg,
            f"""
            INSERT INTO `{main}`
            SELECT s.* FROM `{staging}` s
            WHERE NOT EXISTS (
              SELECT 1 FROM `{main}` t
              WHERE DATE(t.ts) >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 2 DAY)
                AND t.state = s.state AND t.ts = s.ts)""",
        )
        inserted = int(job.num_dml_affected_rows or 0)
        log(
            "INFO",
            f"inserted {inserted} new rows",
            phase="load",
            bytes_billed=int(job.total_bytes_billed or 0),
        )
        return inserted
    finally:
        client.delete_table(staging, not_found_ok=True)
