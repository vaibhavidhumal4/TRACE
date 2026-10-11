"""One campaign: a fixed, seeded list of injections, each run end to end and scored. Resumable.

Per incident, each step runs under a different identity and this script never holds two at once in one client:
  inject / run the job / revert   as ops-automation        (short-lived impersonated token)
  diagnose                        as trace-agents           (inside the deployed trace-api; this script only sends
                                                            it the failed execution's name)
  score                           as trace-evaluator        (short-lived impersonated token)

    GCP_PROJECT=... TRACE_ENV=prod TRACE_API_URL=https://... \\
    OPS_IMPERSONATE=ops-automation@...  EVAL_IMPERSONATE=trace-evaluator@... \\
    python -m campaign.run --campaign demo-1 --seed 20261012

The plan is written to trace_truth.campaign_plan before the first injection. Re-running with the same campaign id
continues where it stopped; a different seed for an existing campaign is refused. Nothing is reselected.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

import httpx
from google.cloud import bigquery as bq

from evaluator.evaluate import Evaluator, evaluate
from evaluator.scoring import accuracy, confusion_matrix
from injector import ledger
from injector.actions import Clients
from injector.config import Settings
from injector.plan import build_plan
from injector.runner import end_fault, execute_and_wait, snapshot, start_fault

LOG_SETTLE_SECONDS = 25  # time for the run's last log lines and audit entries to reach the restricted bucket


def say(message: str, **fields: object) -> None:
    print(
        json.dumps({"time": datetime.now(UTC).strftime("%H:%M:%S"), "message": message, **fields}, default=str),
        flush=True,
    )


def identity_token() -> str:
    """The operator's own identity token, which is what trace-api's invoker permission is granted to."""
    out = subprocess.run(["gcloud", "auth", "print-identity-token"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def diagnose(api_url: str, execution: str, env: str) -> dict:
    response = httpx.post(
        f"{api_url.rstrip('/')}/incidents",
        json={"execution": execution, "env": env},
        headers={"Authorization": f"Bearer {identity_token()}"},
        timeout=620,
    )
    response.raise_for_status()
    return response.json()


def scheduler(action: str, settings: Settings) -> None:
    """Resume the dead-man's-switch schedule while faults are being injected; pause it afterwards."""
    subprocess.run(
        [
            "gcloud",
            "scheduler",
            "jobs",
            action,
            "trace-maintenance-schedule",
            "--location",
            settings.region,
            "--project",
            settings.project,
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def ensure_plan(c: Clients, campaign_id: str, seed: int) -> list[dict]:
    plan = build_plan(seed)
    stored = ledger.stored_plan(c, campaign_id)
    if not stored:
        ledger.record_plan(c, campaign_id, seed, plan)
        say("plan recorded before any injection", campaign=campaign_id, seed=seed, items=len(plan))
        return plan
    same = len(stored) == len(plan) and all(
        s["seed"] == seed and s["fault_type"] == p["fault_type"] and s["params"] == p["params"]
        for s, p in zip(stored, plan, strict=True)
    )
    if not same:
        raise SystemExit(f"campaign {campaign_id} already has a different plan; refusing to change it")
    say("existing plan found; resuming", campaign=campaign_id, seed=seed)
    return plan


def run_campaign(campaign_id: str, seed: int, limit: int | None, finalize: bool) -> int:
    settings = Settings.from_env()
    api_url = os.environ["TRACE_API_URL"]
    c = Clients(settings)
    ev = Evaluator()
    plan = ensure_plan(c, campaign_id, seed)
    baseline = snapshot(c)
    done_now = 0
    scheduler("resume", settings)
    try:
        for position, item in enumerate(plan, start=1):
            progress = ledger.campaign_progress(c, campaign_id).get(position)
            if progress and progress["scored"]:
                continue
            if limit is not None and done_now >= limit:
                break

            if progress is None:
                fault_id = start_fault(
                    c, item["fault_type"], item["params"], campaign_id=campaign_id, seed=seed, plan_position=position
                )
                try:
                    result = execute_and_wait(c)
                    ledger.set_execution(c, fault_id, result["execution"])
                    execution = result["execution"]
                    say("job finished", position=position, execution=execution, succeeded=result["succeeded"])
                    time.sleep(LOG_SETTLE_SECONDS)
                    response = diagnose(api_url, execution, settings.env)
                finally:
                    end_fault(c, fault_id)
                if snapshot(c) != baseline:
                    raise SystemExit(f"position {position}: target not restored after revert; stopping")
            else:
                # Started in an earlier run but not scored: the failure is over, so ask again. The service
                # diagnoses from the evidence it stored the first time.
                if not progress["reverted"]:
                    end_fault(c, progress["fault_id"])
                execution = progress["execution_name"]
                if not execution:
                    say(
                        "no execution was recorded for this position; it will be scored as unanswered",
                        position=position,
                    )
                    continue
                response = diagnose(api_url, execution, settings.env)

            say(
                "diagnosis",
                position=position,
                status=response["status"],
                stages=response["completed_stages"],
                llm_calls=response["llm_calls"],
                degraded_reason=response.get("degraded_reason"),
            )
            if response.get("root_cause_category") is None:
                say("no verdict (cap or provider problem); stopping so it can be retried", position=position)
                break
            scored = evaluate(ev, campaign_id, include_unanswered=False)
            say("scored", position=position, scored_now=len(scored))
            done_now += 1
    finally:
        scheduler("pause", settings)

    if finalize:
        left = evaluate(ev, campaign_id, include_unanswered=True)
        say("final scoring: incidents without a verdict are scored as wrong", scored_now=len(left))

    results = ev.query(
        f"""
        SELECT e.incident_id, e.predicted_category, e.true_category, e.is_correct
        FROM `{ev.truth}.evaluation_results` e
        JOIN `{ev.truth}.fault_ledger` f USING (fault_id)
        WHERE DATE(e.evaluated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
          AND DATE(f.injected_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND f.campaign_id = @campaign""",
        [bq.ScalarQueryParameter("campaign", "STRING", campaign_id)],
    )
    say(
        "campaign status",
        campaign=campaign_id,
        planned=len(plan),
        scored=len(results),
        accuracy=accuracy(results),
        confusion_matrix=confusion_matrix(results),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--limit", type=int, help="run at most this many new incidents, then stop")
    parser.add_argument(
        "--finalize", action="store_true", help="after the loop, score any incident still without a verdict as wrong"
    )
    args = parser.parse_args(argv)
    return run_campaign(args.campaign, args.seed, args.limit, args.finalize)


if __name__ == "__main__":
    sys.exit(main())
