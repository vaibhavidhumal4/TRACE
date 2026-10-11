"""All BigQuery access for the agent service: one helper that always sets a byte cap, ledger writes that are safe to
repeat, and read-only queries on the target dataset. Nothing here can reach trace_truth, by IAM."""

from __future__ import annotations

import json
import uuid

from google.cloud import bigquery as bq

from api.config import Settings, Target

RECENT = "DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"


class Store:
    def __init__(self, settings: Settings, target: Target, client: bq.Client | None = None) -> None:
        self.settings = settings
        self.target = target
        self.client = client or bq.Client(project=settings.project, location=settings.bq_location)

    def _ledger(self, table: str) -> str:
        return f"`{self.settings.project}.{self.target.ledger_dataset}.{table}`"

    def _target(self, table: str) -> str:
        return f"`{self.settings.project}.{self.target.target_dataset}.{table}`"

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        config = bq.QueryJobConfig(maximum_bytes_billed=self.settings.max_bytes_billed, query_parameters=params or [])
        return [dict(row.items()) for row in self.client.query(sql, job_config=config).result()]

    # ---- incidents and events (trace_ledger) -------------------------------------------------------------------

    def open_incident(self, execution: str, detector: str) -> str:
        """Return the incident for this execution, creating it if there is none (dedupe on execution name)."""
        params = [
            bq.ScalarQueryParameter("incident_id", "STRING", str(uuid.uuid4())),
            bq.ScalarQueryParameter("execution", "STRING", execution),
            bq.ScalarQueryParameter("detector", "STRING", detector),
        ]
        self.query(
            f"""
            INSERT INTO {self._ledger("incidents")}
              (incident_id, detected_at, detector, source_run_id, source_execution, severity, status, dedup_key)
            SELECT @incident_id, CURRENT_TIMESTAMP(), @detector, NULL, @execution, NULL, 'open', @execution
            FROM (SELECT 1)
            WHERE NOT EXISTS (
              SELECT 1 FROM {self._ledger("incidents")}
              WHERE DATE(detected_at) >= {RECENT} AND dedup_key = @execution)""",
            params,
        )
        rows = self.query(
            f"""
            SELECT incident_id FROM {self._ledger("incidents")}
            WHERE DATE(detected_at) >= {RECENT} AND dedup_key = @execution
            ORDER BY detected_at LIMIT 1""",
            params[1:2],
        )
        return rows[0]["incident_id"]

    def incident(self, incident_id: str) -> dict:
        rows = self.query(
            f"""
            SELECT incident_id, detected_at, detector, source_execution, status
            FROM {self._ledger("incidents")}
            WHERE DATE(detected_at) >= {RECENT} AND incident_id = @incident_id LIMIT 1""",
            [bq.ScalarQueryParameter("incident_id", "STRING", incident_id)],
        )
        if not rows:
            raise LookupError(f"no incident {incident_id}")
        return rows[0]

    def events(self, incident_id: str) -> list[dict]:
        """The incident's timeline, oldest first. The first event per stage is the one that counts."""
        rows = self.query(
            f"""
            SELECT stage, agent, created_at, TO_JSON_STRING(payload) AS payload
            FROM {self._ledger("incident_events")}
            WHERE DATE(created_at) >= {RECENT} AND incident_id = @incident_id
            ORDER BY created_at""",
            [bq.ScalarQueryParameter("incident_id", "STRING", incident_id)],
        )
        seen: dict[str, dict] = {}
        for row in rows:
            row["payload"] = json.loads(row["payload"]) if row["payload"] else {}
            seen.setdefault(row["stage"], row)
        return list(seen.values())

    def append_event(self, incident_id: str, stage: str, agent: str, payload: dict) -> None:
        """Append a stage event unless one with the same idempotency key already exists."""
        self.query(
            f"""
            INSERT INTO {self._ledger("incident_events")}
              (event_id, incident_id, stage, agent, created_at, idempotency_key, payload)
            SELECT GENERATE_UUID(), @incident_id, @stage, @agent, CURRENT_TIMESTAMP(), @key, PARSE_JSON(@payload)
            FROM (SELECT 1)
            WHERE NOT EXISTS (
              SELECT 1 FROM {self._ledger("incident_events")}
              WHERE DATE(created_at) >= {RECENT} AND idempotency_key = @key)""",
            [
                bq.ScalarQueryParameter("incident_id", "STRING", incident_id),
                bq.ScalarQueryParameter("stage", "STRING", stage),
                bq.ScalarQueryParameter("agent", "STRING", agent),
                bq.ScalarQueryParameter("key", "STRING", f"{incident_id}:{stage}"),
                bq.ScalarQueryParameter("payload", "STRING", json.dumps(payload, default=str)),
            ],
        )

    def write_verdict(
        self,
        incident_id: str,
        agent_name: str,
        *,
        category: str | None = None,
        hypothesis: str | None = None,
        confidence: float | None = None,
        evidence: object = None,
        proposals: object = None,
        model: str | None = None,
        llm_calls: int = 0,
        latency_ms: int = 0,
    ) -> None:
        """One verdict row per (incident, agent); a repeat call writes nothing."""
        self.query(
            f"""
            INSERT INTO {self._ledger("agent_verdicts")}
              (verdict_id, incident_id, agent_name, created_at, root_cause_category, hypothesis, confidence,
               evidence, proposals, model, llm_calls, latency_ms, memory_enabled)
            SELECT GENERATE_UUID(), @incident_id, @agent_name, CURRENT_TIMESTAMP(), @category, @hypothesis,
                   @confidence, PARSE_JSON(@evidence), PARSE_JSON(@proposals), @model, @llm_calls, @latency_ms, FALSE
            FROM (SELECT 1)
            WHERE NOT EXISTS (
              SELECT 1 FROM {self._ledger("agent_verdicts")}
              WHERE DATE(created_at) >= {RECENT} AND incident_id = @incident_id AND agent_name = @agent_name)""",
            [
                bq.ScalarQueryParameter("incident_id", "STRING", incident_id),
                bq.ScalarQueryParameter("agent_name", "STRING", agent_name),
                bq.ScalarQueryParameter("category", "STRING", category),
                bq.ScalarQueryParameter("hypothesis", "STRING", hypothesis),
                bq.ScalarQueryParameter("confidence", "FLOAT64", confidence),
                bq.ScalarQueryParameter("evidence", "STRING", json.dumps(evidence, default=str)),
                bq.ScalarQueryParameter("proposals", "STRING", json.dumps(proposals, default=str)),
                bq.ScalarQueryParameter("model", "STRING", model),
                bq.ScalarQueryParameter("llm_calls", "INT64", llm_calls),
                bq.ScalarQueryParameter("latency_ms", "INT64", latency_ms),
            ],
        )

    def llm_calls_today(self) -> int:
        rows = self.query(
            f"""
            SELECT COALESCE(SUM(llm_calls), 0) AS calls FROM {self._ledger("agent_verdicts")}
            WHERE DATE(created_at) = CURRENT_DATE()"""
        )
        return int(rows[0]["calls"])

    # ---- read-only tools on the target dataset -----------------------------------------------------------------

    def run_rows(self, execution: str) -> list[dict]:
        return self.query(
            f"""
            SELECT phase, status, ts, states_total, states_ok, rows_inserted, error_type, error_message
            FROM {self._target("pipeline_runs")}
            WHERE DATE(ts) >= {RECENT} AND execution_name = @execution ORDER BY ts""",
            [bq.ScalarQueryParameter("execution", "STRING", execution)],
        )

    def recent_end_rows(self, n: int = 5) -> list[dict]:
        return self.query(
            f"""
            SELECT execution_name, status, ts, states_ok, rows_inserted, error_type
            FROM {self._target("pipeline_runs")}
            WHERE DATE(ts) >= {RECENT} AND phase = 'END' ORDER BY ts DESC LIMIT @n""",
            [bq.ScalarQueryParameter("n", "INT64", n)],
        )

    def freshness(self) -> dict:
        """Newest data hour and newest ingestion time (both naive IST), and hours since the last ingestion."""
        rows = self.query(
            f"""
            SELECT MAX(ts) AS newest_ts, MAX(ingested_at_ist) AS last_ingested_at_ist,
                   DATETIME_DIFF(CURRENT_DATETIME('Asia/Kolkata'), MAX(ingested_at_ist), MINUTE) / 60 AS staleness_hours
            FROM {self._target("aqi_hourly")}
            WHERE DATE(ts) >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 3 DAY)"""
        )
        return rows[0]
