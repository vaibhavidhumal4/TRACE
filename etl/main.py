"""Entry point of the target ETL (Cloud Run Job).

Exit codes: 0 for SUCCESS and for PARTIAL (deliberately quiet; pipeline_runs records it), 1 for FAILED.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime

from etl import load as warehouse
from etl.config import Config
from etl.errors import AllStatesFailed
from etl.extract import IST, StateResult, extract, most_common_error
from etl.logs import bind, log
from etl.transform import transform


def run_status(states_ok: int, min_states_ok: int) -> str:
    """SUCCESS when enough states were fetched, PARTIAL when some were. Zero is a failure, raised elsewhere."""
    return "SUCCESS" if states_ok >= min_states_ok else "PARTIAL"


def require_some_states(results: list[StateResult]) -> None:
    if any(r.ok for r in results):
        return
    cause_class, example = most_common_error(results)
    raise AllStatesFailed(f"0/{len(results)} states fetched; most common error {cause_class}: {example}", cause_class)


async def main() -> int:
    run_id = str(uuid.uuid4())
    bind(run_id=run_id)
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        log("ERROR", f"bad configuration: {exc}", phase="config", error_class=type(exc).__name__)
        return 1
    bind(execution_name=cfg.execution_name)
    log("INFO", "run started", phase="start", config=cfg.public_dict())

    client = None
    states_total = states_ok = None
    try:
        client = warehouse.make_client(cfg)
        warehouse.write_run_row(client, cfg, run_id=run_id, phase="START", status="RUNNING")

        results = await extract(cfg)
        states_total, states_ok = len(results), sum(1 for r in results if r.ok)
        require_some_states(results)

        df = transform(results, datetime.now(IST).replace(tzinfo=None))
        inserted = warehouse.load(client, cfg, df, run_id)

        status = run_status(states_ok, cfg.min_states_ok)
        if status == "PARTIAL":
            log("WARNING", f"only {states_ok}/{states_total} states fetched (minimum {cfg.min_states_ok})", phase="end")
        warehouse.write_run_row(
            client,
            cfg,
            run_id=run_id,
            phase="END",
            status=status,
            states_total=states_total,
            states_ok=states_ok,
            rows_inserted=inserted,
        )
        log("INFO", f"run finished: {status}, {inserted} rows inserted", phase="end", status=status)
        return 0
    except Exception as exc:
        error_class = type(exc).__name__
        log(
            "ERROR",
            f"run failed: {error_class}: {exc}",
            phase="end",
            error_class=error_class,
            status="FAILED",
            cause_class=getattr(exc, "cause_class", None),
        )
        if client is not None:
            try:
                warehouse.write_run_row(
                    client,
                    cfg,
                    run_id=run_id,
                    phase="END",
                    status="FAILED",
                    states_total=states_total,
                    states_ok=states_ok,
                    error_type=error_class,
                    error_message=str(exc),
                )
            except Exception as write_exc:
                log(
                    "ERROR",
                    f"could not record the failed run: {type(write_exc).__name__}: {write_exc}",
                    phase="end",
                    error_class=type(write_exc).__name__,
                )
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
