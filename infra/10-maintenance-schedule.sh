#!/usr/bin/env bash
# Schedule for the maintenance runner (the dead-man's-switch reverter). Lets trace-scheduler start that one job and
# creates the Scheduler job PAUSED. Resume it only while a fault is open; pause it again afterwards. Safe to re-run.
# Usage: infra/10-maintenance-schedule.sh [--apply]
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

sa="${SCHEDULER_SA}@${GCP_PROJECT}.iam.gserviceaccount.com"
uri="https://run.googleapis.com/v2/projects/$GCP_PROJECT/locations/$REGION/jobs/$OPS_JOB:run"

cmds=("gcloud run jobs add-iam-policy-binding $OPS_JOB --region=$REGION --member=serviceAccount:$sa --role=roles/run.invoker --project=$GCP_PROJECT")
if gcloud scheduler jobs describe "$OPS_SCHEDULE_JOB" --location "$REGION" --project "$GCP_PROJECT" >/dev/null 2>&1; then
  echo "Scheduler job $OPS_SCHEDULE_JOB already exists; its state is left as it is."
else
  cmds+=("gcloud scheduler jobs create http $OPS_SCHEDULE_JOB --location=$REGION --schedule='$OPS_SCHEDULE' --time-zone=$ETL_SCHEDULE_TZ --uri=$uri --http-method=POST --oauth-service-account-email=$sa --project=$GCP_PROJECT")
  cmds+=("gcloud scheduler jobs pause $OPS_SCHEDULE_JOB --location=$REGION --project=$GCP_PROJECT")
fi

echo "Commands:"
printf '  %s\n\n' "${cmds[@]}"
if [[ "${1:-}" != "--apply" ]]; then echo "Plan only. Re-run with --apply."; exit 0; fi
read -r -p "Apply? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
for c in "${cmds[@]}"; do echo "+ $c"; eval "$c" >/dev/null; done
echo "Done."
