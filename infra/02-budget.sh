#!/usr/bin/env bash
# Creates the project-scoped budget alert (CLAUDE.md section 8). Budgets are free. They only email the billing
# account admins; they do NOT stop spend (infra/kill-switch does that). Safe to re-run.
# Usage: infra/02-budget.sh [--yes]
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

BUDGET_NAME=trace-monthly-guardrail

billing_account="$(gcloud billing projects describe "$GCP_PROJECT" --format='value(billingAccountName)')"
billing_account="${billing_account#billingAccounts/}"
[[ -n "$billing_account" ]] || { echo "No billing account attached to $GCP_PROJECT." >&2; exit 1; }
currency="$(gcloud billing accounts describe "$billing_account" --format='value(currencyCode)')"

# A budget must be in the billing account's currency. BUDGET_AMOUNT_<CURRENCY> in config.env holds the
# equivalent of about 1 USD.
amount_var="BUDGET_AMOUNT_${currency}"
amount="${!amount_var:-}"
[[ -n "$amount" ]] || { echo "Set $amount_var in infra/config.env (about 1 USD in $currency)." >&2; exit 1; }

existing="$(gcloud billing budgets list --billing-account="$billing_account" --billing-project="$GCP_PROJECT" \
  --filter="displayName=$BUDGET_NAME" --format='value(name)')"
if [[ -n "$existing" ]]; then
  echo "Budget already exists: $existing"
  exit 0
fi

echo "Will create budget '$BUDGET_NAME': ${amount} ${currency} per month, project $GCP_PROJECT only,"
echo "email alerts at 50%, 90% and 100% of actual spend."
if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Proceed? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

gcloud billing budgets create \
  --billing-account="$billing_account" \
  --billing-project="$GCP_PROJECT" \
  --display-name="$BUDGET_NAME" \
  --budget-amount="${amount}${currency}" \
  --calendar-period=month \
  --filter-projects="projects/$GCP_PROJECT_NUMBER" \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
echo "Done."
