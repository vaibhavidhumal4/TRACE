#!/usr/bin/env bash
# Creates the TRACE service accounts and grants the roles in CLAUDE.md section 6 that can be granted before the
# Cloud Run resources, secrets and log view exist. Service accounts are free. Safe to re-run.
#
# Usage: infra/03-service-accounts.sh            print the plan only (changes nothing)
#        infra/03-service-accounts.sh --apply    print the plan, ask, then apply
#
# Granted LATER, by the script that creates the resource (not here):
#   logging.viewAccessor on the target-only log view   trace-agents, trace-mcp, trace-dashboard   (04-logging.sh)
#   artifactregistry.writer on the image repo          github-deployer                           (05-artifact-registry.sh)
#   secretAccessor on individual secrets               trace-agents, trace-evaluator, trace-mcp, trace-dashboard
#   update on the target job and upstream gateway      ops-automation                            (Phase 1 / Phase 3)
#   run.invoker on trace-api                           trace-pubsub-invoker                      (Phase 2)
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

sa_email() { echo "$1@${GCP_PROJECT}.iam.gserviceaccount.com"; }

# name|display name
ACCOUNTS=(
  "trace-etl|TRACE target ETL"
  "trace-agents|TRACE API and agents"
  "ops-automation|Ops automation"
  "trace-evaluator|TRACE evaluator"
  "trace-mcp|TRACE MCP server"
  "trace-dashboard|TRACE dashboard"
  "trace-pubsub-invoker|TRACE Pub/Sub push invoker"
  "github-deployer|GitHub Actions deployer"
)
RUNTIME_ACCOUNTS=(trace-etl trace-agents ops-automation trace-evaluator trace-mcp trace-dashboard)

# kind|account|role|resource
#   project  project-level role
#   dataset  BigQuery dataset-level role (applied to the prod dataset and its dev twin)
#   table    BigQuery table-level role   (applied to the prod table and its dev twin); resource is dataset.table
#   dataset-exact  BigQuery dataset-level role on exactly the named dataset (no twin)
#   actas    iam.serviceAccountUser on another service account; resource is that account's name
GRANTS=(
  "dataset|trace-etl|roles/bigquery.dataEditor|$DS_TARGET"
  "project|trace-etl|roles/bigquery.jobUser|"
  "project|trace-etl|roles/logging.logWriter|"

  "dataset|trace-agents|roles/bigquery.dataViewer|$DS_TARGET"
  "dataset|trace-agents|roles/bigquery.dataEditor|$DS_LEDGER"
  "project|trace-agents|roles/bigquery.jobUser|"
  "project|trace-agents|roles/run.viewer|"

  "dataset|ops-automation|roles/bigquery.dataOwner|$DS_TARGET"
  "dataset|ops-automation|roles/bigquery.dataEditor|$DS_TRUTH"
  "project|ops-automation|roles/bigquery.jobUser|"
  "project|ops-automation|roles/logging.logWriter|"

  "dataset|trace-evaluator|roles/bigquery.dataViewer|$DS_LEDGER"
  "table|trace-evaluator|roles/bigquery.dataEditor|$DS_LEDGER.incident_memory"
  "dataset|trace-evaluator|roles/bigquery.dataEditor|$DS_TRUTH"
  "project|trace-evaluator|roles/bigquery.jobUser|"

  "dataset|trace-mcp|roles/bigquery.dataViewer|$DS_LEDGER"
  "dataset|trace-mcp|roles/bigquery.dataViewer|$DS_TARGET"
  "project|trace-mcp|roles/bigquery.jobUser|"

  # trace-dashboard reads the trace_results views only; that grant lives in 11-dashboard-iam.sh.
  "project|trace-dashboard|roles/bigquery.jobUser|"

  "project|github-deployer|roles/run.developer|"
)
for runtime in "${RUNTIME_ACCOUNTS[@]}"; do
  GRANTS+=("actas|github-deployer|roles/iam.serviceAccountUser|$runtime")
done
# The Phase 0 acceptance workflow runs SELECT 1 and one insert as github-deployer. Dev ledger only: the deployer
# gets no role on the prod ledger, trace_target or trace_truth (decision 2026-10-11, CLAUDE.md section 6).
GRANTS+=("project|github-deployer|roles/bigquery.jobUser|")
GRANTS+=("dataset-exact|github-deployer|roles/bigquery.dataEditor|${DS_LEDGER}${DEV_SUFFIX}")

existing_accounts="$(gcloud iam service-accounts list --project "$GCP_PROJECT" --format='value(email)')"

echo "Project: $GCP_PROJECT"
echo
echo "Service accounts:"
for entry in "${ACCOUNTS[@]}"; do
  name="${entry%%|*}"
  if grep -qx "$(sa_email "$name")" <<<"$existing_accounts"; then
    echo "  exists   $name"
  else
    echo "  CREATE   $name"
  fi
done
echo
echo "Role grants (re-applied idempotently):"
for grant in "${GRANTS[@]}"; do
  IFS='|' read -r kind account role resource <<<"$grant"
  case "$kind" in
    project) target="project" ;;
    dataset) target="datasets $resource, ${resource}${DEV_SUFFIX}" ;;
    dataset-exact) target="dataset $resource" ;;
    table)   target="tables $resource, ${resource%%.*}${DEV_SUFFIX}.${resource#*.}" ;;
    actas)   target="service account $resource" ;;
    *) echo "Bad grant kind: $grant" >&2; exit 2 ;;
  esac
  printf '  %-22s %-32s on %s\n' "$account" "$role" "$target"
done

if [[ "${1:-}" != "--apply" ]]; then
  echo
  echo "Plan only. Re-run with --apply to make these changes."
  exit 0
fi
read -r -p "Apply these IAM changes? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }

for entry in "${ACCOUNTS[@]}"; do
  name="${entry%%|*}"
  display="${entry#*|}"
  if ! grep -qx "$(sa_email "$name")" <<<"$existing_accounts"; then
    gcloud iam service-accounts create "$name" --project "$GCP_PROJECT" --display-name "$display"
  fi
done

# A new service account can take a few seconds to become visible to other services.
for entry in "${ACCOUNTS[@]}"; do
  name="${entry%%|*}"
  for _ in $(seq 1 20); do
    gcloud iam service-accounts describe "$(sa_email "$name")" --project "$GCP_PROJECT" >/dev/null 2>&1 && break
    sleep 3
  done
done

bq_grant() {  # object_kind (SCHEMA|TABLE), object path, role, member
  bq --project_id="$GCP_PROJECT" --location="$BQ_LOCATION" query --use_legacy_sql=false --quiet \
    --maximum_bytes_billed="$BQ_MAX_BYTES_BILLED" \
    "GRANT \`$3\` ON $1 \`$GCP_PROJECT.$2\` TO \"serviceAccount:$4\"" >/dev/null
}

for grant in "${GRANTS[@]}"; do
  IFS='|' read -r kind account role resource <<<"$grant"
  member="$(sa_email "$account")"
  case "$kind" in
    project)
      gcloud projects add-iam-policy-binding "$GCP_PROJECT" --member "serviceAccount:$member" --role "$role" \
        --condition=None --quiet >/dev/null
      ;;
    dataset)
      bq_grant SCHEMA "$resource" "$role" "$member"
      bq_grant SCHEMA "${resource}${DEV_SUFFIX}" "$role" "$member"
      ;;
    dataset-exact)
      bq_grant SCHEMA "$resource" "$role" "$member"
      ;;
    table)
      bq_grant TABLE "$resource" "$role" "$member"
      bq_grant TABLE "${resource%%.*}${DEV_SUFFIX}.${resource#*.}" "$role" "$member"
      ;;
    actas)
      gcloud iam service-accounts add-iam-policy-binding "$(sa_email "$resource")" --project "$GCP_PROJECT" \
        --member "serviceAccount:$member" --role "$role" --quiet >/dev/null
      ;;
  esac
  echo "granted: $account $role ${resource:+on $resource}"
done
echo "Done."
