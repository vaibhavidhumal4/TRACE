#!/usr/bin/env bash
# Cloud Scheduler trigger for the target ETL job. Creates the trace-scheduler identity (run.invoker on the one job,
# nothing else) and a Scheduler job that starts an execution on ETL_SCHEDULE. Safe to re-run.
# Usage: infra/08-scheduler.sh [--apply]
#   Pause:  gcloud scheduler jobs pause  trace-target-etl-schedule --location us-central1
#   Resume: gcloud scheduler jobs resume trace-target-etl-schedule --location us-central1
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

sa="${SCHEDULER_SA}@${GCP_PROJECT}.iam.gserviceaccount.com"
uri="https://run.googleapis.com/v2/projects/$GCP_PROJECT/locations/$REGION/jobs/$ETL_JOB:run"

cmds=()
gcloud iam service-accounts describe "$sa" --project "$GCP_PROJECT" >/dev/null 2>&1 ||
  cmds+=("gcloud iam service-accounts create $SCHEDULER_SA --display-name='TRACE scheduler trigger' --project=$GCP_PROJECT")
cmds+=("gcloud run jobs add-iam-policy-binding $ETL_JOB --region=$REGION --member=serviceAccount:$sa --role=roles/run.invoker --project=$GCP_PROJECT")
verb=create
gcloud scheduler jobs describe "$ETL_SCHEDULE_JOB" --location "$REGION" --project "$GCP_PROJECT" >/dev/null 2>&1 && verb=update
cmds+=("gcloud scheduler jobs $verb http $ETL_SCHEDULE_JOB --location=$REGION --schedule='$ETL_SCHEDULE' --time-zone=$ETL_SCHEDULE_TZ --uri=$uri --http-method=POST --oauth-service-account-email=$sa --project=$GCP_PROJECT")

echo "Commands:"
printf '  %s\n\n' "${cmds[@]}"
if [[ "${1:-}" != "--apply" ]]; then echo "Plan only. Re-run with --apply."; exit 0; fi
read -r -p "Apply? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
for c in "${cmds[@]}"; do
  echo "+ $c"
  # A just-created service account can take a few seconds to be usable in a policy binding.
  for try in 1 2 3 4 5 6; do eval "$c" >/dev/null && break; [[ $try -eq 6 ]] && exit 1; sleep 5; done
done
echo "Done."
