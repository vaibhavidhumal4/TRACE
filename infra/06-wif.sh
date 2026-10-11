#!/usr/bin/env bash
# Keyless GitHub Actions sign-in (Workload Identity Federation). The provider accepts tokens only from the one
# repository and owner pinned by numeric ID in config.env, and only that repository may act as github-deployer.
# No service-account key is created. Safe to re-run.
# Usage: infra/06-wif.sh [--apply]
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

deployer="github-deployer@${GCP_PROJECT}.iam.gserviceaccount.com"
pool_path="projects/$GCP_PROJECT_NUMBER/locations/global/workloadIdentityPools/$WIF_POOL"
mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref"
condition="assertion.repository_id == '$GITHUB_REPO_ID' && assertion.repository_owner_id == '$GITHUB_OWNER_ID'"

cmds=()
gcloud iam workload-identity-pools describe "$WIF_POOL" --location global --project "$GCP_PROJECT" >/dev/null 2>&1 ||
  cmds+=("gcloud iam workload-identity-pools create $WIF_POOL --location=global --display-name='GitHub Actions' --project=$GCP_PROJECT")
gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER" --workload-identity-pool "$WIF_POOL" --location global --project "$GCP_PROJECT" >/dev/null 2>&1 ||
  cmds+=("gcloud iam workload-identity-pools providers create-oidc $WIF_PROVIDER --location=global --workload-identity-pool=$WIF_POOL --issuer-uri=https://token.actions.githubusercontent.com --attribute-mapping=\"$mapping\" --attribute-condition=\"$condition\" --project=$GCP_PROJECT")
cmds+=("gcloud iam service-accounts add-iam-policy-binding $deployer --role=roles/iam.workloadIdentityUser --member=principalSet://iam.googleapis.com/$pool_path/attribute.repository_id/$GITHUB_REPO_ID --project=$GCP_PROJECT")

echo "Commands:"
printf '  %s\n\n' "${cmds[@]}"
echo "Workflow settings once applied:"
echo "  workload_identity_provider: $pool_path/providers/$WIF_PROVIDER"
echo "  service_account:            $deployer"
if [[ "${1:-}" != "--apply" ]]; then echo "Plan only. Re-run with --apply."; exit 0; fi
read -r -p "Apply? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
for c in "${cmds[@]}"; do echo "+ $c"; eval "$c" >/dev/null; done
echo "Done."
