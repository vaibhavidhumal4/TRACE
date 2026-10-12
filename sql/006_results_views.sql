-- Reporting views for Looker Studio and the trace-dashboard service. They live in trace_results (created by hand with
-- a restricted access list), because they join ground truth to the ledger. Agents have no access.
-- One row per PLANNED injection, so an item that was never run or never scored is visible, not missing.

CREATE OR REPLACE VIEW `trace_results${SUFFIX}.incident_results` AS
WITH plan AS (
  SELECT campaign_id, seed, position, fault_type, expected_category, TO_JSON_STRING(params) AS injected_params
  FROM `trace_truth${SUFFIX}.campaign_plan`
  WHERE DATE(planned_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY)
),
faults AS (
  SELECT campaign_id, plan_position, fault_id, execution_name, injected_at, reverted_at
  FROM `trace_truth${SUFFIX}.fault_ledger`
  WHERE DATE(injected_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY) AND plan_position IS NOT NULL
),
scores AS (
  SELECT fault_id, incident_id, predicted_category, is_correct, time_to_diagnosis_sec, evaluated_at
  FROM `trace_truth${SUFFIX}.evaluation_results`
  WHERE DATE(evaluated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY)
),
incidents AS (
  SELECT incident_id, source_execution, detected_at
  FROM `trace_ledger${SUFFIX}.incidents`
  WHERE DATE(detected_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY)
),
events AS (
  SELECT incident_id, stage, payload
  FROM `trace_ledger${SUFFIX}.incident_events`
  WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY incident_id, stage ORDER BY created_at) = 1
),
verdicts AS (
  SELECT incident_id, agent_name, root_cause_category, confidence, hypothesis, model
  FROM `trace_ledger${SUFFIX}.agent_verdicts`
  WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY incident_id, agent_name ORDER BY created_at) = 1
)
SELECT
  p.campaign_id,
  p.position,
  i.incident_id,
  TIMESTAMP(JSON_VALUE(win.payload.execution_started_at)) AS failure_time,
  f.execution_name AS failed_execution,
  JSON_VALUE(tri.payload.run_status) AS failed_run_status,
  JSON_VALUE(tri.payload.severity) AS severity,
  (SELECT STRING_AGG(
            CONCAT(JSON_VALUE(l.severity), ' ', COALESCE(JSON_VALUE(l.error_class), JSON_VALUE(l.source), ''), ': ',
                   JSON_VALUE(l.message)), '\n')
   FROM UNNEST(JSON_QUERY_ARRAY(evd.payload.run_logs)) AS l
   WHERE JSON_VALUE(l.severity) IN ('ERROR', 'WARNING')) AS log_excerpt_seen_by_agent,
  rc.root_cause_category AS verdict_category,
  rc.confidence,
  rc.hypothesis AS verdict_hypothesis,
  rc.model AS root_cause_model,
  p.expected_category AS true_category,
  p.fault_type AS true_cause,
  p.injected_params,
  s.is_correct AS match,
  CASE
    WHEN f.fault_id IS NULL THEN 'not run'
    WHEN s.fault_id IS NULL THEN 'not scored'
    WHEN s.is_correct THEN 'correct'
    ELSE 'wrong'
  END AS outcome,
  s.time_to_diagnosis_sec,
  pm.hypothesis AS postmortem_text,
  pm.model AS postmortem_model,
  -- Added for the dashboard (appended, so existing Looker Studio fields keep their positions).
  JSON_VALUE(pme.payload.fallback_reason) AS postmortem_fallback_reason,
  TO_JSON_STRING(tri.payload.reasons) AS triage_reasons,
  TO_JSON_STRING(rem.payload.proposals) AS remediation_proposals,
  SAFE_CAST(JSON_VALUE(rem.payload.executed) AS BOOL) AS remediation_executed,
  p.seed,
  f.injected_at,
  f.reverted_at
FROM plan p
LEFT JOIN faults f ON f.campaign_id = p.campaign_id AND f.plan_position = p.position
LEFT JOIN scores s ON s.fault_id = f.fault_id
LEFT JOIN incidents i ON i.source_execution = f.execution_name
LEFT JOIN events win ON win.incident_id = i.incident_id AND win.stage = 'execution_window'
LEFT JOIN events tri ON tri.incident_id = i.incident_id AND tri.stage = 'triage'
LEFT JOIN events evd ON evd.incident_id = i.incident_id AND evd.stage = 'evidence'
LEFT JOIN verdicts rc ON rc.incident_id = i.incident_id AND rc.agent_name = 'root_cause'
LEFT JOIN verdicts pm ON pm.incident_id = i.incident_id AND pm.agent_name = 'postmortem'
LEFT JOIN events rem ON rem.incident_id = i.incident_id AND rem.stage = 'remediation'
LEFT JOIN events pme ON pme.incident_id = i.incident_id AND pme.stage = 'postmortem';

CREATE OR REPLACE VIEW `trace_results${SUFFIX}.campaign_summary` AS
SELECT
  campaign_id,
  COUNT(*) AS planned,
  COUNTIF(outcome != 'not run') AS run,
  COUNTIF(outcome IN ('correct', 'wrong')) AS scored,
  COUNTIF(outcome = 'correct') AS correct,
  SAFE_DIVIDE(COUNTIF(outcome = 'correct'), COUNTIF(outcome IN ('correct', 'wrong'))) AS accuracy,
  SAFE_DIVIDE(COUNTIF(outcome = 'correct'), COUNT(*)) AS accuracy_over_all_planned,
  MIN(failure_time) AS first_failure,
  MAX(failure_time) AS last_failure
FROM `trace_results${SUFFIX}.incident_results`
GROUP BY campaign_id;

-- All 4 x 5 cells for every campaign, including zeros: true category by predicted category (or no verdict).
CREATE OR REPLACE VIEW `trace_results${SUFFIX}.confusion_matrix` AS
WITH categories AS (
  SELECT c FROM UNNEST(['authorization_failure', 'warehouse_schema_mismatch', 'resource_limit_exceeded',
                        'dns_or_endpoint_unreachable']) AS c
),
predicted AS (SELECT c FROM categories UNION ALL SELECT 'no_verdict'),
campaigns AS (SELECT DISTINCT campaign_id FROM `trace_results${SUFFIX}.incident_results`),
scored AS (
  SELECT campaign_id, true_category, COALESCE(verdict_category, 'no_verdict') AS predicted_category
  FROM `trace_results${SUFFIX}.incident_results`
  WHERE outcome IN ('correct', 'wrong')
)
SELECT
  k.campaign_id,
  t.c AS true_category,
  p.c AS predicted_category,
  COUNTIF(s.campaign_id IS NOT NULL) AS incidents
FROM campaigns k
CROSS JOIN categories t
CROSS JOIN predicted p
LEFT JOIN scored s
  ON s.campaign_id = k.campaign_id AND s.true_category = t.c AND s.predicted_category = p.c
GROUP BY k.campaign_id, t.c, p.c;
