#!/usr/bin/env bash
# Grants read access on the LLM key secrets to the identities that call an LLM, and to nobody else. It never reads
# or prints a secret value, and it does not create secrets or versions. Safe to re-run.
# Usage: infra/07-secrets.sh [--apply]
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

# secret|account. trace-evaluator gets no key: Evaluation never calls an LLM (embeddings and the ablation are deferred).
ACCESS=(
  "gemini-api-key|trace-agents"
  "groq-api-key|trace-agents"
)

cmds=()
for entry in "${ACCESS[@]}"; do
  secret="${entry%%|*}"; account="${entry#*|}"
  gcloud secrets describe "$secret" --project "$GCP_PROJECT" >/dev/null 2>&1 ||
    { echo "Secret $secret does not exist. Create it first (the human does this; never from a script)." >&2; exit 1; }
  cmds+=("gcloud secrets add-iam-policy-binding $secret --member=serviceAccount:${account}@${GCP_PROJECT}.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor --project=$GCP_PROJECT")
done

echo "Commands:"
printf '  %s\n\n' "${cmds[@]}"
if [[ "${1:-}" != "--apply" ]]; then echo "Plan only. Re-run with --apply."; exit 0; fi
read -r -p "Apply? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
for c in "${cmds[@]}"; do echo "+ $c"; eval "$c" >/dev/null; done
echo "Done."
