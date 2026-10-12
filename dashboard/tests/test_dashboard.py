"""Offline tests for the dashboard service. The data below is synthetic test data, not campaign results."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.cache import TTLCache
from dashboard.config import BYTE_CAP, Settings
from dashboard.main import create_app, render_markdown, render_postmortem_page
from dashboard.store import ResultsStore

SETTINGS = Settings.from_env({"GCP_PROJECT": "test-project"})


def incident(position: int, **over) -> dict:
    row = {
        "campaign_id": "test-campaign",
        "position": position,
        "incident_id": f"inc-{position}",
        "failed_execution": f"job-{position}",
        "verdict_category": "authorization_failure",
        "true_category": "authorization_failure",
        "true_cause": "iam_revoked",
        "outcome": "correct",
        "postmortem_text": "# Summary\n\n- **what**: `x`\n<script>alert(1)</script>",
        "postmortem_model": "model-a",
        "injected_params": {"role": "r"},
    }
    return {**row, **over}


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[str | None] = []
        self.fail = False

    def snapshot(self, campaign: str | None) -> dict:
        self.calls.append(campaign)
        if self.fail:
            raise RuntimeError("boom")
        campaign_id = campaign or "test-campaign"
        return {
            "campaign_id": campaign_id,
            "campaigns": [{"campaign_id": "test-campaign", "scored": 2}, {"campaign_id": "older", "scored": 0}],
            "incidents": [
                incident(1, campaign_id=campaign_id),
                incident(2, campaign_id=campaign_id, postmortem_text=None),
            ],
            "matrix": [
                {
                    "true_category": "authorization_failure",
                    "predicted_category": "authorization_failure",
                    "incidents": 2,
                }
            ],
            "bytes_billed": 123,
        }


@pytest.fixture
def served():
    store = FakeStore()
    return TestClient(create_app(SETTINGS, store)), store


def test_settings_can_only_tighten_the_byte_cap_and_lengthen_the_cache():
    loose = Settings.from_env({"GCP_PROJECT": "p", "BQ_MAX_BYTES_BILLED": str(10 * BYTE_CAP), "CACHE_TTL_S": "5"})
    assert loose.max_bytes_billed == BYTE_CAP == 100 * 1024 * 1024
    assert loose.cache_ttl_s == 60
    tight = Settings.from_env({"GCP_PROJECT": "p", "BQ_MAX_BYTES_BILLED": "1000", "CACHE_TTL_S": "300"})
    assert (tight.max_bytes_billed, tight.cache_ttl_s) == (1000, 300)
    with pytest.raises(ValueError):
        Settings.from_env({"GCP_PROJECT": "p", "RESULTS_DATASET": "trace_truth`; DROP"})
    with pytest.raises(ValueError):
        Settings.from_env({})


def test_snapshot_is_served_from_the_cache(served):
    client, store = served
    first = client.get("/api/snapshot").json()
    second = client.get("/api/snapshot?campaign=test-campaign").json()
    assert store.calls == [None]  # one query for both requests
    assert first["campaign_id"] == second["campaign_id"] == "test-campaign"
    assert first["cache_ttl_s"] == 60 and first["stale"] is False and len(first["incidents"]) == 2


def test_another_listed_campaign_is_queried_once_and_an_unknown_one_never(served):
    client, store = served
    assert client.get("/api/snapshot?campaign=older").json()["campaign_id"] == "older"
    assert client.get("/api/snapshot?campaign=older").status_code == 200
    assert client.get("/api/snapshot?campaign=made-up").status_code == 404
    assert client.get("/api/snapshot?campaign=x'%20OR%201=1").status_code == 404
    assert store.calls == [None, "older"]


def test_a_failed_query_is_an_error_not_invented_data(served):
    client, store = served
    store.fail = True
    response = client.get("/api/snapshot")
    assert response.status_code == 502
    assert "incidents" not in response.json()


def test_the_service_is_read_only_and_sends_security_headers(served):
    client, _ = served
    for method in ("post", "put", "patch", "delete"):
        for path in ("/", "/api/snapshot", "/postmortem?campaign=test-campaign&position=1"):
            assert getattr(client, method)(path).status_code == 405
    page = client.get("/")
    assert page.status_code == 200 and "TRACE" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get("/docs").status_code == 404
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_postmortem_page_escapes_model_text(served):
    client, _ = served
    page = client.get("/postmortem?campaign=test-campaign&position=1")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<strong>what</strong>" in page.text and "<code>x</code>" in page.text
    assert client.get("/postmortem?campaign=test-campaign&position=2").status_code == 404  # no postmortem written
    assert client.get("/postmortem?campaign=test-campaign&position=9").status_code == 404
    assert client.get("/postmortem?campaign=made-up&position=1").status_code == 404


def test_markdown_subset():
    out = render_markdown("## Title\n\ntext one\ntext two\n\n1. a\n2. b\n\n```\n<b>raw</b>\n```\n")
    assert "<h3>Title</h3>" in out and "<p>text one text two</p>" in out
    assert "<ol>\n<li>a</li>\n<li>b</li>\n</ol>" in out
    assert "&lt;b&gt;raw&lt;/b&gt;" in out and "<b>raw</b>" not in out
    page = render_postmortem_page('c"><img>', incident(1, failed_execution="<x>", postmortem_fallback_reason="<y>"))
    assert "<img>" not in page and "<x>" not in page and "<y>" not in page


def test_cache_expires_and_serves_stale_on_failure():
    now = [0.0]
    cache = TTLCache(60, clock=lambda: now[0])
    loads: list[int] = []

    def load() -> dict:
        loads.append(1)
        return {"n": len(loads)}

    assert cache.get("k", load) == ({"n": 1}, False)
    now[0] = 59.0
    assert cache.get("k", load) == ({"n": 1}, False)
    now[0] = 61.0
    assert cache.get("k", load) == ({"n": 2}, False)

    def broken() -> dict:
        loads.append(1)
        raise RuntimeError("down")

    now[0] = 130.0
    value, stale = cache.get("k", broken)
    assert stale and value["n"] == 2
    now[0] = 150.0
    assert cache.get("k", broken)[1] is True and len(loads) == 3  # no new attempt inside the TTL
    with pytest.raises(RuntimeError):
        cache.get("never-loaded", broken)


class FakeJob:
    total_bytes_billed = 42

    def __init__(self, row: dict) -> None:
        self._row = row

    def result(self):
        return iter([self._row])


class FakeClient:
    def __init__(self, row: dict) -> None:
        self.row = row
        self.seen: list[tuple[str, object]] = []

    def query(self, sql: str, job_config=None):
        self.seen.append((sql, job_config))
        return FakeJob(self.row)


def test_store_runs_one_capped_parameterised_select_and_decodes_json_text():
    row = {
        "campaign_id": "test-campaign",
        "campaigns": [json.dumps({"campaign_id": "test-campaign"})],
        "incidents": [
            json.dumps({"position": 1, "injected_params": '{"a":1}', "remediation_proposals": '[{"action":"x"}]'})
        ],
        "matrix": [json.dumps({"incidents": 1})],
    }
    client = FakeClient(row)
    snapshot = ResultsStore(SETTINGS, client).snapshot("it's")
    assert len(client.seen) == 1
    sql, config = client.seen[0]
    assert config.maximum_bytes_billed == 100 * 1024 * 1024
    assert config.query_parameters[0].value == "it's" and "it's" not in sql
    statement = sql.strip().upper()
    assert statement.startswith("WITH") and not any(
        w in statement for w in ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE")
    )
    assert "trace_truth" not in sql and "trace_ledger" not in sql and sql.count("test-project.trace_results.") == 3
    assert snapshot["incidents"][0]["injected_params"] == {"a": 1}
    assert snapshot["incidents"][0]["remediation_proposals"] == [{"action": "x"}]
    assert snapshot["bytes_billed"] == 42
