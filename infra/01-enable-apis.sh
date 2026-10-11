#!/usr/bin/env bash
# Enables the APIs TRACE needs. Enabling an API is free; only usage bills. Safe to re-run.
# Usage: infra/01-enable-apis.sh [--yes]
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

# Listed in CLAUDE.md section 9, Phase 0.
CORE_APIS=(
  bigquery.googleapis.com
  run.googleapis.com
  cloudscheduler.googleapis.com
  pubsub.googleapis.com
  secretmanager.googleapis.com
  artifactregistry.googleapis.com
  logging.googleapis.com
  iam.googleapis.com
)
# Not named in the checklist but required by other Phase 0 items:
#   iamcredentials + sts      Workload Identity Federation token exchange for github-deployer
#   cloudresourcemanager      project-level IAM bindings from gcloud and from GitHub Actions
#   billingbudgets            the $1 budget alert
SUPPORT_APIS=(
  iamcredentials.googleapis.com
  sts.googleapis.com
  cloudresourcemanager.googleapis.com
  billingbudgets.googleapis.com
)

enabled="$(gcloud services list --enabled --project "$GCP_PROJECT" --format='value(config.name)')"
to_enable=()
for api in "${CORE_APIS[@]}" "${SUPPORT_APIS[@]}"; do
  if grep -qx "$api" <<<"$enabled"; then
    echo "already enabled: $api"
  else
    to_enable+=("$api")
  fi
done

if [[ ${#to_enable[@]} -eq 0 ]]; then
  echo "Nothing to enable."
  exit 0
fi

echo
echo "Will enable on $GCP_PROJECT:"
printf '  %s\n' "${to_enable[@]}"
if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Proceed? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

gcloud services enable "${to_enable[@]}" --project "$GCP_PROJECT"
echo "Done."
