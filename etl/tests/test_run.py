"""Offline tests for configuration, run status, error classification and fetch retries."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from etl import extract as extract_module
from etl.config import DEFAULT_OPEN_METEO_URL, Config
from etl.errors import AllStatesFailed
from etl.extract import MAX_ATTEMPTS, StateResult, fetch_state, most_common_error
from etl.main import require_some_states, run_status

ENV = {"GCP_PROJECT": "example-project", "BQ_DATASET": "trace_target_dev"}


def test_config_defaults():
    cfg = Config.from_env(ENV)
    assert (cfg.table, cfg.runs_table) == ("aqi_hourly", "pipeline_runs")
    assert cfg.open_meteo_url == DEFAULT_OPEN_METEO_URL
    assert (cfg.max_concurrency, cfg.min_states_ok, cfg.http_timeout_s) == (3, 30, 20.0)
    assert cfg.max_bytes_billed == 100 * 1024 * 1024


def test_config_overrides_and_public_dict():
    cfg = Config.from_env(
        {
            **ENV,
            "OPEN_METEO_URL": "https://example.invalid/aq",
            "HTTP_TIMEOUT_S": "5",
            "MIN_STATES_OK": "36",
            "CLOUD_RUN_EXECUTION": "trace-target-etl-abc12",
        }
    )
    assert cfg.open_meteo_url == "https://example.invalid/aq"
    assert cfg.http_timeout_s == 5.0
    assert cfg.public_dict()["execution_name"] == "trace-target-etl-abc12"


def test_config_requires_project_and_dataset():
    with pytest.raises(ValueError, match="GCP_PROJECT, BQ_DATASET"):
        Config.from_env({})


def test_run_status():
    assert run_status(36, 30) == "SUCCESS"
    assert run_status(30, 30) == "SUCCESS"
    assert run_status(29, 30) == "PARTIAL"
    assert run_status(1, 30) == "PARTIAL"


def test_all_states_failed_reports_the_most_common_cause():
    results = [
        StateResult("A", None, "ConnectError", "[Errno -2] Name or service not known"),
        StateResult("B", None, "ConnectError", "[Errno -2] Name or service not known"),
        StateResult("C", None, "ReadTimeout", "ReadTimeout('')"),
    ]
    assert most_common_error(results)[0] == "ConnectError"
    with pytest.raises(AllStatesFailed) as raised:
        require_some_states(results)
    assert raised.value.cause_class == "ConnectError"
    assert "0/3 states fetched" in str(raised.value)


def test_one_good_state_is_enough_to_continue():
    require_some_states([StateResult("A", {"hourly": {}}), StateResult("B", None, "ReadTimeout", "")])


def _fetch(handler, monkeypatch) -> tuple[StateResult, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(extract_module.asyncio, "sleep", fake_sleep)

    async def run() -> StateResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_state(
                client, asyncio.Semaphore(1), Config.from_env(ENV), "Delhi", 28.61, 77.21, "2026-10-09", "2026-10-10"
            )

    return asyncio.run(run()), sleeps


def test_fetch_retries_five_times_with_exponential_backoff(monkeypatch, capsys):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("[Errno -2] Name or service not known")

    result, sleeps = _fetch(handler, monkeypatch)
    assert len(calls) == MAX_ATTEMPTS == 5
    assert sleeps == [1, 2, 4, 8]
    assert not result.ok and result.error_class == "ConnectError"
    assert capsys.readouterr().out.count('"error_class": "ConnectError"') == 5


def test_fetch_recovers_after_a_server_error(monkeypatch):
    responses = iter([httpx.Response(503), httpx.Response(200, json={"hourly": {"time": []}})])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    result, sleeps = _fetch(handler, monkeypatch)
    assert result.ok and sleeps == [1]


def test_http_error_status_is_reported_by_class(monkeypatch):
    result, _ = _fetch(lambda request: httpx.Response(429), monkeypatch)
    assert not result.ok and result.error_class == "HTTPStatusError"
