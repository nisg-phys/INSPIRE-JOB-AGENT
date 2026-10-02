# Observability

A Grafana dashboard over the deployed backend (Cloud Run service
`pulsar-backend`, project `pulsar-jobs-agent`). Two data sources feed it:

- **Cloud Run's built-in metrics**: instance count, CPU/memory, and latency as
  Cloud Run measures it (including cold starts).
- **Cloud Logging**, for the raw log lines themselves: the dashboard's Logs
  row shows warnings and errors, and every structured line the backend
  writes. Needs the `googlecloud-logging-datasource` plugin, which the compose
  file installs.
- **Log-based metrics** built from the backend's structured JSON logs
  (`backend/app/logging_config.py`): traffic per endpoint, latency percentiles,
  per-step timings, cache hit rate, LLM provider failover, refusals, and web
  fallback runs/failures. Definitions are in `log-metrics/`.

## One-time setup

```sh
# 1. Create the log-based metrics in Cloud Monitoring (idempotent).
./observability/create-log-metrics.sh

# 2. Give Grafana credentials (opens a browser).
gcloud auth application-default login

# 3. Start Grafana (re-create it after pulling a change to the compose file).
docker compose --profile observability up -d --force-recreate grafana
```

Open <http://localhost:3000> (login `admin` / `admin`, bound to localhost
only). The **Pulsar backend** dashboard is provisioned automatically.

Log-based metrics only count logs written **after** they are created, so the
counters start empty and fill as traffic arrives. Cloud Run's built-in panels
(instances, CPU, memory) have history immediately.

## Reading the logs

The **Logs** row at the bottom of the dashboard has two panels:

- **Warnings and errors** - anything at WARNING or above.
- **Backend logs** - every structured line, newest first. httpx's per-request
  lines are left out (an Opik keep-alive ping alone logs one every ~11s).

Click a line to expand its fields. To follow one request end to end, copy its
`request_id` and use **Explore** (left sidebar) with the Google Cloud Logging
datasource and a query such as:

```
resource.type="cloud_run_revision" AND resource.labels.service_name="pulsar-backend"
AND jsonPayload.request_id="3d333d8a5f02"
```

Queries use the [Logging query language](https://cloud.google.com/logging/docs/view/logging-query-language),
the same as Logs Explorer in the Cloud console. Lines logged before the
backend started writing a `severity` field are stored without one, so they
aren't coloured by level; the warnings panel also matches them on
`jsonPayload.level`.

## How it fits together

- `grafana/provisioning/` - the Cloud Monitoring datasource and the dashboard
  loader. Nothing to click through in the UI.
- `grafana/dashboards/pulsar.json` - the dashboard. Edit it in Grafana, then
  export the JSON over this file (`allowUiUpdates` is off, so UI edits are not
  saved back on their own).
- `log-metrics/*.json` - one file per log-based metric. Edit and re-run
  `create-log-metrics.sh` to update.

## Notes

- Grafana runs on your machine, so the dashboard is only visible while it is
  running. Hosting it (Grafana Cloud's free tier reads the same Cloud
  Monitoring data) is the next step if you want it always available or want
  alerts to fire without your laptop.
- The compose file mounts `~/.config/gcloud` read-only into the container so
  Grafana can use your ADC. Fine on a personal machine; don't reuse this
  compose service on a shared host.
- Only `/jobs/search` and `/institutions/papers` are counted per endpoint.
  That is deliberate: a metric label per raw URL path would let bots probing
  random `/jobs/...` URLs create unbounded series.
- The search-latency panel is split by the `cached` label on
  `request_completed`: a hit is ~0.25s, a miss several seconds, so a combined
  percentile would describe neither. Data logged before the label existed
  shows as a series with `cached=` empty. After pulling this change, re-run
  `create-log-metrics.sh` to add the label to the metric.
