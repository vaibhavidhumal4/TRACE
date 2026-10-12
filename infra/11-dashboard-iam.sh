#!/usr/bin/env bash
# Access for the trace-dashboard service (decision 2026-10-12). Nothing here costs money. Safe to re-run.
#
# The dashboard identity reads the reporting views in trace_results and nothing else. The views join trace_truth and
# trace_ledger, so trace_results is registered on those two datasets as an AUTHORIZED DATASET: its views may read
# them on behalf of a caller who has no access of their own. The older direct grants to trace-dashboard are removed.
#
# Usage: infra/11-dashboard-iam.sh                      print the plan only (changes nothing)
#        infra/11-dashboard-iam.sh --apply              data access: authorize, grant, revoke
#        infra/11-dashboard-iam.sh --apply-invoker      after the first deploy: run.invoker for the signed-in account
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

SERVICE=trace-dashboard
SA="${SERVICE}@${GCP_PROJECT}.iam.gserviceaccount.com"
RESULTS=trace_results
VIEWER='roles/bigquery.dataViewer'
REVOKE_DATASETS=("$DS_LEDGER" "${DS_LEDGER}${DEV_SUFFIX}" "$DS_TARGET" "${DS_TARGET}${DEV_SUFFIX}")
REVOKE_TABLES=("$DS_TRUTH.evaluation_results" "${DS_TRUTH}${DEV_SUFFIX}.evaluation_results")

cat <<PLAN
Project: $GCP_PROJECT

--apply
  authorize  dataset $RESULTS (its views) to read  $DS_TRUTH, $DS_LEDGER
  grant      $SERVICE  $VIEWER  on dataset $RESULTS
  revoke     $SERVICE  $VIEWER  on datasets ${REVOKE_DATASETS[*]}
  revoke     $SERVICE  $VIEWER  on tables ${REVOKE_TABLES[*]}
  keep       $SERVICE  roles/bigquery.jobUser on the project (needed to run any query)

--apply-invoker (the service must exist)
  grant      roles/run.invoker on Cloud Run service $SERVICE to user:<you> (the signed-in gcloud account), no one else
PLAN

bq_sql() {
  bq --project_id="$GCP_PROJECT" --location="$BQ_LOCATION" query --use_legacy_sql=false --quiet \
    --maximum_bytes_billed="$BQ_MAX_BYTES_BILLED" "$1" >/dev/null
}

authorize() {  # source dataset whose access list gains the authorized-dataset entry
  local source="$1" file
  file="$(mktemp)"
  bq --project_id="$GCP_PROJECT" show --format=json "$source" | python3 -c '
import json, sys
project, results = sys.argv[1], sys.argv[2]
dataset = json.load(sys.stdin)
entry = {"dataset": {"dataset": {"projectId": project, "datasetId": results}, "targetTypes": ["VIEWS"]}}
access = dataset.get("access", [])
if not any(a.get("dataset", {}).get("dataset", {}).get("datasetId") == results for a in access):
    access.append(entry)
json.dump({"access": access}, sys.stdout)' "$GCP_PROJECT" "$RESULTS" >"$file"
  bq --project_id="$GCP_PROJECT" update --source "$file" "$source" >/dev/null
  rm -f "$file"
  echo "authorized: $RESULTS on $source"
}

confirm() {
  read -r -p "$1 [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
}

case "${1:-}" in
  "")
    echo
    echo "Plan only. Re-run with --apply or --apply-invoker."
    ;;
  --apply)
    confirm "Apply the data-access changes?"
    authorize "$DS_TRUTH"
    authorize "$DS_LEDGER"
    bq_sql "GRANT \`$VIEWER\` ON SCHEMA \`$GCP_PROJECT.$RESULTS\` TO \"serviceAccount:$SA\""
    echo "granted: $SERVICE $VIEWER on $RESULTS"
    for dataset in "${REVOKE_DATASETS[@]}"; do
      bq_sql "REVOKE \`$VIEWER\` ON SCHEMA \`$GCP_PROJECT.$dataset\` FROM \"serviceAccount:$SA\""
      echo "revoked: $SERVICE $VIEWER on $dataset"
    done
    for table in "${REVOKE_TABLES[@]}"; do
      bq_sql "REVOKE \`$VIEWER\` ON TABLE \`$GCP_PROJECT.$table\` FROM \"serviceAccount:$SA\""
      echo "revoked: $SERVICE $VIEWER on $table"
    done
    echo "Done."
    ;;
  --apply-invoker)
    account="$(gcloud config get-value account 2>/dev/null)"
    [[ -n "$account" ]] || { echo "No signed-in gcloud account." >&2; exit 1; }
    confirm "Grant run.invoker on $SERVICE to the signed-in account only?"
    gcloud run services add-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" --region "$REGION" \
      --member "user:$account" --role roles/run.invoker --quiet >/dev/null
    policy="$(gcloud run services get-iam-policy "$SERVICE" --project "$GCP_PROJECT" --region "$REGION" --format=json)"
    if grep -qE 'allUsers|allAuthenticatedUsers' <<<"$policy"; then echo "$SERVICE is PUBLIC; fix this now." >&2; exit 1; fi
    echo "granted: roles/run.invoker on $SERVICE to user:<you>. The service is not public."
    ;;
  *)
    echo "Usage: $0 [--apply | --apply-invoker]" >&2
    exit 2
    ;;
esac
