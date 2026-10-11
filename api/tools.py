"""Read-only evidence tools. Logs are read through the restricted `target-only` view, never project-wide."""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from google.cloud import logging_v2, run_v2

from api.config import Settings, Target
from api.store import Store

MAX_MESSAGE = 420
MAX_LOG_LINES = 25
MAX_CHANGES = 8


class Tools:
    def __init__(
        self,
        settings: Settings,
        target: Target,
        store: Store,
        log_client: logging_v2.Client | None = None,
        jobs_client: run_v2.JobsClient | None = None,
        executions_client: run_v2.ExecutionsClient | None = None,
    ) -> None:
        self.settings = settings
        self.target = target
        self.store = store
        self._logs = log_client or logging_v2.Client(project=settings.project)
        self._jobs = jobs_client or run_v2.JobsClient()
        self._executions = executions_client or run_v2.ExecutionsClient()

    def _entries(self, log_filter: str, limit: int) -> list[dict]:
        entries = self._logs.list_entries(
            resource_names=[self.settings.log_view_path],
            filter_=log_filter,
            order_by=logging_v2.ASCENDING,
            page_size=min(limit, 500),
            max_results=limit,
        )
        return [entry.to_api_repr() for entry in entries]

    def get_pipeline_runs(self, execution: str) -> dict:
        return {"this_execution": self.store.run_rows(execution), "recent_finished_runs": self.store.recent_end_rows(5)}

    def get_run_logs(self, execution: str) -> list[dict]:
        """The execution's log lines, with repeats of the same line collapsed into one entry with a count."""
        raw = self._entries(f'labels."run.googleapis.com/execution_name"="{execution}"', 400)
        collapsed: OrderedDict[tuple, dict] = OrderedDict()
        for entry in raw:
            payload = entry.get("jsonPayload")
            if payload:
                line = {
                    "severity": entry.get("severity"),
                    "phase": payload.get("phase"),
                    "error_class": payload.get("error_class"),
                    "message": str(payload.get("message", ""))[:MAX_MESSAGE],
                }
                if payload.get("config"):
                    line["config"] = payload["config"]
                key = (line["severity"], line["phase"], line["error_class"], line["message"][:80])
            else:
                text = entry.get("textPayload") or json.dumps(entry.get("protoPayload", {}).get("status", {}))
                line = {
                    "severity": entry.get("severity"),
                    "source": entry.get("logName", "").rsplit("%2F", 1)[-1],
                    "message": str(text)[:MAX_MESSAGE],
                }
                key = (line["severity"], line["source"], line["message"][:80])
            if key in collapsed:
                collapsed[key]["count"] += 1
            else:
                collapsed[key] = {**line, "count": 1, "first_seen": entry.get("timestamp")}
        return list(collapsed.values())[:MAX_LOG_LINES]

    def get_recent_changes(self, hours: float, before: datetime | None = None) -> list[dict]:
        """Admin Activity audit entries for the target job, dataset and service account: who changed what, when.

        Covers both configuration changes and permission changes.
        """
        end = before or datetime.now(UTC)
        start = end - timedelta(hours=hours)
        raw = self._entries(
            f'logName:"cloudaudit.googleapis.com%2Factivity" AND timestamp>="{start.isoformat()}" '
            f'AND timestamp<="{end.isoformat()}"',
            200,
        )
        changes = []
        for entry in raw:
            proto = entry.get("protoPayload", {})
            method = proto.get("methodName", "")
            if method.endswith(("RunJob", "InsertJob")) and "tableChange" not in json.dumps(proto.get("metadata", {})):
                continue  # running the job or an ordinary query is not a change
            detail = proto.get("metadata") or proto.get("request") or proto.get("serviceData") or {}
            changes.append(
                {
                    "time": entry.get("timestamp"),
                    "method": method,
                    "resource": proto.get("resourceName"),
                    "by": proto.get("authenticationInfo", {}).get("principalEmail"),
                    "detail": json.dumps(detail, default=str)[:500],
                }
            )
        return changes[-MAX_CHANGES:]

    def get_job_config(self) -> dict:
        """Current settings of the target job (Cloud Run Admin API, read-only)."""
        name = f"projects/{self.settings.project}/locations/{self.settings.region}/jobs/{self.target.job}"
        job = self._jobs.get_job(name=name)
        task = job.template.template
        container = task.containers[0]
        return {
            "job": self.target.job,
            "task_timeout_seconds": task.timeout.seconds,
            "max_retries": task.max_retries,
            "service_account": task.service_account,
            "env": {e.name: e.value for e in container.env},
            "cpu": container.resources.limits.get("cpu"),
            "memory": container.resources.limits.get("memory"),
            "last_modifier": job.last_modifier,
            "update_time": job.update_time.isoformat() if job.update_time else None,
        }

    def get_execution_times(self, execution: str) -> dict:
        """When the execution started and finished (Cloud Run Admin API, read-only)."""
        name = (
            f"projects/{self.settings.project}/locations/{self.settings.region}"
            f"/jobs/{self.target.job}/executions/{execution}"
        )
        ex = self._executions.get_execution(name=name)
        return {
            "execution_started_at": ex.start_time.isoformat() if ex.start_time else None,
            "execution_completed_at": ex.completion_time.isoformat() if ex.completion_time else None,
            "succeeded_count": ex.succeeded_count,
            "failed_count": ex.failed_count,
        }

    def get_data_freshness(self) -> dict:
        return self.store.freshness()
