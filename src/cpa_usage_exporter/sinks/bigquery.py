"""BigQuery sink.

Streams each record type into its own table, creating tables on first use and
adding columns when the schema grows. Table names are the configured prefix plus
``requests``, ``accounts``, ``quota`` and ``limits``.

The ``google-cloud-bigquery`` dependency is optional and imported lazily, so a
Prometheus-only deployment does not need it installed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..config import BigQueryConfig
from ..models import AccountHealth, ProviderLimit, QuotaWindow, UsageEvent
from ..pricing import PriceBook
from .base import Sink

log = logging.getLogger(__name__)

REQUESTS_TABLE = "requests"
ACCOUNTS_TABLE = "accounts"
QUOTA_TABLE = "quota"
LIMITS_TABLE = "limits"

# (name, type) per column. Everything is NULLABLE: providers omit fields freely
# and a missing value must not reject the row.
SCHEMAS: dict[str, tuple[tuple[str, str], ...]] = {
    REQUESTS_TABLE: (
        ("timestamp", "TIMESTAMP"),
        ("provider", "STRING"),
        ("model", "STRING"),
        ("alias", "STRING"),
        ("executor_type", "STRING"),
        ("endpoint", "STRING"),
        ("source", "STRING"),
        ("auth_index", "STRING"),
        ("auth_type", "STRING"),
        ("api_key", "STRING"),
        ("request_id", "STRING"),
        ("service_tier", "STRING"),
        ("reasoning_effort", "STRING"),
        ("input_tokens", "INT64"),
        ("output_tokens", "INT64"),
        ("reasoning_tokens", "INT64"),
        ("cached_tokens", "INT64"),
        ("cache_read_tokens", "INT64"),
        ("cache_creation_tokens", "INT64"),
        ("total_tokens", "INT64"),
        ("estimated_cost", "FLOAT64"),
        ("failed", "BOOL"),
        ("status_code", "INT64"),
        ("latency_ms", "INT64"),
        ("ttft_ms", "INT64"),
        ("rl_5h_utilization", "FLOAT64"),
        ("rl_5h_status", "STRING"),
        ("rl_7d_utilization", "FLOAT64"),
        ("rl_7d_status", "STRING"),
    ),
    ACCOUNTS_TABLE: (
        ("recorded_at", "TIMESTAMP"),
        ("auth_index", "STRING"),
        ("provider", "STRING"),
        ("account", "STRING"),
        ("label", "STRING"),
        ("status", "STRING"),
        ("status_message", "STRING"),
        ("unavailable", "BOOL"),
        ("disabled", "BOOL"),
        ("success", "INT64"),
        ("failed", "INT64"),
    ),
    QUOTA_TABLE: (
        ("recorded_at", "TIMESTAMP"),
        ("provider", "STRING"),
        ("account", "STRING"),
        ("tier", "STRING"),
        ("window_id", "STRING"),
        ("used_fraction", "FLOAT64"),
        ("remaining_fraction", "FLOAT64"),
        ("resets_at", "TIMESTAMP"),
        ("status", "STRING"),
    ),
    LIMITS_TABLE: (
        ("recorded_at", "TIMESTAMP"),
        ("provider", "STRING"),
        ("account", "STRING"),
        ("kind", "STRING"),
        ("limit_group", "STRING"),
        ("scope_model", "STRING"),
        ("percent", "FLOAT64"),
        ("severity", "STRING"),
        ("is_active", "BOOL"),
        ("resets_at", "TIMESTAMP"),
        ("used_dollars", "FLOAT64"),
        ("limit_dollars", "FLOAT64"),
        ("extra_used_credits", "FLOAT64"),
        ("extra_limit_credits", "FLOAT64"),
        ("reset_credits", "INT64"),
    ),
}

# Time column each table is partitioned on, to keep scans over long retention cheap.
PARTITION_FIELDS = {
    REQUESTS_TABLE: "timestamp",
    ACCOUNTS_TABLE: "recorded_at",
    QUOTA_TABLE: "recorded_at",
    LIMITS_TABLE: "recorded_at",
}


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


class BigQuerySink(Sink):
    name = "bigquery"

    def __init__(self, config: BigQueryConfig, *, price_book: PriceBook | None = None) -> None:
        self.config = config
        self.price_book = price_book
        self._client: Any = None
        self._ready: set[str] = set()

    def _bigquery(self) -> Any:
        try:
            from google.cloud import bigquery
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "the bigquery sink requires the 'bigquery' extra: "
                "pip install 'cpa-usage-exporter[bigquery]'"
            ) from exc
        return bigquery

    def start(self) -> None:
        bigquery = self._bigquery()
        if self.config.credentials_file:
            self._client = bigquery.Client.from_service_account_json(
                self.config.credentials_file,
                project=self.config.project,
                location=self.config.location or None,
            )
        else:
            self._client = bigquery.Client(
                project=self.config.project, location=self.config.location or None
            )
        for table in SCHEMAS:
            self._ensure_table(table)
        log.info(
            "bigquery sink writing to %s.%s (prefix %r)",
            self.config.project,
            self.config.dataset,
            self.config.table_prefix,
        )

    def _table_id(self, table: str) -> str:
        return f"{self.config.project}.{self.config.dataset}.{self.config.table_prefix}{table}"

    def _ensure_table(self, table: str) -> None:
        if table in self._ready:
            return
        bigquery = self._bigquery()
        schema = [
            bigquery.SchemaField(name, field_type, mode="NULLABLE")
            for name, field_type in SCHEMAS[table]
        ]
        table_id = self._table_id(table)
        try:
            existing = self._client.get_table(table_id)
        except Exception:  # noqa: BLE001 - google.api_core.NotFound and friends
            target = bigquery.Table(table_id, schema=schema)
            partition_field = PARTITION_FIELDS.get(table)
            if partition_field:
                target.time_partitioning = bigquery.TimePartitioning(
                    type_=bigquery.TimePartitioningType.DAY, field=partition_field
                )
            self._client.create_table(target, exists_ok=True)
            log.info("created table %s", table_id)
            self._ready.add(table)
            return
        known = {field.name for field in existing.schema}
        additions = [field for field in schema if field.name not in known]
        if additions:
            existing.schema = list(existing.schema) + additions
            self._client.update_table(existing, ["schema"])
            log.info("added %d column(s) to %s", len(additions), table_id)
        self._ready.add(table)

    def _insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        self._ensure_table(table)
        errors = self._client.insert_rows_json(self._table_id(table), rows)
        if errors:
            log.error("bigquery rejected %d row(s) for %s: %s", len(errors), table, errors[:3])

    def record_usage(self, events: list[UsageEvent]) -> None:
        rows = []
        for event in events:
            rl_5h = event.rate_limits.get("5h", (None, None))
            rl_7d = event.rate_limits.get("7d", (None, None))
            cost = (
                self.price_book.estimate_cost(event.model, event.tokens)
                if self.price_book is not None
                else None
            )
            rows.append(
                {
                    "timestamp": _iso(event.timestamp),
                    "provider": event.provider,
                    "model": event.model,
                    "alias": event.alias,
                    "executor_type": event.executor_type,
                    "endpoint": event.endpoint,
                    "source": event.source,
                    "auth_index": event.auth_index,
                    "auth_type": event.auth_type,
                    "api_key": event.api_key,
                    "request_id": event.request_id,
                    "service_tier": event.service_tier,
                    "reasoning_effort": event.reasoning_effort,
                    "input_tokens": event.tokens.input_tokens,
                    "output_tokens": event.tokens.output_tokens,
                    "reasoning_tokens": event.tokens.reasoning_tokens,
                    "cached_tokens": event.tokens.cached_tokens,
                    "cache_read_tokens": event.tokens.cache_read_tokens,
                    "cache_creation_tokens": event.tokens.cache_creation_tokens,
                    "total_tokens": event.tokens.total_tokens,
                    "estimated_cost": cost,
                    "failed": event.failed,
                    "status_code": event.status_code,
                    "latency_ms": event.latency_ms,
                    "ttft_ms": event.ttft_ms,
                    "rl_5h_utilization": rl_5h[0],
                    "rl_5h_status": rl_5h[1],
                    "rl_7d_utilization": rl_7d[0],
                    "rl_7d_status": rl_7d[1],
                }
            )
        self._insert(REQUESTS_TABLE, rows)

    def record_accounts(self, accounts: list[AccountHealth]) -> None:
        self._insert(
            ACCOUNTS_TABLE,
            [
                {
                    "recorded_at": _iso(account.recorded_at),
                    "auth_index": account.auth_index,
                    "provider": account.provider,
                    "account": account.account,
                    "label": account.label,
                    "status": account.status,
                    "status_message": account.status_message,
                    "unavailable": account.unavailable,
                    "disabled": account.disabled,
                    "success": account.success,
                    "failed": account.failed,
                }
                for account in accounts
            ],
        )

    def record_quota(self, windows: list[QuotaWindow]) -> None:
        self._insert(
            QUOTA_TABLE,
            [
                {
                    "recorded_at": _iso(window.recorded_at),
                    "provider": window.provider,
                    "account": window.account,
                    "tier": window.tier,
                    "window_id": window.window,
                    "used_fraction": window.used_fraction,
                    "remaining_fraction": window.remaining_fraction,
                    "resets_at": _iso(window.resets_at),
                    "status": window.status,
                }
                for window in windows
            ],
        )

    def record_limits(self, limits: list[ProviderLimit]) -> None:
        self._insert(
            LIMITS_TABLE,
            [
                {
                    "recorded_at": _iso(limit.recorded_at),
                    "provider": limit.provider,
                    "account": limit.account,
                    "kind": limit.kind,
                    "limit_group": limit.group,
                    "scope_model": limit.scope_model,
                    "percent": limit.percent,
                    "severity": limit.severity,
                    "is_active": limit.is_active,
                    "resets_at": _iso(limit.resets_at),
                    "used_dollars": limit.used_dollars,
                    "limit_dollars": limit.limit_dollars,
                    "extra_used_credits": limit.extra_used_credits,
                    "extra_limit_credits": limit.extra_limit_credits,
                    "reset_credits": limit.reset_credits,
                }
                for limit in limits
            ],
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
