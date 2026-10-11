"""Score incidents against trace_truth and write evaluation_results. Runs as trace-evaluator.

    GCP_PROJECT=... TRACE_ENV=dev EVAL_IMPERSONATE=trace-evaluator@... python -m evaluator.evaluate [--campaign ID]

Every incident whose failed execution started while a fault was active is scored exactly once. An incident with no
root-cause verdict is scored as wrong, never skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import google.auth
from google.auth import impersonated_credentials
from google.cloud import bigquery as bq

from evaluator.scoring import accuracy, attribute, confusion_matrix, score

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
RECENT = "DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"


class Evaluator:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = dict(os.environ) if env is None else env
        self.project = env["GCP_PROJECT"]
        suffix = "_dev" if env.get("TRACE_ENV", "dev") == "dev" else ""
        self.ledger = f"{self.project}.trace_ledger{suffix}"
        self.truth = f"{self.project}.trace_truth{suffix}"
        self.max_bytes = int(env.get("BQ_MAX_BYTES_BILLED", str(100 * 1024 * 1024)))
        credentials, _ = google.auth.default(scopes=SCOPES)
        if env.get("EVAL_IMPERSONATE"):
            credentials = impersonated_credentials.Credentials(
                source_credentials=credentials, target_principal=env["EVAL_IMPERSONATE"], target_scopes=SCOPES
            )
        self.bq = bq.Client(
            project=self.project, location=env.get("BQ_LOCATION", "us-central1"), credentials=credentials
        )

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        config = bq.QueryJobConfig(maximum_bytes_billed=self.max_bytes, query_parameters=params or [])
        return [dict(r.items()) for r in self.bq.query(sql, job_config=config).result()]

    def faults(self, campaign_id: str | None) -> list[dict]:
        where = "AND campaign_id = @campaign" if campaign_id else ""
        return self.query(
            f"""
            SELECT fault_id, fault_type, expected_category, injected_at, reverted_at, campaign_id, split
            FROM `{self.truth}.fault_ledger`
            WHERE DATE(injected_at) >= {RECENT} {where} ORDER BY injected_at""",
            [bq.ScalarQueryParameter("campaign", "STRING", campaign_id)] if campaign_id else [],
        )

    def incidents(self) -> list[dict]:
        """Each incident with the time its failed execution started (recorded by the campaign runner at open),
        its root-cause verdict if any, and when that verdict was written."""
        return self.query(
            f"""
            SELECT i.incident_id, i.source_execution, i.detected_at,
                   COALESCE(TIMESTAMP(JSON_VALUE(s.payload.execution_started_at)), i.detected_at) AS started_at,
                   v.root_cause_category AS predicted_category, v.confidence, v.created_at AS verdict_at
            FROM `{self.ledger}.incidents` i
            LEFT JOIN (
              SELECT incident_id, ANY_VALUE(payload) AS payload FROM `{self.ledger}.incident_events`
              WHERE DATE(created_at) >= {RECENT} AND stage = 'execution_window' GROUP BY incident_id
            ) s USING (incident_id)
            LEFT JOIN (
              SELECT incident_id, ANY_VALUE(root_cause_category HAVING MIN created_at) AS root_cause_category,
                     ANY_VALUE(confidence HAVING MIN created_at) AS confidence, MIN(created_at) AS created_at
              FROM `{self.ledger}.agent_verdicts`
              WHERE DATE(created_at) >= {RECENT} AND agent_name = 'root_cause' GROUP BY incident_id
            ) v USING (incident_id)
            WHERE DATE(i.detected_at) >= {RECENT}"""
        )

    def already_scored(self) -> set[str]:
        rows = self.query(
            f"SELECT incident_id FROM `{self.truth}.evaluation_results` WHERE DATE(evaluated_at) >= {RECENT}"
        )
        return {r["incident_id"] for r in rows}

    def write_result(self, row: dict) -> None:
        self.query(
            f"""
            INSERT INTO `{self.truth}.evaluation_results`
              (eval_id, incident_id, fault_id, predicted_category, true_category, is_correct, confidence,
               time_to_diagnosis_sec, split, evaluated_at)
            SELECT GENERATE_UUID(), @incident_id, @fault_id, @predicted, @true_category, @is_correct, @confidence,
                   @ttd, @split, CURRENT_TIMESTAMP()
            FROM (SELECT 1)
            WHERE NOT EXISTS (
              SELECT 1 FROM `{self.truth}.evaluation_results`
              WHERE DATE(evaluated_at) >= {RECENT} AND incident_id = @incident_id)""",
            [
                bq.ScalarQueryParameter("incident_id", "STRING", row["incident_id"]),
                bq.ScalarQueryParameter("fault_id", "STRING", row["fault_id"]),
                bq.ScalarQueryParameter("predicted", "STRING", row["predicted_category"]),
                bq.ScalarQueryParameter("true_category", "STRING", row["true_category"]),
                bq.ScalarQueryParameter("is_correct", "BOOL", row["is_correct"]),
                bq.ScalarQueryParameter("confidence", "FLOAT64", row["confidence"]),
                bq.ScalarQueryParameter("ttd", "INT64", row["time_to_diagnosis_sec"]),
                bq.ScalarQueryParameter("split", "STRING", row["split"]),
            ],
        )


def evaluate(ev: Evaluator, campaign_id: str | None) -> list[dict]:
    """Score every attributable incident not yet scored. Returns all rows scored in this call."""
    faults = ev.faults(campaign_id)
    done = ev.already_scored()
    scored = []
    for incident in ev.incidents():
        if incident["incident_id"] in done:
            continue
        fault = attribute(incident["started_at"], faults)
        if fault is None:
            continue
        ttd = None
        if incident["verdict_at"] is not None:
            ttd = int((incident["verdict_at"] - incident["started_at"]).total_seconds())
        row = {
            "incident_id": incident["incident_id"],
            "fault_id": fault["fault_id"],
            "predicted_category": incident["predicted_category"],
            "true_category": fault["expected_category"],
            "is_correct": score(incident["predicted_category"], fault["expected_category"]),
            "confidence": incident["confidence"],
            "time_to_diagnosis_sec": ttd,
            "split": fault["split"],
        }
        ev.write_result(row)
        scored.append(row)
    return scored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", help="only attribute incidents to faults of this campaign")
    args = parser.parse_args(argv)
    scored = evaluate(Evaluator(), args.campaign)
    print(
        json.dumps(
            {
                "scored_now": len(scored),
                "accuracy_of_these": accuracy(scored),
                "confusion_matrix": confusion_matrix(scored),
            },
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
