-- Links each ledger row to its place in the campaign plan and to the execution that ran under it, so a campaign can
-- resume and so every planned item can be shown, scored or not.
ALTER TABLE `trace_truth${SUFFIX}.fault_ledger` ADD COLUMN IF NOT EXISTS plan_position INT64;
ALTER TABLE `trace_truth${SUFFIX}.fault_ledger` ADD COLUMN IF NOT EXISTS execution_name STRING;
