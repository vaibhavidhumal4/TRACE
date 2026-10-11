#!/usr/bin/env bash
# Applies the numbered migrations in sql/ to the prod or dev datasets. Every statement is CREATE ... IF NOT EXISTS,
# so this is safe to re-run. DDL scans no data (0 bytes billed).
# Usage: infra/apply-sql.sh dev|prod [--dry-run] [--yes]
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

env_name="${1:-}"
case "$env_name" in
  prod) SUFFIX="" ;;
  dev)  SUFFIX="$DEV_SUFFIX" ;;
  *) echo "Usage: $0 dev|prod [--dry-run] [--yes]" >&2; exit 2 ;;
esac
shift
dry_run=0
assume_yes=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    --yes) assume_yes=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

export SUFFIX BQ_LOCATION
files=(../sql/[0-9]*.sql)

echo "Project: $GCP_PROJECT  Location: $BQ_LOCATION  Env: $env_name (dataset suffix '${SUFFIX}')"
echo "Migrations:"
printf '  %s\n' "${files[@]}"

if [[ $dry_run -eq 1 ]]; then
  for f in "${files[@]}"; do
    echo "----- $f -----"
    # shellcheck disable=SC2016
    envsubst '${SUFFIX} ${BQ_LOCATION}' <"$f"
  done
  exit 0
fi

if [[ $assume_yes -ne 1 ]]; then
  read -r -p "Apply? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

for f in "${files[@]}"; do
  echo "Applying $f"
  # shellcheck disable=SC2016
  envsubst '${SUFFIX} ${BQ_LOCATION}' <"$f" | bq --project_id="$GCP_PROJECT" --location="$BQ_LOCATION" query \
    --use_legacy_sql=false --maximum_bytes_billed="$BQ_MAX_BYTES_BILLED" --quiet
done
echo "Done."
