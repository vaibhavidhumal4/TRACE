#!/usr/bin/env bash
# IAM for the maintenance tooling and the campaign runner (CLAUDE.md 5.2 and 6). Safe to re-run.
#   1. ops-automation may update and run the two target jobs, and only those (run.developer on each job, plus
#      serviceAccountUser on trace-etl and read access to the image repository, both of which Cloud Run requires
#      before it accepts a change to a job).
#   2. The person running the campaign may mint short-lived tokens for ops-automation and trace-evaluator, so one
#      script can act as a different identity per step. No key is created.
# Usage: infra/09-campaign-iam.sh [--apply]
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

ops="ops-automation@${GCP_PROJECT}.iam.gserviceaccount.com"
etl="trace-etl@${GCP_PROJECT}.iam.gserviceaccount.com"
evaluator="trace-evaluator@${GCP_PROJECT}.iam.gserviceaccount.com"
operator="user:$(gcloud config get-value account 2>/dev/null)"

cmds=()
for job in "$ETL_JOB" "$ETL_JOB_DEV"; do
  cmds+=("gcloud run jobs add-iam-policy-binding $job --region=$REGION --member=serviceAccount:$ops --role=roles/run.developer --project=$GCP_PROJECT")
done
cmds+=("gcloud iam service-accounts add-iam-policy-binding $etl --member=serviceAccount:$ops --role=roles/iam.serviceAccountUser --project=$GCP_PROJECT")
# Cloud Run refuses a job update unless the caller can read the job's container image.
cmds+=("gcloud artifacts repositories add-iam-policy-binding $ARTIFACT_REPO --location=$REGION --member=serviceAccount:$ops --role=roles/artifactregistry.reader --project=$GCP_PROJECT")
for target in "$ops" "$evaluator"; do
  cmds+=("gcloud iam service-accounts add-iam-policy-binding $target --member=$operator --role=roles/iam.serviceAccountTokenCreator --project=$GCP_PROJECT")
done

echo "Commands:"
printf '  %s\n\n' "${cmds[@]}" | sed -E 's/user:[^ ]+/user:<you>/'
if [[ "${1:-}" != "--apply" ]]; then echo "Plan only. Re-run with --apply."; exit 0; fi
read -r -p "Apply? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
for c in "${cmds[@]}"; do echo "+ ${c//$operator/user:<you>}"; eval "$c" >/dev/null; done
echo "Done."
