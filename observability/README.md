# Observability

A Grafana dashboard over the deployed backend (Cloud Run service
`pulsar-backend`, project `pulsar-jobs-agent`). Two data sources feed it:

- **Cloud Run's built-in metrics**: instance count, CPU/memory, and latency as
  Cloud Run measures it (including cold starts).
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

# 3. Start Grafana.
docker compose --profile observability up -d grafana
```

Open <http://localhost:3000> (login `admin` / `admin`, bound to localhost
only). The **Pulsar backend** dashboard is provisioned automatically.

Log-based metrics only count logs written **after** they are created, so the
counters start empty and fill as traffic arrives. Cloud Run's built-in panels
(instances, CPU, memory) have history immediately.

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
- The search-latency panel can't yet split cache hits from misses (the
  request log line doesn't record `cached`). The step-latency panel shows the
  same bimodality via `cache_lookup` vs `query_rewrite`/`inspire_search`.
