#!/usr/bin/env bash
# Create or update the Cloud Logging log-based metrics the Grafana dashboard
# reads. Idempotent: safe to re-run after editing a file in log-metrics/.
set -euo pipefail

PROJECT="${PROJECT_ID:-pulsar-jobs-agent}"
DIR="$(cd "$(dirname "$0")" && pwd)/log-metrics"

for file in "$DIR"/*.json; do
  name="$(basename "$file" .json)"
  if gcloud logging metrics describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
    echo "updating $name"
    gcloud logging metrics update "$name" --project "$PROJECT" --config-from-file "$file"
  else
    echo "creating $name"
    gcloud logging metrics create "$name" --project "$PROJECT" --config-from-file "$file"
  fi
done
