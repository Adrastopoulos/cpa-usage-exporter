# cpa-usage-exporter

Turns [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) usage data into
metrics you can graph and alert on. It drains CPA's per-request usage queue,
snapshots credential health, and polls provider subscription-quota endpoints,
writing everything to **Prometheus** or **BigQuery**. A Grafana dashboard is
included.

## Why this exists

CLIProxyAPI removed its built-in usage tracking in **v6.10.0** ("remove usage
tracking and logging functionality"), along with the aggregated `/usage`,
`/usage/export` and `/usage/import` endpoints. What replaced it is an *ephemeral
usage queue*: CPA publishes one JSON record per request into a short-retention
in-memory FIFO and expects an external consumer to drain it.

That queue is the only place per-request telemetry exists, and it is
**destructive and short-lived** — records are removed as they are read, and
anything older than `redis-usage-queue-retention-seconds` (default 60s, max
3600s) is pruned. Poll too slowly and the data is simply gone.

This exporter is that consumer. It drains the queue on a tight interval and
turns each record into metrics, so you get request rates, token and cache
accounting, latency and TTFT distributions, cost estimates, per-account health
and subscription-quota burn — retained by your own monitoring stack rather than
by CPA.

It also fills two gaps the queue does not cover:

- **Account health** from `GET /v0/management/auth-files` — which credentials are
  disabled, unavailable, or accumulating failures.
- **Subscription quota** from the providers themselves. OAuth/subscription
  traffic has 5-hour and 7-day rate-limit windows that no per-request record
  reports. The exporter replays stored access tokens against the provider usage
  endpoints to read them directly.

## What it is not

**The management/control-plane UI is out of scope.** This project does not add,
edit, rotate or authenticate credentials, does not proxy traffic, and ships no
web UI of its own. It is read-only telemetry that assumes you already run CPA.

Related community projects solve an adjacent problem differently:

- [`Willxup/cpa-usage-keeper`](https://github.com/Willxup/cpa-usage-keeper) —
  persists queue records and serves its own self-contained usage UI.
- [`seakee/CPA-Manager-Plus`](https://github.com/seakee/CPA-Manager-Plus) — an
  enhanced management console for operating CPA.

Both are bespoke, self-contained applications with their own storage and
frontend. This exporter instead emits **standard metrics into standard
infrastructure**: scrape it with Prometheus, graph it in Grafana alongside the
rest of your fleet, and alert with Alertmanager using the same rules and
routing you already run — or stream to BigQuery for long-retention SQL
analysis. Choose this when you want CPA telemetry to live in your existing
observability stack; choose those when you want a dedicated, standalone UI.

## Quickstart

Enable the queue in your CPA config — records are only published when this is on:

```yaml
usage-statistics-enabled: true
# Optional: widen the retention window so a missed poll is not fatal.
redis-usage-queue-retention-seconds: 300
```

Then run the exporter:

```bash
pip install cpa-usage-exporter

export CPA_MANAGEMENT_URL=http://127.0.0.1:8317/v0/management
export CPA_MANAGEMENT_KEY=your-management-key

cpa-usage-exporter --check-config   # validate and print a summary
cpa-usage-exporter                  # serves metrics on :9185/metrics
```

Verify:

```bash
curl -s localhost:9185/metrics | grep '^cpa_'
```

Scrape it:

```yaml
scrape_configs:
  - job_name: cliproxyapi
    static_configs:
      - targets: ["cpa-usage-exporter:9185"]
```

> **Set `interval_seconds` below CPA's retention window.** The default 60s
> exporter interval against the default 60s retention leaves no margin. Either
> raise `redis-usage-queue-retention-seconds` or lower `CPA_INTERVAL_SECONDS`
> (30s is a good pairing with the default). Only one consumer can drain the
> queue — running two exporters, or an exporter alongside another queue
> consumer, means each sees a random half of your traffic.

### Docker

```bash
docker build -t cpa-usage-exporter .
docker run --rm -p 9185:9185 \
  -e CPA_MANAGEMENT_URL=http://cliproxyapi:8317/v0/management \
  -e CPA_MANAGEMENT_KEY=your-management-key \
  cpa-usage-exporter
```

## Configuration

Config comes from a YAML file, environment variables, or both. Environment
variables override the file; every key maps to `CPA_` + the underscored path.
See [`config.example.yaml`](config.example.yaml) for a fully commented file.

```bash
cpa-usage-exporter --config /etc/cpa-usage-exporter/config.yaml
# or
export CPA_CONFIG_FILE=/etc/cpa-usage-exporter/config.yaml
```

### Core

| YAML | Environment | Default | Description |
| --- | --- | --- | --- |
| `sink` | `CPA_SINK` | `prometheus` | `prometheus` or `bigquery` |
| `interval_seconds` | `CPA_INTERVAL_SECONDS` | `60` | Collection cycle period |
| `log_level` | `CPA_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### CPA connection (`cpa.*`)

| YAML | Environment | Default | Description |
| --- | --- | --- | --- |
| `cpa.management_url` | `CPA_MANAGEMENT_URL` | `http://127.0.0.1:8317/v0/management` | Management API base URL |
| `cpa.management_key` | `CPA_MANAGEMENT_KEY` | *(required)* | Management key, sent as a bearer token |
| `cpa.queue_batch` | `CPA_QUEUE_BATCH` | `1000` | Records requested per queue call |
| `cpa.queue_max_batches` | `CPA_QUEUE_MAX_BATCHES` | `20` | Cap on calls per cycle, so a backlog cannot stall a cycle |
| `cpa.timeout_seconds` | `CPA_HTTP_TIMEOUT_SECONDS` | `30` | HTTP timeout |
| `cpa.verify_tls` | `CPA_VERIFY_TLS` | `true` | Verify TLS certificates |
| `cpa.collect_usage_queue` | `CPA_COLLECT_USAGE_QUEUE` | `true` | Drain the usage queue |
| `cpa.collect_auth_files` | `CPA_COLLECT_AUTH_FILES` | `true` | Snapshot account health |

### Provider quota (`quota.*`)

Disabled by default. Requires read access to CPA's auth directory, where each
`*.json` credential file holds a provider `type` and an `access_token`.

| YAML | Environment | Default | Description |
| --- | --- | --- | --- |
| `quota.enabled` | `CPA_QUOTA_ENABLED` | `false` | Poll provider quota endpoints |
| `quota.auth_dir` | `CPA_QUOTA_AUTH_DIR` | *(required when enabled)* | CPA auth directory |
| `quota.interval_seconds` | `CPA_QUOTA_INTERVAL_SECONDS` | `300` | Quota poll period |
| `quota.timeout_seconds` | `CPA_QUOTA_TIMEOUT_SECONDS` | `20` | Provider HTTP timeout |
| `quota.providers` | `CPA_QUOTA_PROVIDERS` | `claude,codex` | Providers to poll |

Supported endpoints:

- **Anthropic** (`claude`) — `GET https://api.anthropic.com/api/oauth/usage`
  with `anthropic-beta: oauth-2025-04-20`. Yields 5h/7d utilization, active
  limits with severity, spend against cap, and extra-usage credits.
- **Codex** (`codex`) — `GET https://chatgpt.com/backend-api/wham/usage` with a
  `ChatGPT-Account-ID` header. Yields primary (5h) and secondary (7d) windows,
  metered add-on windows, and available rate-limit reset credits.

These are undocumented vendor endpoints. A poller that breaks logs a warning and
reports nothing rather than failing the cycle.

### Pricing (`pricing.*`)

| YAML | Environment | Default | Description |
| --- | --- | --- | --- |
| `pricing.enabled` | `CPA_PRICING_ENABLED` | `true` | Emit cost estimates |
| `pricing.path` | `CPA_PRICING_PATH` | *(bundled map)* | Your own price map |
| `pricing.fallback_to_bundled` | `CPA_PRICING_FALLBACK_TO_BUNDLED` | `true` | Overlay your file on the bundled entries |
| `pricing.currency` | `CPA_PRICING_CURRENCY` | `USD` | Value of the `currency` label |

Rates are per million tokens. Lookup is exact, then longest-prefix, so
`claude-sonnet-4-5` covers `claude-sonnet-4-5-20250929`. Requests whose model
has no entry increment `cpa_unpriced_requests_total` instead of being silently
counted as free.

```yaml
models:
  my-custom-model:
    input: 1.50
    output: 6.00
    cache_read: 0.15
    cache_write: 1.90
```

**Costs are estimates.** Subscription (OAuth) traffic is not metered per token
by the provider at all, so treat that portion as "what this traffic would have
cost on the API", not as an invoice.

## Sinks

### Prometheus (default)

Serves OpenMetrics on `prometheus.host:prometheus.port` at `/metrics`.

| YAML | Environment | Default |
| --- | --- | --- |
| `prometheus.host` | `CPA_PROMETHEUS_HOST` | `0.0.0.0` |
| `prometheus.port` | `CPA_PROMETHEUS_PORT` | `9185` |
| `prometheus.latency_buckets` | `CPA_PROMETHEUS_LATENCY_BUCKETS` | `0.1,0.25,0.5,1,2,5,10,30,60,120,300` |
| `prometheus.ttft_buckets` | `CPA_PROMETHEUS_TTFT_BUCKETS` | `0.1,0.25,0.5,1,2,5,10,30,60` |
| `prometheus.include_endpoint_label` | `CPA_PROMETHEUS_INCLUDE_ENDPOINT_LABEL` | `true` |
| `prometheus.include_api_key_label` | `CPA_PROMETHEUS_INCLUDE_API_KEY_LABEL` | `false` |

Metrics emitted:

| Metric | Type | Labels |
| --- | --- | --- |
| `cpa_requests_total` | counter | `provider`, `model`, `source`, `auth_index`, `auth_type`, `outcome`, `endpoint`\* |
| `cpa_request_errors_total` | counter | `provider`, `model`, `auth_index`, `status_code` |
| `cpa_tokens_total` | counter | `provider`, `model`, `auth_index`, `kind` |
| `cpa_estimated_cost_total` | counter | `provider`, `model`, `auth_index`, `currency` |
| `cpa_unpriced_requests_total` | counter | `provider`, `model` |
| `cpa_request_duration_seconds` | histogram | `provider`, `model` |
| `cpa_request_ttft_seconds` | histogram | `provider`, `model` |
| `cpa_rate_limit_utilization_ratio` | gauge | `provider`, `auth_index`, `window` |
| `cpa_account_up` | gauge | `provider`, `account`, `auth_index` |
| `cpa_account_state` | gauge | `provider`, `account`, `auth_index`, `status` |
| `cpa_account_requests` | gauge | `provider`, `account`, `auth_index`, `result` |
| `cpa_quota_used_ratio` | gauge | `provider`, `account`, `window`, `tier` |
| `cpa_quota_remaining_ratio` | gauge | `provider`, `account`, `window`, `tier` |
| `cpa_quota_resets_at_seconds` | gauge | `provider`, `account`, `window`, `tier` |
| `cpa_limit_percent` | gauge | `provider`, `account`, `kind`, `group`, `scope_model`, `severity` |
| `cpa_limit_active` | gauge | `provider`, `account`, `kind`, `group`, `scope_model` |
| `cpa_spend_used_dollars` | gauge | `provider`, `account` |
| `cpa_spend_limit_dollars` | gauge | `provider`, `account` |
| `cpa_extra_usage_credits_used` | gauge | `provider`, `account` |
| `cpa_extra_usage_credits_limit` | gauge | `provider`, `account` |
| `cpa_reset_credits_available` | gauge | `provider`, `account` |
| `cpa_exporter_usage_events_total` | counter | — |
| `cpa_exporter_collection_errors_total` | counter | `collector` |
| `cpa_exporter_last_success_timestamp_seconds` | gauge | `collector` |
| `cpa_exporter_collection_duration_seconds` | histogram | `collector` |

\* `endpoint` only when `include_endpoint_label` is true; `api_key` (redacted to
the last four characters) only when `include_api_key_label` is true.

`kind` on `cpa_tokens_total` is one of `input`, `output`, `reasoning`, `cached`,
`cache_read`, `cache_creation`.

Request IDs and timestamps are never used as labels. Enabling the `api_key`
label multiplies series by your number of client keys — leave it off unless you
need per-key attribution.

#### Example alerts

```yaml
groups:
  - name: cliproxyapi
    rules:
      - alert: CPAQuotaWindowNearlyExhausted
        expr: cpa_quota_used_ratio{window="7d"} > 0.9
        for: 10m
        annotations:
          summary: "{{ $labels.account }} has used {{ $value | humanizePercentage }} of its 7d window"

      - alert: CPAAccountDown
        expr: cpa_account_up == 0
        for: 15m
        annotations:
          summary: "Credential {{ $labels.account }} ({{ $labels.provider }}) is not routable"

      - alert: CPAHighErrorRate
        expr: |
          sum by (provider) (rate(cpa_requests_total{outcome="error"}[10m]))
            / sum by (provider) (rate(cpa_requests_total[10m])) > 0.1
        for: 10m

      - alert: CPAExporterStalled
        expr: time() - cpa_exporter_last_success_timestamp_seconds{collector="usage_queue"} > 300
        for: 5m
        annotations:
          summary: "Usage queue has not been drained in 5m; records are being dropped"
```

### BigQuery

A port of the original drain behaviour, for long-retention SQL analysis. Install
the extra: `pip install 'cpa-usage-exporter[bigquery]'`.

| YAML | Environment | Default | Description |
| --- | --- | --- | --- |
| `bigquery.project` | `CPA_BIGQUERY_PROJECT` | *(required)* | GCP project |
| `bigquery.dataset` | `CPA_BIGQUERY_DATASET` | *(required)* | Dataset, must already exist |
| `bigquery.location` | `CPA_BIGQUERY_LOCATION` | *(client default)* | Dataset location |
| `bigquery.table_prefix` | `CPA_BIGQUERY_TABLE_PREFIX` | `cliproxy_` | Prefix for created tables |
| `bigquery.credentials_file` | `CPA_BIGQUERY_CREDENTIALS_FILE` | *(ADC)* | Service account JSON; omit to use Application Default Credentials |

Four day-partitioned tables are created on first use and gain columns as the
schema grows: `<prefix>requests`, `<prefix>accounts`, `<prefix>quota`,
`<prefix>limits`. The service account needs `roles/bigquery.dataEditor` on the
dataset.

## Dashboard

[`dashboards/cpa-usage-prometheus.json`](dashboards/cpa-usage-prometheus.json)
targets the Prometheus sink. Import it in Grafana via **Dashboards → New →
Import**, upload the JSON, and pick your Prometheus data source when prompted.

It covers subscription quota and burn pace, estimated cost, traffic and routing,
latency p50/p95 with TTFT, token and cache accounting, and account health. Every
panel reads a `DS_PROMETHEUS` data source input and filters on `provider`,
`model` and `account` template variables, so nothing is bound to a particular
environment.

If quota polling is off, the quota row stays empty — everything else works from
the usage queue alone.

## Development

```bash
pip install -e .
python -m compileall src
cpa-usage-exporter --help
```

## License

Apache-2.0
