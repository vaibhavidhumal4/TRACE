-- Dataset trace_truth: ground truth and evaluation results. Agents have NO access; only the injector and evaluator
-- (plus table-level read on evaluation_results for the dashboard identity).

CREATE SCHEMA IF NOT EXISTS `trace_truth${SUFFIX}`
OPTIONS (location = '${BQ_LOCATION}');

CREATE TABLE IF NOT EXISTS `trace_truth${SUFFIX}.fault_ledger` (
  fault_id STRING, fault_type STRING, expected_category STRING, injected_at TIMESTAMP,
  revert_at TIMESTAMP, reverted_at TIMESTAMP, params JSON, seed INT64,
  campaign_id STRING, revert_plan JSON,
  split STRING                                              -- memory (training) | heldout; assigned at campaign planning
) PARTITION BY DATE(injected_at);

CREATE TABLE IF NOT EXISTS `trace_truth${SUFFIX}.evaluation_results` (
  eval_id STRING, incident_id STRING, fault_id STRING, predicted_category STRING,
  true_category STRING, is_correct BOOL, confidence FLOAT64,
  time_to_diagnosis_sec INT64, split STRING, evaluated_at TIMESTAMP   -- split: memory (training) | heldout
) PARTITION BY DATE(evaluated_at);
