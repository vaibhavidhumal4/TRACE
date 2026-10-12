"""All BigQuery access for the dashboard: one read-only query over the three trace_results views, always with a
byte cap. There is no write path in this service, and its identity can read trace_results only."""

from __future__ import annotations

import json

from google.cloud import bigquery as bq

from dashboard.config import Settings

# JSON kept as text in the views; decoded here so the page gets structured values.
_JSON_TEXT_COLUMNS = ("injected_params", "triage_reasons", "remediation_proposals")


class ResultsStore:
    def __init__(self, settings: Settings, client: bq.Client | None = None) -> None:
        self.settings = settings
        self.client = client or bq.Client(project=settings.project, location=settings.bq_location)

    def _view(self, name: str) -> str:
        return f"`{self.settings.project}.{self.settings.results_dataset}.{name}`"

    def snapshot(self, campaign: str | None) -> dict:
        """Everything the page shows for one campaign, in ONE query job: the three views share their underlying
        tables, so one job bills them once. With no campaign given, the one that failed most recently is used."""
        sql = f"""
            WITH s AS (SELECT * FROM {self._view("campaign_summary")}),
            pick AS (
              SELECT COALESCE(
                @campaign, (SELECT campaign_id FROM s ORDER BY last_failure DESC, campaign_id LIMIT 1)) AS campaign_id
            )
            SELECT
              (SELECT campaign_id FROM pick) AS campaign_id,
              ARRAY(SELECT TO_JSON_STRING(s) FROM s ORDER BY s.last_failure DESC, s.campaign_id) AS campaigns,
              ARRAY(SELECT TO_JSON_STRING(r) FROM {self._view("incident_results")} AS r
                    WHERE r.campaign_id = (SELECT campaign_id FROM pick) ORDER BY r.position) AS incidents,
              ARRAY(SELECT TO_JSON_STRING(m) FROM {self._view("confusion_matrix")} AS m
                    WHERE m.campaign_id = (SELECT campaign_id FROM pick)) AS matrix"""
        config = bq.QueryJobConfig(
            maximum_bytes_billed=self.settings.max_bytes_billed,
            query_parameters=[bq.ScalarQueryParameter("campaign", "STRING", campaign)],
        )
        job = self.client.query(sql, job_config=config)
        row = next(iter(job.result()))
        incidents = [json.loads(text) for text in row["incidents"]]
        for incident in incidents:
            for column in _JSON_TEXT_COLUMNS:
                if isinstance(incident.get(column), str):
                    incident[column] = json.loads(incident[column])
        return {
            "campaign_id": row["campaign_id"],
            "campaigns": [json.loads(text) for text in row["campaigns"]],
            "incidents": incidents,
            "matrix": [json.loads(text) for text in row["matrix"]],
            "bytes_billed": job.total_bytes_billed,
        }
