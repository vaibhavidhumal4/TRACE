#!/usr/bin/env bash
# Docker repository for TRACE images, with a cleanup policy that keeps the newest version of each image and
# deletes the rest, and push access for github-deployer on this repository only. Safe to re-run.
# Usage: infra/05-artifact-registry.sh [--apply]
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

policy_file="$(pwd)/artifact-cleanup-policy.json"
deployer="github-deployer@${GCP_PROJECT}.iam.gserviceaccount.com"

cmds=()
gcloud artifacts repositories describe "$ARTIFACT_REPO" --location "$REGION" --project "$GCP_PROJECT" >/dev/null 2>&1 ||
  cmds+=("gcloud artifacts repositories create $ARTIFACT_REPO --repository-format=docker --location=$REGION --description='TRACE container images' --project=$GCP_PROJECT")
cmds+=("gcloud artifacts repositories set-cleanup-policies $ARTIFACT_REPO --location=$REGION --policy=$policy_file --no-dry-run --project=$GCP_PROJECT")
cmds+=("gcloud artifacts repositories add-iam-policy-binding $ARTIFACT_REPO --location=$REGION --member=serviceAccount:$deployer --role=roles/artifactregistry.writer --project=$GCP_PROJECT")

echo "Commands:"
printf '  %s\n\n' "${cmds[@]}"
if [[ "${1:-}" != "--apply" ]]; then echo "Plan only. Re-run with --apply."; exit 0; fi
read -r -p "Apply? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
for c in "${cmds[@]}"; do echo "+ $c"; eval "$c" >/dev/null; done
echo "Done."
