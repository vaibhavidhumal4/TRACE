#!/usr/bin/env bash
# Restricted log bucket, sink and view (CLAUDE.md 5.3). The sink admits only the target job's logs and the Admin
# Activity audit entries for the target job, the trace_target datasets and the trace-etl service account. Log views can
# filter only on source, resource type and log ID, so the sink is what keeps everything else out of the bucket.
# Safe to re-run.
# Usage: infra/04-logging.sh            print the plan only
#        infra/04-logging.sh --apply    print the plan, ask, then apply
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./config.env

VIEW_READERS=(trace-agents)   # trace-mcp and trace-dashboard are added when those services are built

bucket_path="projects/$GCP_PROJECT/locations/$REGION/buckets/$LOG_BUCKET"
view_path="$bucket_path/views/$LOG_VIEW"
etl_sa="trace-etl@${GCP_PROJECT}.iam.gserviceaccount.com"
sink_filter="(resource.type=\"cloud_run_job\" AND resource.labels.job_name=(\"$ETL_JOB\" OR \"$ETL_JOB_DEV\")) OR (logName=\"projects/$GCP_PROJECT/logs/cloudaudit.googleapis.com%2Factivity\" AND ((resource.type=\"bigquery_dataset\" AND resource.labels.dataset_id=(\"$DS_TARGET\" OR \"${DS_TARGET}${DEV_SUFFIX}\")) OR (resource.type=\"service_account\" AND resource.labels.email_id=\"$etl_sa\")))"
condition="expression=resource.name == \"$view_path\",title=trace-target-only-view"

cmds=()
gcloud logging buckets describe "$LOG_BUCKET" --location "$REGION" --project "$GCP_PROJECT" >/dev/null 2>&1 ||
  cmds+=("gcloud logging buckets create $LOG_BUCKET --location=$REGION --retention-days=30 --project=$GCP_PROJECT")
if gcloud logging sinks describe "$LOG_SINK" --project "$GCP_PROJECT" >/dev/null 2>&1; then
  cmds+=("gcloud logging sinks update $LOG_SINK logging.googleapis.com/$bucket_path --log-filter='$sink_filter' --project=$GCP_PROJECT")
else
  cmds+=("gcloud logging sinks create $LOG_SINK logging.googleapis.com/$bucket_path --log-filter='$sink_filter' --project=$GCP_PROJECT")
fi
gcloud logging views describe "$LOG_VIEW" --bucket "$LOG_BUCKET" --location "$REGION" --project "$GCP_PROJECT" >/dev/null 2>&1 ||
  cmds+=("gcloud logging views create $LOG_VIEW --bucket=$LOG_BUCKET --location=$REGION --project=$GCP_PROJECT")
for reader in "${VIEW_READERS[@]}"; do
  cmds+=("gcloud projects add-iam-policy-binding $GCP_PROJECT --member=serviceAccount:${reader}@${GCP_PROJECT}.iam.gserviceaccount.com --role=roles/logging.viewAccessor --condition='$condition' --quiet")
done

echo "Commands:"
printf '  %s\n\n' "${cmds[@]}"
if [[ "${1:-}" != "--apply" ]]; then echo "Plan only. Re-run with --apply."; exit 0; fi
read -r -p "Apply? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
for c in "${cmds[@]}"; do echo "+ $c"; eval "$c" >/dev/null; done
echo "Done."
