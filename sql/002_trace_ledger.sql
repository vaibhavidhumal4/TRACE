-- Dataset trace_ledger: incidents, events, verdicts and embedding memory. Agents can read and write here.

CREATE SCHEMA IF NOT EXISTS `trace_ledger${SUFFIX}`
OPTIONS (location = '${BQ_LOCATION}');

CREATE TABLE IF NOT EXISTS `trace_ledger${SUFFIX}.incidents` (
  incident_id STRING, detected_at TIMESTAMP, detector STRING, source_run_id STRING,
  source_execution STRING, severity STRING, status STRING, dedup_key STRING
) PARTITION BY DATE(detected_at);

CREATE TABLE IF NOT EXISTS `trace_ledger${SUFFIX}.incident_events` (   -- append-only; latest row per incident = state
  event_id STRING, incident_id STRING, stage STRING, agent STRING, created_at TIMESTAMP,
  idempotency_key STRING,                                   -- '{incident_id}:{stage}'
  payload JSON
) PARTITION BY DATE(created_at) CLUSTER BY incident_id;

CREATE TABLE IF NOT EXISTS `trace_ledger${SUFFIX}.agent_verdicts` (
  verdict_id STRING, incident_id STRING, agent_name STRING, created_at TIMESTAMP,
  root_cause_category STRING, hypothesis STRING, confidence FLOAT64,
  evidence JSON, proposals JSON, model STRING, llm_calls INT64, latency_ms INT64, memory_enabled BOOL
) PARTITION BY DATE(created_at) CLUSTER BY incident_id;

-- Embedding memory. Written ONLY by the evaluator, for training-split incidents, after they have been evaluated.
-- Not partitioned: it stays tiny (about 100 rows x 768 floats, under 1 MB), so a full scan is negligible.
-- Never mix embedding models or dimensions in this table. Changing either means re-embedding every row.
CREATE TABLE IF NOT EXISTS `trace_ledger${SUFFIX}.incident_memory` (
  memory_id STRING, incident_id STRING, promoted_at TIMESTAMP,
  evidence_text STRING,              -- sanitized text that was embedded; never contains a fault name or truth-derived text
  root_cause_category STRING,        -- confirmed true category, copied from trace_truth by the evaluator (training split only)
  resolution_summary STRING,         -- standard remedy text for that category from the fault catalog
  embedding ARRAY<FLOAT64>,          -- one fixed length for the lifetime of the table
  embedding_model STRING, embedding_dim INT64
);
