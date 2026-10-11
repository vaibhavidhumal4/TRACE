-- The fixed, ordered list of injections for a campaign, written with its seed BEFORE the first injection.
-- It is the record that no incident was reselected after results were seen. Lives in trace_truth: agents have no access.

CREATE TABLE IF NOT EXISTS `trace_truth${SUFFIX}.campaign_plan` (
  campaign_id STRING, seed INT64, position INT64, fault_type STRING, expected_category STRING,
  params JSON, planned_at TIMESTAMP
) PARTITION BY DATE(planned_at);
