-- Dataset trace_target: the target ETL's data and run ledger.
-- ${SUFFIX} is "" for prod and "_dev" for dev; ${BQ_LOCATION} comes from infra/config.env (see infra/apply-sql.sh).
-- The trace-scope label marks what the maintenance runner is allowed to touch (CLAUDE.md 5.2).

CREATE SCHEMA IF NOT EXISTS `trace_target${SUFFIX}`
OPTIONS (location = '${BQ_LOCATION}', labels = [('trace-scope', 'target')]);

CREATE TABLE IF NOT EXISTS `trace_target${SUFFIX}.aqi_hourly` (
  state STRING, ts DATETIME, pm25 FLOAT64, pm10 FLOAT64, no2 FLOAT64, so2 FLOAT64,
  co FLOAT64, o3 FLOAT64, us_aqi FLOAT64, aqi_category STRING, ingested_at_ist DATETIME
) PARTITION BY DATE(ts) CLUSTER BY state OPTIONS (partition_expiration_days = 90);

CREATE TABLE IF NOT EXISTS `trace_target${SUFFIX}.pipeline_runs` (
  run_id STRING, execution_name STRING, phase STRING,      -- START | END
  status STRING,                                            -- RUNNING | SUCCESS | PARTIAL | FAILED
  ts TIMESTAMP, states_total INT64, states_ok INT64, rows_inserted INT64,
  error_type STRING, error_message STRING, config JSON      -- non-secret env config
) PARTITION BY DATE(ts);
