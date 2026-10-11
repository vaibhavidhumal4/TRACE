# TRACE — Multi-Agent Incident Investigation & Root-Cause Analysis

TRACE watches a real, scheduled data pipeline, deliberately breaks it in controlled ways, and uses a team of AI agents
(Triage, Root-Cause, Remediation, Postmortem, Evaluation) to diagnose each failure. Because we cause every fault, we know the
true cause, so diagnostic accuracy is a **measured number**, not a claim. Agents may only **propose** fixes, never execute them.

This file is the source of truth for how to build it. Read all of it before writing code.

---

## 0. Rules that override everything else

1. **Nothing runs on the developer's laptop.** No local Docker, no local servers, no local model training. Code is written and
   run in the cloud (Cloud Shell or GitHub Actions). If a step seems to need a local runtime, find a cloud alternative or ask.
2. **Free tier only.** Before enabling any API, creating any resource, or running anything that could bill, say what it costs and
   ask. Never assume a service is free; check the current pricing page.
3. **BigQuery is the only database.** No PostgreSQL, no Redis, no Firestore. Managed plumbing is allowed (Pub/Sub, Secret Manager,
   Artifact Registry, Cloud Logging, Cloud Scheduler). Cloud Storage is NOT used unless artifacts outgrow BigQuery STRING columns.
4. **Propose-only remediation, enforced by IAM, not by prompt.** The agents' service account must have no write permission on the
   target system. A test must prove a write attempt returns 403 (TC-20).
5. **Every function must actually work.** No stubs, `pass`, `TODO`, or hardcoded mock returns in non-test code. Each phase ends
   with an acceptance check that passes in the cloud. Do not start phase N+1 until phase N's acceptance passes.
6. **No secrets in git, no service-account JSON keys.** Use Secret Manager for API keys and Workload Identity Federation for
   GitHub Actions. The repo will be public and open source.
7. **Ask before**: any IAM change, any delete, enabling an API, changing Scheduler jobs, or spending anything.
   (In Cloud Shell you hold the human's full credentials. Do not auto-approve `gcloud` commands that mutate IAM or delete resources.)
8. **Evaluation integrity** (see section 5.3): agents must never be able to read ground truth.
9. **No skipped points, no shortcuts.** Every checklist item in section 9 and every test case must be implemented as written. If
   something is blocked (quota, permission, plan limit, a service that does not behave as documented), stop, report the exact
   error, and ask. Never substitute a weaker approach, stub it, or tick a box silently.
10. **Scope is fixed (decision 2026-10-11, demo scope; replaces the 2026-10-09 scope).** Where another section still describes
    the wider design, this rule wins.
    - **In:** four faults (`iam_revoked`, `bq_extra_column`, `task_timeout_kill`, `bad_endpoint_url`), injected by changing the job,
      the dataset permissions or the table directly; a closed four-category taxonomy with no `unknown`; six components (Triage,
      Root-Cause, Remediation, Postmortem, Evaluation, and a thin LangGraph orchestrator); 12 labelled incidents (3 of each
      fault) produced by one campaign runner and recorded, not shown live; accuracy and a confusion matrix, shown in Looker
      Studio over BigQuery views.
    - **Designed, next steps (do not build now):** the upstream gateway service and the faults that need it (`upstream_slow`,
      `upstream_429`, `upstream_schema_drift`, `partial_upstream`), `dataset_missing`, the `unknown` category, control runs,
      embedding memory and the memory ablation (4.2), the MCP server (4.3), the dashboard service (Phase 6), and the automatic
      trigger (log sink -> Pub/Sub push, the freshness watchdog and the rest of Phase 2). For the demo the campaign runner
      calls `trace-api` directly.
    - **Out:** n8n (it cannot run on the free tier without a laptop or a VM, and a laptop breaks rule 1).
    - Every IAM and no-leakage rule (rules 4, 6, 7, 8 and sections 5.3 and 6) is unchanged by the reduced scope.
    - Do not add other frameworks or services without asking.

---

## 1. Architecture (BigQuery-only, GCP-native)

The original design used Oracle VM + PostgreSQL + Redis + Celery. This is the cloud-only equivalent.

| Original design | Now | Notes |
|---|---|---|
| Target ETL container | Cloud Run **Job** `trace-target-etl` | Cloud Scheduler triggers it every 4 h (`0 */4 * * *`, tz Asia/Kolkata) |
| Celery Beat | Cloud Scheduler | |
| Celery + Redis queues | **Pub/Sub** topics with push subscriptions to the API | At-least-once delivery, so every handler must be idempotent |
| Redis (cache / state) | BigQuery append-only `incident_events` + view `incident_state`; config in env / Secret Manager | BigQuery is not low-latency; keep in-flight state inside the request |
| PostgreSQL Incident Ledger | BigQuery dataset `trace_ledger` | |
| Ground truth inside the ledger | BigQuery dataset **`trace_truth`** (separate, restricted IAM) | **Changed vs. SDD**, see 5.3 |
| Object Storage | Postmortem text as BigQuery STRING | |
| FastAPI orchestrator + agents | Cloud Run **service** `trace-api` (agents are Python modules; orchestration is a **LangGraph** graph, 4.1) | Runs as `trace-agents`. Scale to zero, max-instances 2 |
| Dashboard | Cloud Run **service** `trace-dashboard`: same container image, different entrypoint (Jinja2 + Chart.js) | Runs as `trace-dashboard`, the only runtime identity allowed to read evaluation results (5.3) |
| MCP server (new) | Cloud Run **service** `trace-mcp`: same image, read-only tools over streamable HTTP (4.3) | Runs as `trace-mcp`; no access to `trace_truth` |
| Memory retrieval (new) | Gemini embeddings stored in `trace_ledger.incident_memory`, searched with BigQuery `VECTOR_SEARCH` (4.2) | No new service |
| Fault Injector | Cloud Run Job `trace-maintenance-runner` + service `trace-upstream-gateway` (neutral names on purpose, 5.3) | Own service account |
| LLM API | Gemini API (AI Studio key) primary, Groq fallback | Keys in Secret Manager. Claude Pro does NOT include API access |
| Tool APIs (Docker/K8s) | Cloud Run Admin API, Cloud Logging API, BigQuery API, Cloud Audit Logs | Docker/K8s labels in the old diagram no longer apply |
| Observability | Cloud Logging (structured JSON), Cloud Monitoring | |
| CI/CD | GitHub Actions + Workload Identity Federation -> Artifact Registry -> Cloud Run | |

```
Cloud Scheduler --(4h)--> [Cloud Run Job: target ETL] --> BigQuery trace_target.{aqi_hourly, pipeline_runs}
        ^                         ^   |
        |                         |   +--> Cloud Logging (JSON logs, exit codes)
 [Fault Injector Job] --breaks--> +-- (via config / IAM / schema / fault-proxy)
        |                                        |
        +--> trace_truth.fault_ledger            v
                                   log sink / watchdog --> Pub/Sub --> [trace-api on Cloud Run: LangGraph graph]
                                                                        Triage -> Root-Cause -> Remediation -> Postmortem
                                                                        writes --> trace_ledger.*
                                   [Evaluation] reads trace_ledger + trace_truth --> trace_truth.evaluation_results
                                   Dashboard [trace-dashboard] reads views + evaluation_results; MCP [trace-mcp] reads ledger/target only
```

**Detection is deterministic code, not an LLM.** An incident opens when (a) a Cloud Run Job execution fails (log sink -> Pub/Sub),
(b) `pipeline_runs` shows `PARTIAL`/`FAILED`, or (c) a scheduled freshness watchdog finds no new rows for more than 6 h.
Dedupe on execution name. False-positive rate = incidents opened while no fault was active.

---

## 2. Target system: the AQI ETL

Two reference scripts were provided; treat both as read-only references under `reference/`, not deployment targets:

- `reference/aqi_pollution_days_colab.py` — pulls a static yearly good/bad-days dataset from data.gov.in. Its
  `fetch_page` signature (`offset` only) does not match its caller (`offset=0, limit=limit`) — this script cannot
  run as-is. Its dedupe key (`state`, `city`) means re-running it never inserts new rows, since the source data
  doesn't change per run. Useful for extraction/staging patterns only, not as the live target.
- `reference/aqi_hourly_colab.py` — pulls live hourly air-quality data (36 states, Open-Meteo). This is the one to
  deploy: it re-fetches real, changing data every run, which is what a schedulable, breakable target system needs.

**Build the deployable target from `aqi_hourly_colab.py`. Keep** (these are good): semaphore concurrency of 3,
exponential-backoff retries (5 tries), staging table + idempotent insert on `(state, ts)`, dropping future forecast
hours, naive-IST `DATETIME` convention.

**Must change to make it deployable and diagnosable:**

1. Delete `google.colab` auth. Use Application Default Credentials (the Cloud Run runtime service account).
2. Replace top-level `await main()` (notebook-only, a SyntaxError in a container) with `sys.exit(asyncio.run(main()))`.
3. Read all config from env vars: `GCP_PROJECT`, `BQ_DATASET`, `BQ_TABLE`, `OPEN_METEO_URL`, `HTTP_TIMEOUT_S`, `MAX_CONCURRENCY`,
   `MIN_STATES_OK`. `OPEN_METEO_URL` is what lets the fault proxy be swapped in. No hardcoded project ID (the repo is public).
4. **Exit codes and status.** The current script exits 0 even if every state failed. Define: all states failed or any unhandled
   error -> `FAILED`, exit 1. Some states fetched but fewer than `MIN_STATES_OK` -> `PARTIAL`, log WARNING, **exit 0** (deliberately
   silent; detection relies on `pipeline_runs`). Otherwise `SUCCESS`, exit 0.
5. Structured JSON logs to stdout with `severity`, `message`, `run_id`, `phase`, `state`, `attempt`, `error_class`. Log real
   exception classes (`ReadTimeout`, `HTTPStatusError`, `Forbidden`) because the agents diagnose from them.
6. Write a `pipeline_runs` row at start (`RUNNING`) and at end. A killed run leaves an orphan `RUNNING` row, which is a real signal.
7. New table `aqi_hourly`, `PARTITION BY DATE(ts)`, `CLUSTER BY state`, 90-day partition expiry. Add a `ts` range
   filter (last 2 days) inside the `NOT EXISTS` subquery to cut scanned bytes.
8. Fix `aqi_category`: NaN and AQI > 500 currently become the string `"nan"`. Use `"Unknown"`.
9. Classify failures into typed exceptions so `error_type` is meaningful (an all-empty response currently crashes in `pd.concat([])`).
10. `requirements.txt` (pinned): `pandas`, `pyarrow`, `db-dtypes`, `google-cloud-bigquery`, `httpx`, `pytz`. Slim Python base image, non-root user.
11. Offline unit tests for `transform()` using a saved JSON fixture (run in GitHub Actions).
12. Cloud Run Job must use `--max-retries=0`. Default retries would re-run a failed task and blur the fault/incident mapping.

**Known limitation to document, not fix:** Open-Meteo may revise recent hours, and `NOT EXISTS` never updates existing rows.
Also attribute Open-Meteo (and CAMS/Copernicus) in the README, and verify their license terms before the open-source release.
At 36 calls per run and 6 runs per day (about 216/day), usage is far below Open-Meteo's free limits, but re-check current limits.

---

## 3. BigQuery data model

Single location for everything. Default `us-central1` (regional, not the `US` multi-region) so datasets co-locate with Cloud Run.
Dataset location cannot be changed later, and query jobs must run in the dataset's location. Confirm the region in Phase 0.
BigQuery has no enforced primary or unique keys: enforce idempotency with `idempotency_key` + `MERGE` / insert-if-not-exists.
Prefer **load jobs** (free) over streaming inserts (billed, need billing enabled).

```sql
-- Dataset trace_target
CREATE TABLE IF NOT EXISTS trace_target.aqi_hourly (
  state STRING, ts DATETIME, pm25 FLOAT64, pm10 FLOAT64, no2 FLOAT64, so2 FLOAT64,
  co FLOAT64, o3 FLOAT64, us_aqi FLOAT64, aqi_category STRING, ingested_at_ist DATETIME
) PARTITION BY DATE(ts) CLUSTER BY state OPTIONS (partition_expiration_days = 90);

CREATE TABLE IF NOT EXISTS trace_target.pipeline_runs (
  run_id STRING, execution_name STRING, phase STRING,      -- START | END
  status STRING,                                            -- RUNNING | SUCCESS | PARTIAL | FAILED
  ts TIMESTAMP, states_total INT64, states_ok INT64, rows_inserted INT64,
  error_type STRING, error_message STRING, config JSON      -- non-secret env config
) PARTITION BY DATE(ts);

-- Dataset trace_ledger  (agents can read AND write here)
CREATE TABLE IF NOT EXISTS trace_ledger.incidents (
  incident_id STRING, detected_at TIMESTAMP, detector STRING, source_run_id STRING,
  source_execution STRING, severity STRING, status STRING, dedup_key STRING
) PARTITION BY DATE(detected_at);

CREATE TABLE IF NOT EXISTS trace_ledger.incident_events (   -- append-only; latest row per incident = state
  event_id STRING, incident_id STRING, stage STRING, agent STRING, created_at TIMESTAMP,
  idempotency_key STRING,                                   -- '{incident_id}:{stage}'
  payload JSON
) PARTITION BY DATE(created_at) CLUSTER BY incident_id;

CREATE TABLE IF NOT EXISTS trace_ledger.agent_verdicts (
  verdict_id STRING, incident_id STRING, agent_name STRING, created_at TIMESTAMP,
  root_cause_category STRING, hypothesis STRING, confidence FLOAT64,
  evidence JSON, proposals JSON, model STRING, llm_calls INT64, latency_ms INT64, memory_enabled BOOL
) PARTITION BY DATE(created_at) CLUSTER BY incident_id;

-- Embedding memory (4.2). Written ONLY by the evaluator, for training-split incidents, after they have been evaluated.
-- Not partitioned: it stays tiny (about 100 rows x 768 floats, under 1 MB), so a full scan is negligible. The partition-filter
-- rule below does not apply to this one table; maximum_bytes_billed still does.
CREATE TABLE IF NOT EXISTS trace_ledger.incident_memory (
  memory_id STRING, incident_id STRING, promoted_at TIMESTAMP,
  evidence_text STRING,              -- sanitized text that was embedded; never contains a fault name or truth-derived text
  root_cause_category STRING,        -- confirmed true category, copied from trace_truth by the evaluator (training split only)
  resolution_summary STRING,         -- standard remedy text for that category from the fault catalog
  embedding ARRAY<FLOAT64>,          -- one fixed length for the lifetime of the table
  embedding_model STRING, embedding_dim INT64
);
-- Never mix embedding models or dimensions in this table. Changing either means re-embedding every row.

-- Dataset trace_truth  (agents have NO access; only injector and evaluator)
CREATE TABLE IF NOT EXISTS trace_truth.fault_ledger (
  fault_id STRING, fault_type STRING, expected_category STRING, injected_at TIMESTAMP,
  revert_at TIMESTAMP, reverted_at TIMESTAMP, params JSON, seed INT64,
  campaign_id STRING, revert_plan JSON,
  split STRING                                              -- memory (training) | heldout; assigned at campaign planning (4.2)
) PARTITION BY DATE(injected_at);

CREATE TABLE IF NOT EXISTS trace_truth.evaluation_results (
  eval_id STRING, incident_id STRING, fault_id STRING, predicted_category STRING,
  true_category STRING, is_correct BOOL, confidence FLOAT64,
  time_to_diagnosis_sec INT64, split STRING, evaluated_at TIMESTAMP   -- split: memory (training, promoted to incident_memory) | heldout
) PARTITION BY DATE(evaluated_at);
```

Every query goes through one helper that sets `maximum_bytes_billed` (default 100 MB) and filters on the partition column.

---

## 4. Agents

Six components. Only **Root-Cause** and **Postmortem** call an LLM; Triage, Remediation, Evaluation and the Orchestrator are
rule-based code.

Common contract: input is `incident_id`. Each component reads through a small allow-list of **read-only tools**, returns JSON validated
by a pydantic schema, and writes to `agent_verdicts` + `incident_events` with idempotency key `{incident_id}:{stage}`.
Cap LLM calls per incident (default 12) and per day (env `LLM_DAILY_CALL_CAP`). Keep an `llm.py` interface with provider fallback.
Only send this project's own logs to free-tier LLMs (providers may use free-tier inputs for training). No personal data exists here.

| Component | LLM? | Job | Output |
|---|---|---|---|
| Triage | No (rules) | Severity from run status, blast radius (states affected), staleness | `severity` in Low/Medium/High/Critical |
| Root-Cause | **Yes** | Diagnose from evidence; **must choose from the closed taxonomy** | `root_cause_category`, free-text hypothesis, `confidence` 0-1, `evidence[]` |
| Remediation | No (rules) | Look up the fault catalog's standard fix for the diagnosed category and record it as a proposal. A ledger write only; never executes | `proposals[]`: `{action, risk, blast_radius, rollback, confidence}` |
| Postmortem | **Yes** | Markdown report from the incident timeline | text stored in BigQuery, with the model that wrote it |
| Evaluation | No (deterministic, never an LLM) | Scores verdicts against `trace_truth`. Runs **separately, as `trace-evaluator`, after** the orchestrator's four stages; the orchestrator never invokes it, because it runs as `trace-agents` and must not reach truth | rows in `evaluation_results` |
| Orchestrator | No | Thin LangGraph graph that runs Triage, Root-Cause, Remediation and Postmortem in order for one incident (4.1) | stage events in `incident_events` |

**Models (decision 2026-10-11).**
- **Root-Cause: Groq `openai/gpt-oss-120b`, reasoning effort `low`.** No fallback model: every accuracy number must come from one
  model. If Groq is unavailable or a cap is hit, the incident takes the `degraded` route (4.1) and is retried later; it is never
  diagnosed by a different model.
- **Postmortem: Gemini 2.5 Flash, falling back to Groq `openai/gpt-oss-120b` when the daily Gemini quota is exhausted.** Record the
  model that actually wrote each postmortem (`agent_verdicts.model` and the postmortem event payload).
- **Keep prompts compact, about 3K tokens.** Groq allows 8K tokens per minute and 200K per day, so that is at most two Root-Cause
  calls a minute and roughly 60 a day. Root-Cause therefore gathers its evidence with the read-only tools in code first and makes
  **one** LLM call per incident; the caller paces requests to stay under the per-minute limit.

**Root-Cause tools (all read-only):** `get_pipeline_runs(n)`, `get_run_logs(execution)` (Cloud Logging API, through the restricted
log view), `get_recent_config_changes(hours)` (Cloud Run Admin API + Audit Logs), `get_iam_changes(hours)`,
`get_bq_job_errors(hours)` (`INFORMATION_SCHEMA.JOBS`), `get_data_freshness()`. Designed, next steps: `get_anomaly_stats(metric)`
and `get_similar_past_incidents(k)` (4.2).

**Closed root-cause taxonomy** (the agent must output exactly one of these four; there is **no** `unknown` option):
`authorization_failure`, `warehouse_schema_mismatch`, `resource_limit_exceeded`, `dns_or_endpoint_unreachable`.
A closed enum makes accuracy a deterministic equality check. Never an LLM judge for accuracy.
Designed, next steps: the wider nine-category taxonomy (`network_timeout`, `upstream_rate_limited`, `upstream_schema_drift`,
`partial_data_loss`, `unknown`) that goes with the deferred faults in 5.1.

**Memory: designed, next steps.** No memory retrieval is built for the demo, so `memory_enabled` is always `false`. The design is
kept in 4.2.

### 4.1 Orchestrator (LangGraph)
- Demo scope: the graph runs four stages. Only `root_cause` and `postmortem` call an LLM; `triage` and `remediation` are rule-based
  nodes. Evaluation is **not** a node: it runs afterwards as `trace-evaluator` (section 4 table). `memory_enabled` stays in the state
  and is always `false`.
- Demo trigger: the campaign runner calls an authenticated HTTPS endpoint on `trace-api` with the failed execution's name. The
  endpoint opens the incident (dedupe on execution name) and runs the graph, all as `trace-agents`. The Pub/Sub push trigger
  below is designed, next steps; the idempotency rules apply to the direct call in the same way.
- One `StateGraph` in `api/orchestrator/graph.py`. Each node **wraps the existing agent function unchanged**; do not rewrite agents to
  fit LangGraph. State is a small typed dict: `incident_id`, `memory_enabled`, `completed_stages`, `llm_calls`, `status`.
- Flow: `load_state` -> `triage` -> `root_cause` -> `remediation` -> `postmortem` -> END. `load_state` reads `incident_events` and
  records which stages are finished; a node is skipped when its `{incident_id}:{stage}` key already exists (this is how a crash or a
  Pub/Sub redelivery resumes). Before each node check the per-incident cap (12) and the daily cap (`LLM_DAILY_CALL_CAP`); if either is
  exceeded, route to a `degraded` node that appends an `llm_cap_reached` event and ends cleanly, with no exception.
- Trigger: Pub/Sub push subscription -> `POST /pubsub/incidents` on `trace-api`. Verify the push OIDC token, dedupe on the Pub/Sub
  message ID plus the idempotency keys, then `graph.invoke(...)`. Set the subscription ack deadline to 600 s (the maximum) and process
  synchronously: Cloud Run throttles CPU once the response is sent, so never ack early and keep working in the background.
- **No LangGraph checkpointer.** BigQuery is too slow to checkpoint every step (section 1); durability comes from the append-only
  `incident_events` table.
- No LangChain LLM wrappers and no `langchain-google-genai`. `llm.py` stays the only LLM interface (provider fallback, call counting).
  Pin `langgraph` and `langchain-core`; CI prints the image size (section 8).
- Tests: TC-26 (deliver the same message twice: no duplicate verdicts and no second LLM call); TC-27 (cap reached: ends in
  `degraded`, no crash).

### 4.2 Embedding memory (designed, next steps: not built for the demo)
- `get_similar_past_incidents(k)` embeds the current incident's sanitized evidence text and runs `VECTOR_SEARCH` (`COSINE`, exact
  brute force; no vector index is needed at this size) over `trace_ledger.incident_memory`. It returns `k` neighbours (default 3) with
  category, resolution summary and distance, excludes the current incident, is read-only, and runs under `maximum_bytes_billed`. The
  ablation therefore measures the value of **retrieval**, not of a plain lookup.
- Embeddings come from the Gemini API with the AI Studio key. Do **not** use BigQuery ML `ML.GENERATE_EMBEDDING` (it goes through a
  Vertex AI connection and bills). Code lives in `api/memory/embeddings.py` behind an interface. Choose the model in Phase 0 from what AI
  Studio shows as free for this key: `gemini-embedding-001` (text only; a list of strings returns one vector per string) or
  `gemini-embedding-2` (a list of inputs is merged into **one** vector unless each input is its own `Content`). Use
  `output_dimensionality=768`, normalise vectors, and store `embedding_model` and `embedding_dim` on every row.
  **TC-30:** N texts in one call return N vectors of the configured length, and one text embedded twice lands within a tight cosine
  distance of itself.
- **Promotion** is the only path from truth into the ledger. After the evaluator scores an incident with `split = 'memory'`, it builds
  the sanitized evidence text from the ledger (the same text the agent saw), copies the confirmed category from `trace_truth`, adds the
  catalog's standard remedy text, embeds, and inserts one row. Held-out incidents are **never** promoted (TC-31).
- Leakage controls that keep the ablation honest:
  1. `split` is assigned at campaign planning, stratified by fault type, with a logged seed.
  2. Fault parameters differ between training and held-out incidents of the same type (a different timeout value, different states
     dropped, a different extra column name), so retrieval cannot succeed by copying.
  3. The verdict's `evidence` JSON records the retrieved neighbours (`incident_id`, category, distance). The evaluator reports
     **retrieval precision@1** next to accuracy, so a gain that only reflects "same fault type" is visible.
  4. The embedded text never contains a fault name or any truth-derived text; the category and remedy are separate columns.
- Memory on and memory off both re-run **only Root-Cause** on the stored evidence of each held-out incident (no re-injection).
  Do this inside the 30-day log retention window (section 8), because the tools read logs live.
- Volume: a few hundred embedding calls in total. The binding free-tier limit is chat calls per day (section 8).
- Fallback, which needs an explicit human decision because it changes image size and the embedding model: if Phase 0 shows embeddings
  are not free or are too limited, embed inside the container with a small ONNX model (CPU, no API quota). Never fall back silently to a
  plain lookup.

### 4.3 MCP server (`trace-mcp`) (designed, next steps: not built for the demo)
- Purpose: let Claude Desktop, or any MCP client, query TRACE read-only. Use the official `mcp` Python SDK's `FastMCP` over
  **streamable HTTP with `stateless_http=True`** (Cloud Run may run several instances and scales to zero between calls). Same image as
  `trace-api`, its own Cloud Run service `trace-mcp`, its own service account `trace-mcp`. Claude connects from Anthropic's cloud, not
  from the laptop, so the service is public; this also keeps rule 1 intact (no stdio server on the laptop).
- Tools, all read-only, typed, with a `limit` cap: `list_incidents(status, since_hours, limit)`, `get_incident(incident_id)` (state
  and timeline from `incident_events`), `get_evidence(incident_id)` (verdict evidence, proposals and retrieved neighbours),
  `get_recent_logs(minutes, severity, limit)` (target logs through the restricted log view only), `get_pipeline_runs(n)`,
  `get_data_freshness()`. No tool takes SQL. No tool touches `trace_truth`, evaluation results, accuracy numbers or fault names. No
  write tools.
- Auth: a bearer token in the `Authorization` header, compared in constant time **before** any BigQuery or Logging call; token in
  Secret Manager. Cloud Run must allow unauthenticated invocation (Anthropic's cloud cannot present a Google identity), so this token
  is the only gate: treat it like a password, rotate by adding a new secret version and **destroying** the old one. `max-instances=1`.
- Claude side: on a Pro plan, Customize -> Connectors -> Add custom connector, paste the service URL, set Authentication to **No sign-in**,
  and put the token under Request headers as `Authorization` with the value `Bearer <token>` (the scheme must be typed into the value;
  Claude sends the value exactly as entered). Editing a connector means removing and re-adding it. Custom remote connectors are available on
  Free (one), Pro, Max, Team and Enterprise and work in Claude Desktop (docs: claude.com/docs/connectors/custom/add-unlisted).
- **Known risk, confirmed from Anthropic's docs 2026-10-10:** Request headers is a **beta available to a limited set of organizations**; if the
  field is not shown in the dialog, the account does not have it. So the Phase 0 hello-tool smoke test (done by the human) decides the path:
  1. Request headers field present -> bearer-token design above.
  2. Field absent -> stop and ask the human. Options, in order: (a) OAuth 2.0 on the MCP server with Dynamic Client Registration or a Client ID
     Metadata Document (the SDK supports an auth provider; a `401` with `WWW-Authenticate: resource_metadata=...` is required; redirect URI
     `https://claude.ai/api/mcp/auth_callback`; more work, still free); (b) an unauthenticated server exposing only low-sensitivity,
     capped, aggregated read-only data (the human must explicitly accept this); (c) drop the MCP server, which nothing else depends on.
  A local bridge process on the laptop is **not** an option (rule 1).
- Tests: TC-28 (the `trace-mcp` identity gets 403 on `trace_truth` and on the injector's logs); TC-29 (no token or a wrong token
  returns 401 and creates zero BigQuery jobs).

---

## 5. Fault injection

### 5.1 Fault catalog (each maps to a test case and a taxonomy label)

Exactly four faults, each injected by changing the job, the dataset permissions or the table directly. No upstream gateway
service is involved. 3 injections of each, 12 labelled incidents in total.

| Fault | How (cloud-native, reversible) | Expected category | Test |
|---|---|---|---|
| `iam_revoked` | Remove the ETL service account's write access on `trace_target` | `authorization_failure` | TC-11 |
| `bq_extra_column` | `ALTER TABLE ... ADD COLUMN` on `aqi_hourly` so the `INSERT ... SELECT s.*` column count mismatches | `warehouse_schema_mismatch` | TC-14 |
| `task_timeout_kill` | `gcloud run jobs update ... --task-timeout` to a few seconds so the run is killed mid-execution | `resource_limit_exceeded` | TC-10 |
| `bad_endpoint_url` | Override `OPEN_METEO_URL` on the job with an unresolvable host | `dns_or_endpoint_unreachable` | extra |

**Verify empirically in Phase 3** that each fault produces a distinct, observable signal in logs / `pipeline_runs`. Record the real
error text in the decision log. If two faults are confusable, **tell the human** before running the campaign. Report a confusion
matrix.

**No reselection.** The list of injections is fixed, with a logged seed, before the campaign runs. Every incident it produces is
scored; none is dropped, re-run or replaced after results are seen.

**Designed, next steps (not injected for the demo):**

| Fault | How | Expected category | Test |
|---|---|---|---|
| `upstream_slow` | Fault proxy delays responses beyond `HTTP_TIMEOUT_S` | `network_timeout` | TC-12 |
| `upstream_429` | Fault proxy returns HTTP 429 | `upstream_rate_limited` | TC-13 |
| `upstream_schema_drift` | Fault proxy renames JSON keys | `upstream_schema_drift` | extra |
| `partial_upstream` | Fault proxy returns empty data for some states (run ends `PARTIAL`, exit 0) | `partial_data_loss` | TC-19 |
| `dataset_missing` (held out) | Rename the target dataset or table | not in taxonomy, expected answer `unknown` with low confidence | TC-22 |
| none (control) | No fault, scheduled at random | no incident should open | TC-15, TC-18 |

### 5.2 Fault safety
- The injector only touches resources labelled `trace-scope=target` and uses a least-privilege service account.
- **Dead-man's switch:** write the revert plan and `revert_at` to `fault_ledger` *before* injecting. A separate scheduled reverter
  always restores state at `revert_at`, even if the injector crashed. Test this explicitly.
- Randomize fault selection with a logged `seed`. Group runs into a `campaign_id`.
- **Campaign runner:** the 4-hourly schedule gives only about 6 runs per day, so 50 labelled incidents would take weeks.
  Build an on-demand runner that does inject -> execute job -> detect -> diagnose -> revert -> evaluate, N times back-to-back
  (bounded by the LLM daily cap). The 4-hour schedule is for the live demo; campaigns produce the accuracy numbers.
- **One campaign script, a different identity per step (decision 2026-10-11).** Per incident: inject, trigger the job directly
  (`gcloud run jobs execute`), wait for the failure, call the orchestrator, revert, score. The script never holds all three
  powers at once: it injects and reverts as `ops-automation`, the agents run as `trace-agents` inside the deployed `trace-api`
  service (the script only sends it the execution name over HTTPS), and scoring runs as `trace-evaluator`. The agents therefore
  never execute with credentials that can read `trace_truth`. The impersonation and invoker grants this needs are brought to
  the human as a plan in Phase 3. The dead-man's-switch reverter stays.

### 5.3 Evaluation integrity (do not weaken these)
- `trace_truth` is a separate dataset; `trace-agents` has **no** role on it. (The old SDD put `injected_fault_type` in `incidents`,
  where the Root-Cause Agent's memory query could read it. That is removed.)
- Name the injector's service account neutrally (e.g. `ops-automation@`) and put no descriptive labels like `fault=true` on
  resources. Agents *should* see that a config change happened (a real on-call would) but never why.
- Incidents are attributed to faults by time window: `source_execution` start time within `[injected_at, reverted_at]`.
- **Neutral names everywhere agents can look.** Anything visible through `run.viewer` or Audit Logs must not say what it is for: the
  injector job is `trace-maintenance-runner`, the fault proxy service is `trace-upstream-gateway`, and their images and env vars use
  neutral names (`OPS_*`, not `FAULT_*`). Conceptual names (Fault Injector, Fault Proxy) live only in code folders and documents.
- **Logs are read through a restricted view, never project-wide.** Create a log bucket `trace-target-logs` with a log view
  `target-only` that admits the target job's logs and the Admin Activity audit entries for the target job, the `trace_target` dataset
  and the target service account. Agents, MCP and the dashboard get `logging.viewAccessor` on that view only (IAM condition on the
  view). Only the human and `ops-automation` may have project-wide `logging.viewer`; otherwise an agent could read the injector's own
  logs. Check the sink and view behave as described before relying on them.
- **Accuracy and confusion numbers are truth-derived.** `evaluation_results` is readable only by `trace-evaluator` and
  `trace-dashboard`; the agent runtime never reads accuracy data, including from earlier campaigns. This is why the dashboard is a
  separate service from `trace-api` (same image, different identity).
- **Leakage audit (TC-32, TC-33).** With each runtime identity's own credentials, try to read `trace_truth`, the injector's logs and
  resource config, and `evaluation_results`, and confirm that only the allowed signals are visible.

---

## 6. Service accounts (least privilege)

Service accounts are free identities, not a billing dimension — GCP does not charge per service account, and having 7 (or
50) of them costs nothing extra on their own. What costs money is *usage* (BigQuery bytes scanned, Cloud Run vCPU-seconds,
API calls) and that all bills to the **one** billing account attached to the project, however many service accounts made
the calls. So 7 service accounts under your single billing account is completely normal and does not multiply cost — it
only narrows *what each identity is allowed to do*, which is a security boundary, not a cost boundary. The only IAM things
that ever cost money are Workload Identity Federation and Secret Manager active secret *versions* past the free allowance
(section 8) — neither scales with the number of service accounts.

| Service account | Used by | Roles |
|---|---|---|
| `trace-etl` | Target ETL job | `bigquery.dataEditor` on `trace_target`; `bigquery.jobUser`; `logging.logWriter` |
| `trace-agents` | `trace-api` (LangGraph orchestrator + agents) | `dataViewer` on `trace_target`; `dataEditor` on `trace_ledger`; `jobUser`; `logging.viewAccessor` on the `target-only` log view (**not** project-wide `logging.viewer`, which would expose injector logs); `run.viewer`; secret accessor for the LLM key only (chat + embeddings). **No** `trace_truth`, **no** `run.developer`, **no** IAM roles |
| `ops-automation` | Fault injector + reverter (`trace-maintenance-runner`, `trace-upstream-gateway`) | Update permission on the target job and the upstream gateway only; `dataOwner` on `trace_target` (schema + IAM faults); `dataEditor` on `trace_truth`; `jobUser`; `logging.logWriter` |
| `trace-evaluator` | Evaluation and memory promotion | `dataViewer` on `trace_ledger`; table-level `dataEditor` on `trace_ledger.incident_memory` **only**; `dataEditor` on `trace_truth`; `jobUser`; secret accessor for the LLM key (embeddings + ablation re-runs) |
| `trace-mcp` | `trace-mcp` service (read-only MCP server) | `dataViewer` on `trace_ledger` and `trace_target`; `jobUser`; `logging.viewAccessor` on the `target-only` view; secret accessor for the MCP token only. **No** `trace_truth`, **no** LLM key, no write roles |
| `trace-dashboard` | `trace-dashboard` service | `dataViewer` on `trace_ledger` and `trace_target`; table-level `dataViewer` on `trace_truth.evaluation_results` **only** (so it can show accuracy without the agents ever seeing truth); `jobUser`; secret accessor for the dashboard token only |
| `trace-scheduler` | Cloud Scheduler triggers (added 2026-10-11) | `run.invoker` on the job each schedule starts, and nothing else: `trace-target-etl` now, `trace-maintenance-runner` when the reverter is scheduled. No project roles, no data access |
| `github-deployer` (via WIF) | GitHub Actions | `run.developer`, `artifactregistry.writer`, `iam.serviceAccountUser` on the six runtime accounts above only; `bigquery.jobUser` plus `bigquery.dataEditor` on `trace_ledger_dev` **only**, so the Phase 0 acceptance run can do `SELECT 1` and one ledger insert (added 2026-10-11). **No** role on the prod `trace_ledger`, on `trace_target` or on `trace_truth` |

Six runtime accounts plus the deployer, all free. Pub/Sub push subscriptions authenticate with OIDC as a dedicated invoker identity
that holds `run.invoker` on `trace-api` only.

---

## 7. Cloud-only development workflow

**Dev surface (choose in Phase 0):**
- **A. Google Cloud Shell + Claude Code CLI (recommended).** Runs in the browser, already authenticated to the project, has `gcloud`
  and `bq`. Free with a 50 h/week quota and a 5 GB persistent `$HOME`; the VM is discarded after an hour idle but `$HOME` persists.
  Install Claude Code per the current official docs and sign in with the Claude account.
- **B. Claude Code on the web + GitHub.** Fine for writing code, but its sandbox may not reach Google APIs. If so, only GitHub Actions
  touches GCP (write code -> push -> Actions deploys and runs cloud tests -> read the logs). Test with a "hello BigQuery" job first.
- **C. GitHub Codespaces.** Works, but it is another quota to manage.

**Build and deploy:** GitHub Actions builds images (free on public repos), pushes to Artifact Registry, then `gcloud run jobs deploy` /
`gcloud run deploy`. Use path filters so a docs change does not rebuild images. Tests (`pytest`) also run in Actions.
One `api` image is built once and deployed as three services (`trace-api`, `trace-dashboard`, `trace-mcp`), each with its own service
account and entrypoint. CI prints the image size and fails if the retained images would pass about 400 MB (a budget, not a measured value).
Use `TRACE_ENV=dev|prod` with a `-dev` dataset suffix and a separate dev job so development never breaks the live pipeline.

**Repo layout (suggested):**
```
etl/            target pipeline (main.py, transform.py, load.py, config.py) + tests/
faultproxy/     small FastAPI service in front of Open-Meteo
injector/       fault catalog, injector, reverter, campaign runner
api/            ONE image, three entrypoints (deployed as three Cloud Run services):
                  trace-api       routes, orchestrator/ (LangGraph), agents/, tools/, memory/ (embeddings + retrieval), llm.py, bq.py
                  trace-dashboard dashboard/ (Jinja2 templates + Chart.js)
                  trace-mcp       mcp_server/ (FastMCP, read-only tools)
sql/            DDL, views, migrations (numbered, idempotent)
infra/          gcloud / IaC scripts (idempotent, re-runnable)
tests/          unit + cloud integration tests, TC-to-test mapping
reference/      aqi_hourly_colab.py, aqi_pollution_days_colab.py (originals, read-only)
CLAUDE.md
.github/workflows/
```

---

## 8. Free-tier guardrails (verify these against the current pricing page before relying on them — free tiers change)

- **BigQuery:** 1 TiB queries/month and 10 GiB storage free. Batch loads are free; streaming inserts are billed (about $0.01 per 200 MB).
  Every query has `maximum_bytes_billed`. Never `SELECT *` without a partition filter.
- **Cloud Run:** 2M requests/month free (per billing account); vCPU/GiB-second allowances also apply for jobs (check the pricing page).
  `min-instances=0`, `max-instances=2`, jobs with `--max-retries=0`.
- **Pub/Sub:** confirm the current free allowance before relying on it. Our volume is a few KB per event, so cost is negligible either way.
- **Cloud Scheduler:** about 3 jobs are free per billing account; extra jobs cost cents per month. Design for 3: ETL, reverter, watchdog on Scheduler.
  The injector's live-demo schedule runs from a GitHub Actions cron, and campaigns run via `workflow_dispatch`. Ask the human before adding a 4th job.
- **Secret Manager:** about 6 active secret versions free; **old versions keep billing**, so destroy superseded versions.
- **Artifact Registry:** about 0.5 GB free. Set a cleanup policy (keep the last 2-3 images per repo) and use slim images.
- **Cloud Build:** avoid it; build in GitHub Actions. (About 120 build-minutes/day is free if ever used.)
- **Budget alert at $1** (Cloud Billing -> Budgets). Alerts only email; they do **not** stop spend. Also keep an `infra/kill-switch` script
  that pauses all Scheduler jobs and scales services to zero.
- Confirm whether the project has billing attached (trial credits expire after a set period; Always Free continues). Streaming inserts
  and some APIs need billing enabled.
- **Gemini API chat is the binding constraint**, not BigQuery or Cloud Run. Exact RPM/RPD are visible only in AI Studio; record them in Phase 0.
  Campaigns are resumable and stop at `LLM_DAILY_CALL_CAP`, continuing the next day. Embeddings need only a few hundred calls in total; confirm
  their limit and free status in AI Studio too.
- **Cloud Run vCPU-seconds** (about 180,000/month free): track consumption. Services scale to zero; never run anything always-on.
- **Public endpoints** (`trace-dashboard`, `trace-mcp`) are gated by bearer tokens (constant-time compare before any query), every query uses
  `maximum_bytes_billed`, and `max-instances` is 1.
- **Cloud Logging:** 30-day retention bounds the ablation window; stay inside the free ingestion allowance.
- **Secret Manager:** 4 secrets (Gemini key, Groq key, dashboard token, MCP token), one active version each, within the ~6 free versions.
- **Artifact Registry:** one image deployed as three services keeps storage under the ~0.5 GB free allowance; cleanup policy applies.

---

## 9. Roadmap

Tick boxes as work completes and add dated notes to the decision log at the bottom. Each phase has an **acceptance** check.

### Phase 0: Cloud workspace and guardrails (no application code)
- [x] Pick the dev surface (section 7) and run a smoke test showing it can reach BigQuery
- [x] Confirm project ID, billing status, and region; write them to `infra/config.env` (no secrets)
- [x] $1 budget alert; write `infra/kill-switch`
- [x] Enable only the needed APIs: BigQuery, Cloud Run, Cloud Scheduler, Pub/Sub, Secret Manager, Artifact Registry, Cloud Logging, IAM
- [ ] Create the six runtime service accounts (section 6) with exactly those roles, plus the Pub/Sub OIDC invoker identity
  (accounts and all grants on existing resources done 2026-10-11; still owed, with the resources they attach to: `ops-automation` update rights on the target job in Phase 3, `run.invoker` on `trace-api` in Phase 4)
- [x] Restricted Cloud Logging bucket, `target-only` log view and sink (section 5.3); grant `logging.viewAccessor` on the view only
  (granted to `trace-agents` only; `trace-mcp` and `trace-dashboard` get it when those services are built)
- [ ] Record in the decision log: Gemini chat and embedding limits from AI Studio, Pub/Sub and Scheduler free counts, billing status
  (LLM limits and billing status recorded 2026-10-11; the Pub/Sub and Scheduler free counts are NOT yet confirmed from a pricing page)
- Designed, next steps (MCP deferred, rule 10): verify Claude Desktop (Pro) can add a remote custom connector with a Request-header token
- [x] Artifact Registry repo with cleanup policy
- [x] Public GitHub repo, Workload Identity Federation for `github-deployer`, no JSON keys anywhere
- [x] Create datasets `trace_target`, `trace_ledger`, `trace_truth` (+ `-dev`); apply DDL from section 3 via `sql/`
- **Acceptance:** a GitHub Actions run authenticates with no key file and runs `SELECT 1` and a `trace_ledger` insert.
  **Passed 2026-10-11** (run 38132931515 on `phase-0`; the insert goes to `trace_ledger_dev`, see section 6).

### Phase 1: Target system live
- [x] Port the ETL from `reference/aqi_hourly_colab.py` (section 2, items 1-12), containerize, unit-test `transform()`
- [x] Deploy as Cloud Run Job (`--max-retries=0`) and schedule it. The designed production schedule is every 4 h. For the demo the
  job is scheduled every 10 minutes temporarily, three consecutive scheduled runs are accepted, and the schedule is then paused
- **Acceptance:** 3 consecutive scheduled runs end `SUCCESS`; re-running inserts 0 duplicate rows; queries stay under the byte cap;
  `pipeline_runs` has START and END rows. (Baseline for TC-15.)
  **Passed 2026-10-11** on the temporary 10-minute schedule (executions `trace-target-etl-xl7nk`, `-6kd9z`, `-pdcc8`); the schedule is now paused.

### Phase 2: Detection and incident ledger (designed, next steps: skipped for the demo)
The demo's campaign runner calls `trace-api` directly (4.1), so the log sink, Pub/Sub push and watchdog are not built now. The
structured-log helper and the `incident_state` view are built in Phase 4 where the agents need them.
- [ ] Structured-log helper shared by all services
- [ ] Log sink -> Pub/Sub push subscription -> `trace-api /pubsub/incidents` with OIDC auth (`run.invoker` on `trace-api` only), 600 s ack deadline,
  processed synchronously (Cloud Run throttles CPU after the response); freshness watchdog; dedupe by execution name
- [ ] `incident_events` + `incident_state` view
- **Acceptance:** a deliberately failed run on the dev job opens exactly one incident; a healthy run opens none.

### Phase 3: Fault injector and ground truth
- [ ] Injector job named `trace-maintenance-runner` with the four faults in 5.1, `fault_ledger` written **before** injection, reverter + dead-man's switch
- [ ] Fixed injection list (3 of each fault, 12 in total) with a logged seed, decided before any run
- [ ] Plan for the human's approval: impersonation of `ops-automation` and `trace-evaluator` and invoke rights on `trace-api` for the campaign runner (5.2)
- [ ] Verify each fault's observable signature, record the real error text in the decision log, and tell the human if two faults are confusable
- Designed, next steps: `trace-upstream-gateway` and the faults that need it; `split` assignment (memory | heldout); control runs
- **Acceptance:** each of the four faults produces a distinct signature; a crashed injector is auto-reverted; agents' service account gets 403 on `trace_truth`;
  leakage audit TC-32 and TC-33 pass (no injector names or truth in anything the agents can read).

### Phase 4: Agents (one at a time, each with fixture-based unit tests plus one live cloud test)
- [ ] 4a Triage (rule-based, no LLM)
- [ ] 4b Root-Cause (tools + closed four-category taxonomy; Groq `openai/gpt-oss-120b`, low reasoning effort, one call per incident)
- [ ] 4c Remediation (rule-based: the catalog's standard fix as a ledger write only; IAM test proving writes to the target fail with 403)
- [ ] 4d Postmortem (Gemini 2.5 Flash, Groq fallback when the daily quota is exhausted; the model used is recorded)
- [ ] 4e Thin LangGraph orchestrator (section 4.1) over Pub/Sub push with idempotency-key skips and the `degraded` route (redeliver a message, confirm no duplicate verdicts)
- Designed, next steps: 4f embeddings client and `get_similar_past_incidents` (TC-30); the `unknown` behaviour (TC-22)
- **Acceptance:** TC-01 to TC-09, TC-20, TC-26, TC-27.

### Phase 5: Evaluation and campaign
- [ ] Deterministic category match run as `trace-evaluator` after the orchestrator finishes; accuracy and a 4x4 confusion matrix. No LLM
- [ ] Views in `trace_truth` for Looker Studio, readable by the human owner and `trace-evaluator` only (agents must never read them):
  an incident view with one row per incident (incident id, failure time, failed run status, the log excerpt the agent saw, verdict
  category, confidence, true cause, match true/false, postmortem text, the model that wrote it), and a summary view with accuracy
  and the 4x4 confusion matrix. The human connects Looker Studio to them with their own account
- [ ] One campaign runner script (5.2) that works through the fixed injection list of 12, a different identity per step, stops at the LLM caps and can resume
- **Accuracy target: 80-90% on the four-way classification. A goal, not a pass/fail gate.** Report the number that comes out.
  Never leak ground truth to the agents and never reselect incidents after seeing results (5.1).
- Designed, next steps: time-to-diagnosis, false-positive / false-negative rates (TC-18), partial-data detection (TC-19), memory promotion and the
  memory on/off ablation (TC-31)
- **Acceptance:** TC-16 and TC-17 with real numbers from the 12 labelled incidents, every one of them scored.

### Phase 6: Dashboard and access (designed, next steps: not built for the demo)
The demo shows Looker Studio over the BigQuery views from Phase 5 instead. The items below stay as the design.
- [ ] **Frontend decision (updated 2026-10-09): server-rendered, no separate frontend platform, but its own service.** `trace-dashboard`
  (same image as `trace-api`, own entrypoint, runs as `trace-dashboard`) serves Jinja2 templates with Chart.js from a CDN. It is separate from
  `trace-api` so the dashboard identity can read truth-derived accuracy (`evaluation_results` only) while the agent identity never can.
  No React/Next.js, no separate hosting, no build step.
- [ ] Live incident timeline, verdicts, accuracy trend, served by `trace-dashboard` (templates in `api/dashboard/`)
- [ ] Dashboard endpoints: `/incidents`, `/verdicts`, `/accuracy-trend` — query BigQuery with `maximum_bytes_billed`, return data the templates render
- [ ] Bearer-token access (token in Secret Manager, constant-time compare), Admin vs Viewer roles
- [ ] 6b MCP server `trace-mcp` (section 4.3): first a hello-tool smoke test added in Claude via Customize > Connectors with the token under Request headers (beta; if the field is absent follow the 4.3 ladder)
  (the human performs this step), then the read-only tools; no SQL tool, no truth access
- **Acceptance:** TC-09, TC-25, TC-28, TC-29 and TC-33; dashboard loads in under 3 s from another network.

### Phase 7: Hardening and open-source release
- [ ] Concurrency test (TC-23), alert-budget test (TC-21), CI end-to-end (TC-24)
- [ ] README with cost report, threat model, architecture diagram (updated), attribution; LICENSE
- [ ] Update the SDD, architecture diagram, and test-case doc (see "Doc drift")
- **Acceptance:** a stranger can clone the repo and deploy from the README on a fresh free-tier project.

---

## 10. Doc drift to fix in Phase 7
- SDD, diagram, and slides still mention Oracle VM, PostgreSQL, Redis, Celery, Docker SDK / K8s API. Replace with the table in section 1.
- SDD schema had `incidents.injected_fault_type`; ground truth now lives in `trace_truth.fault_ledger`.
- TC-11 said "invalid API key" for the old data.gov.in pipeline. Open-Meteo (the live target) needs no key, so the fault
  is IAM revocation instead. Reword the test.
- Add the closed root-cause taxonomy and the memory ablation to the SDD.
- Add TC-26 to TC-33 (orchestrator, MCP, embedding memory, leakage audit) to the Test Case Document; update the SDD for LangGraph, embedding memory,
  the MCP server, the separate dashboard service and the restricted log view.
- Diagram resource names are conceptual (`trace-fault-proxy`); the real names are the neutral `trace-upstream-gateway` and `trace-maintenance-runner`.
- Review II deck corrections: slide 4 Triage line should read "Rates severity (Low–Critical) from run status, blast radius and staleness";
  slide 5 "7 catalogued ways" should be 8 (plus 1 held-out and a control); slide 5 should gain an Evaluation Agent bullet.
- Drop n8n from any doc that mentions it; it is out of scope.

## 11. Open decisions for the human (ask in the first session)

**Resolved 2026-10-09:**
6. **Scope additions:** LangGraph orchestrator, embedding memory and a read-only MCP server are in. n8n is out (Cloud Run cannot reach a laptop
   without a tunnel, always-on Cloud Run would exhaust the free vCPU-seconds in about 2 days, and a free VM is too tight). Rules 9 and 10 apply.

**Resolved 2026-09-28:**
1. **GCP project ID:** `project-66f56fc9-9de6-4cfd-8cf`, owned by the project owner's Google account. Whoever runs
   Phase 0 must `gcloud auth login` (in Cloud Shell, this is automatic) as a principal with Owner/Editor on this project —
   if the dev surface is Claude Code running as a different identity, it has **no** access to this project until the human
   (signed in as the project owner) either does the `gcloud` steps themselves interactively, or grants the acting
   identity a role on the project first. First Phase-0 step must be `gcloud config list` + `gcloud projects describe
   project-66f56fc9-9de6-4cfd-8cf` to confirm access before doing anything else. Billing-attachment status still unconfirmed —
   check `gcloud billing projects describe project-66f56fc9-9de6-4cfd-8cf` in Phase 0 (Always Free requires a billing account
   on file even at $0 spend).
2. **Region:** `us-central1` (default accepted).
3. **GitHub repo:** public, named `TRACE`, **no license file**. Note the trade-off: "no license" on a public repo does not
   mean open source — under default copyright law it means all rights reserved, so legally nobody (including a future
   employer or the mentor) may copy, modify, fork-and-reuse, or redistribute the code without asking you first. GitHub will
   itself show a "no license" banner on the repo page. This is fine and common for a portfolio/academic project where you
   want the code visible for hirers to read but not to be reused — it just means the earlier "fully open source" framing
   from the original project brief no longer applies unless a license is added later. Nothing to build differently either
   way; only affects the README and the repo's license field.

**Resolved 2026-09-28:**
4. **LLM key: Gemini 2.5 Flash (free tier, Google AI Studio), confirmed as primary.**
   Honest read on whether it's "good enough": for what this project actually asks the LLM to do, yes.
   - The architecture is deliberately built to not need a frontier model. Root-Cause classifies against a **fixed 9-category
     taxonomy** (not open-ended free text), from **structured JSON logs** (not messy natural language), with tool calls that
     fetch specific, well-labeled evidence. That is a constrained extraction/classification task, which is exactly where a
     mid-tier fast model like Flash performs close to a frontier model — the gap between Flash and a top model widens on
     open-ended reasoning and long-horizon planning, not on "given these 6 labeled fields, pick 1 of 9 categories."
   - Flash-Lite is cheaper/faster but noticeably weaker at instruction-following under load; plain **2.5 Flash** is the
     better default for the agents that produce verdicts (Root-Cause, Remediation), and Flash-Lite is fine for the cheaper,
     Triage step (rating severity Low–Critical from run status, blast radius and staleness).
   - Where it could matter: your own accuracy number (the 70% target) will reflect **both** the agent design and the model's
     ceiling — if the campaign lands well under 70%, that is a real, useful finding for the postmortem/eval writeup either
     way ("here's the model's ceiling on this taxonomy, here's what memory context bought us"), not a failure of the
     project. That is genuinely defensible in review: a mentor is unlikely to expect a free-tier model to hit frontier
     numbers, and the ablation study's *shape* (does memory help, does it help more on some fault types than others)
     matters more to the grade than the raw accuracy digit.
   - If accuracy turns out to be the bottleneck rather than the agent design, the fallback isn't Groq for quality — it's
     worth budgeting a few dollars for a pay-as-you-go Claude or GPT key later in the semester, since at this project's
     call volume (a few hundred LLM calls total across development, plus one ~60-incident campaign) the real-money cost
     would likely be low single-digit dollars, not something that needs the free-tier constraint to hold at all costs. Not
     needed to start — just don't rule it out later if Gemini's ceiling turns out to be the limiting factor.
   - **Groq** stays as documented fallback for throughput only (fast during a big campaign burst), not for accuracy.
   - Get the key from https://aistudio.google.com/apikey — independent of the GCP project login, no card required.
     Verify the exact current RPM/TPM/RPD numbers in-console at the start of Phase 0 (these drift) and log them below.
5. **Campaign size and accuracy threshold:** recommend **60 labelled incidents** for the first real campaign (roadmap's
   Phase 5 floor is 50; 60 gives slack for a handful of indistinguishable/pruned faults from Phase 3 without dropping below
   50) and a **70% root-cause exact-match accuracy** target against the 9-category closed taxonomy (including `unknown`).
   70% is deliberately not a moonshot: it is high enough to demonstrate the memory-ablation study is meaningful (a gap
   between the "memory on" and "heldout" conditions is only interpretable if neither is near-perfect or near-random), and
   realistic for a single-project, single-mentor timeline. Confirm this number with the mentor before running the real
   campaign, per Phase 5's own acceptance check — a mentor-agreed threshold carries more weight in review than a
   self-picked one.

## 12. Conventions
- Python 3.12, type hints, `ruff` + `pytest` in CI, small PRs, one phase per branch.
- SQL migrations are numbered and idempotent (`CREATE ... IF NOT EXISTS`).
- Scripts under `infra/` are safe to re-run. Print what they will change and ask before mutating IAM or deleting.
- Every test case (TC-xx) maps to at least one automated test; keep that mapping in `tests/README.md`.
- Never log secrets. Never print API keys in Actions output.

## 13. First session checklist for Claude Code
1. Read this file fully. Run `gcloud config list` and `gcloud auth list`; report what you see.
2. Ask the human the open decisions in section 11.
3. Begin Phase 0. Do not write application code yet.
4. After each completed item, tick its box here and add a line to the decision log.

## Decision log
<!-- Append: YYYY-MM-DD - decision - reason -->
2026-09-28 - Reuse existing project `project-66f56fc9-9de6-4cfd-8cf` (the human's own account) instead of a fresh project - human already has it provisioned; avoids re-doing API enablement.
2026-09-28 - Region set to us-central1 - default, cheapest/most free-tier-friendly, no reason given to deviate.
2026-09-28 - Repo will be public - human confirmed; open-source goal from the original project brief.
2026-09-28 - Primary LLM provider: Gemini 2.5 Flash free tier (AI Studio) for Root-Cause/Remediation, Flash-Lite acceptable for Triage; Groq kept as documented throughput-only fallback - closed 9-category taxonomy + structured logs is a constrained classification task where Flash's ceiling is close to a frontier model's; free, no card needed. Real-money fallback (a few dollars of pay-as-you-go Claude/GPT) noted as an option later if accuracy, not agent design, turns out to be the bottleneck.
2026-09-28 - Target: 60 labelled incidents, 70% root-cause exact-match accuracy for the first campaign - gives slack above the 50-incident floor and sets a threshold meaningful for the memory-ablation comparison; to be confirmed with the mentor before the real campaign run (not yet mentor-confirmed as of this entry).
2026-09-28 - GitHub repo: public, name `TRACE`, no license file (all-rights-reserved by default) - human's explicit choice; supersedes the "fully open source" framing from the original project brief unless a license is added later.
2026-09-28 - Confirmed: 5 service accounts under 1 billing account causes no additional charges - service accounts are free IAM identities; only usage (BQ bytes, Cloud Run seconds, WIF, Secret Manager versions past free allowance) bills, and it all bills to the same single billing account regardless of which service account made the call.
2026-09-29 - Dashboard frontend: server-rendered from trace-api (FastAPI + Jinja2 + Chart.js via CDN), no separate frontend platform - avoids adding a 6th service, a second deploy pipeline, and another free-tier limit to track for a dashboard that only needs a timeline, a table, and one or two trend charts. (Superseded 2026-10-09: now the separate trace-dashboard service, same stack.)
2026-10-09 - Added LangGraph orchestrator, Gemini-API embedding memory (BigQuery VECTOR_SEARCH, not ML.GENERATE_EMBEDDING which bills via Vertex) and a read-only remote MCP server; n8n excluded - the three fit the free tier and cloud-only rule, n8n does not.
2026-10-09 - Dashboard moved to its own service `trace-dashboard` with its own SA that can read `evaluation_results` only - the agent identity must stay barred from truth.
2026-10-09 - Logs read through restricted `target-only` log view with `logging.viewAccessor`; injector resources renamed neutrally (`trace-upstream-gateway`, `trace-maintenance-runner`) - prevents leaking fault purpose to agents.
2026-10-09 - Designed for 3 Cloud Scheduler jobs (ETL, reverter, watchdog); injector schedule via GitHub Actions cron, campaigns via workflow_dispatch - stays within the free job count.
2026-10-09 - Service accounts now 6 runtime + deployer (adds trace-mcp, trace-dashboard) - still free; evaluator gets table-level write on incident_memory only.
2026-10-10 - Checked in the human's Claude (Pro) Add custom connector dialog: the **Request headers** section is present and `authorization` is in the header-name list (up to four headers). Bearer-token design for `trace-mcp` is viable; remember to set Authentication to No sign-in and enter the value as `Bearer <token>`. Still to confirm with a real hello-tool server.
2026-10-10 - Project's full title is "Multi-Agent Incident Investigation & Root-Cause Analysis System"; TRACE is the short name. Use this title consistently in README, SDD, slides and resume.
2026-10-10 - FINAL: TRACE expands to "Triage and Root-cause Analysis with Coordinated Evaluation agents"; full project title remains "Multi-Agent Incident Investigation & Root-Cause Analysis System". Use both consistently (README, SDD, slides, resume).
2026-10-10 - ETL source confirmed by the human: github.com/vaibhavidhumal4/Air-Quality-ETL-Pipeline (data_pipeline.py). Fetched summary matches `reference/aqi_hourly_colab.py` (Open-Meteo, 36 states, staging + NOT EXISTS dedupe on (state, ts)); diff the two files in Phase 1 before porting. Replace `google.colab` auth with the `trace-etl` service account. Embeddings and MCP are now OPTIONAL: ask the human at the start of Phase 5 (embeddings) and Phase 6 (MCP) with the with/without impact before building either.
2026-10-11 - Dev surface: Google Cloud Shell + Claude Code CLI (option A). `gcloud` is authenticated as the project owner; project `project-66f56fc9-9de6-4cfd-8cf` (number 616734266046) reachable; `SELECT 1` in us-central1 under a 100 MB byte cap succeeded.
2026-10-11 - Billing status: billing IS attached (`billingEnabled: true`). Alerts do not stop spend, so every resource is approved before creation. Values written to `infra/config.env`.
2026-10-11 - Public repo github.com/vaibhavidhumal4/TRACE created (README + .gitignore on `main`, no license file). Phase 0 work is on branch `phase-0`.
2026-10-11 - Dev datasets use the suffix `_dev` (e.g. `trace_target_dev`), not `-dev` - BigQuery dataset names cannot contain hyphens.
2026-10-11 - Pre-existing dataset `air_quality` found in the project (not created by TRACE); left untouched.
2026-10-11 - APIs enabled (human approved): Cloud Run, Cloud Scheduler, Pub/Sub, Secret Manager, Artifact Registry, IAM, plus four the checklist did not name: IAM Credentials and STS (needed by Workload Identity Federation), Cloud Resource Manager (project IAM bindings), Billing Budgets (budget alert). BigQuery and Cloud Logging were already on.
2026-10-11 - Budget alert `trace-monthly-guardrail` created, scoped to this project, thresholds 50/90/100%. The billing account is in INR, so the "$1" budget is 85 INR (a budget must use the account currency). `infra/kill-switch` written; verified only against an empty project, so re-test it once the first Scheduler job and Cloud Run service exist.
2026-10-11 - Datasets `trace_target`, `trace_ledger`, `trace_truth` and their `_dev` twins created in us-central1 from `sql/001`-`003` via `infra/apply-sql.sh`. `trace_target*` carry the label `trace-scope=target`. No IAM granted on them yet.
2026-10-11 - `github-deployer` additionally gets `bigquery.jobUser` and `bigquery.dataEditor` on `trace_ledger_dev` only (human approved) - the Phase 0 acceptance run must insert into a ledger and section 6 gave the deployer no BigQuery role; the acceptance insert therefore targets the dev ledger, and the deployer has no access to the prod ledger or to `trace_truth`.
2026-10-11 - `reference/aqi_hourly_colab.py` added: an unmodified copy of `data_pipeline.py` from github.com/vaibhavidhumal4/Air-Quality-ETL-Pipeline at commit f844b29. That repo holds only this one pipeline script; `aqi_pollution_days_colab.py` (the data.gov.in script) is not in it and has not been added. `creds.py` from that repo was deliberately not copied (it holds an API key).
2026-10-11 - Service accounts created (human approved): `trace-etl`, `trace-agents`, `ops-automation`, `trace-evaluator`, `trace-mcp`, `trace-dashboard`, `trace-pubsub-invoker`, `github-deployer`; 31 grants applied by `infra/03-service-accounts.sh --apply` and read back from project, dataset, table and service-account policies. `trace-agents` has no entry on `trace_truth` or `trace_truth_dev`. The Phase 0 service-account box stays unticked until the remaining grants exist: `logging.viewAccessor` on the log view, secret accessors, `artifactregistry.writer`, `ops-automation` update rights on the target job and gateway, `run.invoker` for the Pub/Sub invoker.
2026-10-11 - SCOPE CHANGE for the demo on 2026-10-12 (human decision; rule 10, sections 4, 5.1 and 9 updated). Four faults injected directly on the job, dataset permissions or table (`iam_revoked`, `bq_extra_column`, `task_timeout_kill`, `bad_endpoint_url`), about 5 each, about 20 labelled incidents. Closed four-category taxonomy, no `unknown`, no control runs. Only Root-Cause and Postmortem call an LLM; Triage, Remediation, Evaluation and the orchestrator are rule-based. Report accuracy and a confusion matrix only; target 80-90% as a goal, not a gate. This supersedes the 2026-09-28 target of 60 incidents at 70% and section 11 item 5.
2026-10-11 - Designed, next steps (not built for the demo): upstream gateway and its four faults, `dataset_missing`, `unknown`, control runs, embedding memory and the ablation, MCP server, dashboard service. Test cases deferred with them: TC-15, TC-18, TC-22 (named by the human), and by consequence TC-12, TC-13, TC-19 (their faults are deferred), TC-30, TC-31 (memory), TC-25, TC-28, TC-29 (dashboard and MCP).
2026-10-11 - Evaluation stays a separate step run as `trace-evaluator` after the orchestrator's four stages, and counts as the sixth component - the orchestrator runs as `trace-agents`, which must never reach `trace_truth`, so it cannot invoke the scoring itself.
2026-10-11 - Models: Root-Cause on Groq `openai/gpt-oss-120b` with low reasoning effort and no fallback, so every accuracy number comes from one model. Postmortem on Gemini 2.5 Flash, falling back to Groq `openai/gpt-oss-120b` when the daily Gemini quota is exhausted; the model that wrote each postmortem is recorded. Supersedes the 2026-09-28 choice of Gemini for Root-Cause.
2026-10-11 - Free-tier limits as given by the human (from the provider consoles). Gemini 2.5 Flash: 5 RPM / 250K TPM / 20 RPD. Gemini 2.5 Flash-Lite: 10 RPM / 250K TPM / 20 RPD; other Gemini models also 20 RPD. Gemini embedding: 100 RPM / 30K TPM / 1K RPD. Groq `openai/gpt-oss-120b`: 30 RPM / 8K TPM / 1K RPD / 200K TPD. Consequences: about 20 postmortems uses the whole Gemini day (hence the Groq fallback), and at about 3K tokens a prompt Groq allows two Root-Cause calls a minute and roughly 60 a day, so Root-Cause makes one LLM call per incident.
2026-10-11 - Standing rules from the human: secret scan before every commit and push; push the `phase-0` branch only, not `main`; show the plan and the cost and wait for approval before any change to IAM, billing, Scheduler, Cloud Run, Pub/Sub or secrets. The dev job is named `trace-target-etl-dev`.
2026-10-11 - Restricted logging applied (human approved): bucket `trace-target-logs` (us-central1, 30 days), sink `trace-target-sink`, view `target-only`, and `logging.viewAccessor` for `trace-agents` only, conditioned on that view. Verified with description-only edits on dev datasets: the `trace_target_dev` dataset patch and a table `ALTER` both arrived in the bucket (both as `resource.type=bigquery_dataset` Admin Activity entries), and the same edit on `trace_ledger_dev` did not. A new sink took under four minutes to start routing; entries made about 40 s after creation were not captured. Cloud Run Job log routing is still to be verified once the job exists (Phase 1). The logging pricing page could not be read with the available tools; applied on the human's figure of a few MB a month against a 50 GiB free allowance.
2026-10-11 - Artifact Registry repo `trace` (Docker, us-central1) created with a cleanup policy that keeps the 2 newest versions of each image and deletes the rest; `github-deployer` has `artifactregistry.writer` on that repo only.
2026-10-11 - Workload Identity Federation: pool `github`, provider `github-actions`, accepting only repository ID 1414002331 and owner ID 64737822 (vaibhavidhumal4/TRACE); only that repository may act as `github-deployer`. No user-managed key exists on any TRACE service account.
2026-10-11 - `trace-agents` granted `secretAccessor` on `gemini-api-key` and `groq-api-key`; no other identity has access. `trace-evaluator` gets no LLM key while embeddings and the ablation are deferred (narrower than section 6, human approved).
2026-10-11 - PHASE 0 ACCEPTANCE PASSED: GitHub Actions run 38132931515 signed in as `github-deployer` with no key file, ran `SELECT 1`, inserted one row into `trace_ledger_dev.incident_events` and read it back.
2026-10-11 - Revised demo plan (human decision): record a real run, not live. ETL on a temporary 10-minute schedule for Phase 1 acceptance, then paused (designed schedule stays every 4 h). One campaign runner does inject -> execute job -> wait for failure -> call `trace-api` -> revert -> score, borrowing `ops-automation`, `trace-agents` (inside `trace-api`) and `trace-evaluator` per step. 12 incidents, 3 of each fault, seed logged before the run, no reselection, every incident scored. Pub/Sub trigger and Phase 2 detection deferred. Results shown in Looker Studio over views in `trace_truth`. Order: Phase 1, then 3, 4, 5.
2026-10-11 - The human's pasted messages in this project are their own instructions (stated 2026-10-11).
2026-10-11 - Phase 1 deployed (human approved): workflow `etl-deploy` builds the ETL image in GitHub Actions, pushes it to Artifact Registry and deploys Cloud Run jobs `trace-target-etl` (dataset `trace_target`) and `trace-target-etl-dev` (`trace_target_dev`), both as `trace-etl`, `--max-retries=0`, 900 s task timeout, 1 vCPU / 512 MB, label `trace-scope=target`. Compressed image 153 MB.
2026-10-11 - Verified: a hand-run execution of the dev job (`trace-target-etl-dev-cgwt5`, running as `trace-etl`) wrote its JSON logs, the container exit line and the Cloud Run system event into the restricted bucket, readable through the `target-only` view. An execution takes about 2 minutes end to end, of which the pipeline itself is about 10 seconds.
2026-10-11 - New service account `trace-scheduler` (human approved; not in the original section 6): `run.invoker` on `trace-target-etl` only, used by Cloud Scheduler job `trace-target-etl-schedule`. Scheduler job count is now 1.
2026-10-11 - PHASE 1 ACCEPTANCE PASSED on the temporary `*/10 * * * *` schedule: three consecutive scheduled executions ended SUCCESS with 36/36 states; the first inserted 1512 rows and the next two inserted 0; 1512 rows = 1512 distinct (state, ts); every run has START and END rows; the largest query billed 20 MB against the 100 MB cap. The schedule was then PAUSED. The designed production schedule is `0 */4 * * *` (in `infra/config.env`).
2026-10-11 - ETL defaults chosen: `HTTP_TIMEOUT_S=20`, `MIN_STATES_OK=30`, five tries with 1/2/4/8 s backoff (no sleep after the last try). The staging table is named per run and expires after one hour, so a killed run leaves nothing behind for long. Each idempotent insert bills 20 MB (the 10 MB minimum for each of two tables).
2026-10-11 - Artifact Registry budget risk: the ETL image is 153 MB compressed, so two retained versions are 306 MB before the maintenance and API images exist; three images at two versions each would pass the roughly 0.5 GB free allowance. To be decided with the human: keep one version per image.
