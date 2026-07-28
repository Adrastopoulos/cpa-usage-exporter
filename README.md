# cpa-usage-exporter

Exports [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) telemetry to
Prometheus or BigQuery: it drains CPA's per-request usage queue, snapshots
credential health from `/v0/management/auth-files`, and polls provider
subscription-quota endpoints. CPA replaced its built-in usage tracking in v6.10.0
with an ephemeral queue that expects an external consumer; this is that consumer,
plus the account health and 5h/7d quota windows the queue omits.

## Install

Not on PyPI yet. Install from git, or build the image with
`docker build -t cpa-usage-exporter .`.

```bash
pip install git+https://github.com/Adrastopoulos/cpa-usage-exporter
pip install 'cpa-usage-exporter[bigquery] @ git+https://github.com/Adrastopoulos/cpa-usage-exporter'
```

## Quickstart

Records are only published when CPA has `usage-statistics-enabled: true`; set
`redis-usage-queue-retention-seconds: 300` to widen the window.

```bash
export CPA_MANAGEMENT_URL=http://127.0.0.1:8317/v0/management CPA_MANAGEMENT_KEY=key
cpa-usage-exporter --check-config   # validate and print a summary
cpa-usage-exporter                  # serves metrics on :9185/metrics
```

The same variables work with `docker run --rm -p 9185:9185 -e CPA_MANAGEMENT_URL=...
-e CPA_MANAGEMENT_KEY=... cpa-usage-exporter`. Scrape `cpa-usage-exporter:9185`.

## Configuration

YAML file (`--config PATH` or `CPA_CONFIG_FILE`), environment variables, or both;
env wins. Each variable is `CPA_` + the suffix below; see
[`config.example.yaml`](config.example.yaml) for the commented reference.

| YAML | Env suffix | Default |
| --- | --- | --- |
| `sink` · `interval_seconds` · `log_level` | `SINK` · `INTERVAL_SECONDS` · `LOG_LEVEL` | `prometheus` · `60` · `INFO` |
| `cpa.management_url` · `management_key` | `MANAGEMENT_URL` · `MANAGEMENT_KEY` | `http://127.0.0.1:8317/v0/management` · *(required, sent as bearer)* |
| `cpa.queue_batch` · `queue_max_batches` | `QUEUE_BATCH` · `QUEUE_MAX_BATCHES` | `1000` · `20` (records per call, call cap per cycle) |
| `cpa.timeout_seconds` · `verify_tls` · `collect_usage_queue` · `collect_auth_files` | `HTTP_TIMEOUT_SECONDS` · `VERIFY_TLS` · `COLLECT_USAGE_QUEUE` · `COLLECT_AUTH_FILES` | `30` · `true` · `true` · `true` |
| `quota.enabled` · `auth_dir` · `providers` · `interval_seconds` · `timeout_seconds` | `QUOTA_ENABLED` · `QUOTA_AUTH_DIR` · `QUOTA_PROVIDERS` · `QUOTA_INTERVAL_SECONDS` · `QUOTA_TIMEOUT_SECONDS` | `false` · *(required when enabled)* · `claude,codex` · `300` · `20` |
| `pricing.enabled` · `path` · `fallback_to_bundled` · `currency` | `PRICING_ENABLED` · `PRICING_PATH` · `PRICING_FALLBACK_TO_BUNDLED` · `PRICING_CURRENCY` | `true` · *(bundled map)* · `true` · `USD` |
| `prometheus.host` · `port` · `latency_buckets` · `ttft_buckets` | `PROMETHEUS_HOST` · `PROMETHEUS_PORT` · `PROMETHEUS_LATENCY_BUCKETS` · `PROMETHEUS_TTFT_BUCKETS` | `0.0.0.0` · `9185` · comma-separated seconds |
| `prometheus.include_endpoint_label` · `include_api_key_label` | `PROMETHEUS_INCLUDE_ENDPOINT_LABEL` · `PROMETHEUS_INCLUDE_API_KEY_LABEL` | `true` · `false` (adds a series per client key) |
| `bigquery.project` · `dataset` · `location` · `table_prefix` · `credentials_file` | `BIGQUERY_PROJECT` · `BIGQUERY_DATASET` · `BIGQUERY_LOCATION` · `BIGQUERY_TABLE_PREFIX` · `BIGQUERY_CREDENTIALS_FILE` | *(required)* · *(required, must exist)* · *(client default)* · `cliproxy_` · *(ADC)* |

## Sinks and dashboard

The Prometheus sink serves OpenMetrics at `/metrics`; series are prefixed `cpa_`
(requests, errors, tokens, estimated cost, latency and TTFT histograms, account
health, quota gauges). The BigQuery sink creates four day-partitioned tables on
first use — `<prefix>requests`, `accounts`, `quota`, `limits` — adding columns as
the schema grows, and needs `roles/bigquery.dataEditor`.

Import [`dashboards/cpa-usage-prometheus.json`](dashboards/cpa-usage-prometheus.json)
in Grafana (**Dashboards → New → Import**) against your Prometheus data source.

## Notes

- The usage queue is destructive and short-lived: reads remove records, and
  anything older than `redis-usage-queue-retention-seconds` (default 60s) is
  pruned. Keep `interval_seconds` below that window and run exactly one consumer;
  two drainers each see a random half of your traffic.
- Quota comes from undocumented vendor endpoints
  (`api.anthropic.com/api/oauth/usage`, `chatgpt.com/backend-api/wham/usage`). A
  broken poller logs a warning and reports nothing rather than failing the cycle.
- Costs are estimates: bundled prices drift, and subscription (OAuth) traffic is
  not token-metered by the provider, so treat that portion as what the traffic
  would have cost on the API. Unpriced models increment
  `cpa_unpriced_requests_total`.

## License

Apache-2.0
