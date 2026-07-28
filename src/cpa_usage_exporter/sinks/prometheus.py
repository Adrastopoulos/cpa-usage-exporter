"""Prometheus / OpenMetrics sink.

Serves a scrape endpoint on ``/metrics``. Per-request usage events fold into
counters and histograms; snapshot data (account health, provider quota) lands in
gauges that are cleared and repopulated each cycle so credentials that disappear
stop reporting instead of going stale.

Request-scoped identifiers — request IDs, timestamps, full API keys — are never
used as labels. The API key label is opt-in and carries only a redacted suffix.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from ..config import PrometheusConfig
from ..models import AccountHealth, ProviderLimit, QuotaWindow, UsageEvent
from ..pricing import PriceBook
from .base import Sink

log = logging.getLogger(__name__)

NAMESPACE = "cpa"


def _label(value: Any) -> str:
    """Labels must be strings; absent dimensions become the empty string."""
    if value is None:
        return ""
    return str(value)


class PrometheusSink(Sink):
    name = "prometheus"

    def __init__(
        self,
        config: PrometheusConfig,
        *,
        price_book: PriceBook | None = None,
        currency: str = "USD",
        registry: CollectorRegistry | None = None,
    ) -> None:
        self.config = config
        self.price_book = price_book
        self.currency = currency
        self.registry = registry if registry is not None else CollectorRegistry()
        self._server: Any = None

        request_labels = ["provider", "model", "source", "auth_index", "auth_type", "outcome"]
        if config.include_endpoint_label:
            request_labels.append("endpoint")
        if config.include_api_key_label:
            request_labels.append("api_key")
        self._request_labels = request_labels

        self.requests = Counter(
            f"{NAMESPACE}_requests_total",
            "Requests observed in the CLIProxyAPI usage queue.",
            request_labels,
            registry=self.registry,
        )
        self.request_errors = Counter(
            f"{NAMESPACE}_request_errors_total",
            "Failed requests, labelled by the upstream HTTP status code.",
            ["provider", "model", "auth_index", "status_code"],
            registry=self.registry,
        )
        self.tokens = Counter(
            f"{NAMESPACE}_tokens_total",
            "Tokens accounted per request, split by kind.",
            ["provider", "model", "auth_index", "kind"],
            registry=self.registry,
        )
        self.cost = Counter(
            f"{NAMESPACE}_estimated_cost_total",
            "Estimated request cost from the configured model price map.",
            ["provider", "model", "auth_index", "currency"],
            registry=self.registry,
        )
        self.unpriced_requests = Counter(
            f"{NAMESPACE}_unpriced_requests_total",
            "Requests whose model has no entry in the price map.",
            ["provider", "model"],
            registry=self.registry,
        )
        self.latency = Histogram(
            f"{NAMESPACE}_request_duration_seconds",
            "End-to-end request latency reported by CLIProxyAPI.",
            ["provider", "model"],
            buckets=tuple(config.latency_buckets),
            registry=self.registry,
        )
        self.ttft = Histogram(
            f"{NAMESPACE}_request_ttft_seconds",
            "Time to first token for streaming requests.",
            ["provider", "model"],
            buckets=tuple(config.ttft_buckets),
            registry=self.registry,
        )
        self.rate_limit_utilization = Gauge(
            f"{NAMESPACE}_rate_limit_utilization_ratio",
            "Live rate-limit utilization (0-1) read from upstream response headers.",
            ["provider", "auth_index", "window"],
            registry=self.registry,
        )

        self.account_up = Gauge(
            f"{NAMESPACE}_account_up",
            "1 when a credential is selectable for routing, 0 when disabled or unavailable.",
            ["provider", "account", "auth_index"],
            registry=self.registry,
        )
        self.account_state = Gauge(
            f"{NAMESPACE}_account_state",
            "1 for the credential's current status string, 0 for the others.",
            ["provider", "account", "auth_index", "status"],
            registry=self.registry,
        )
        self.account_requests = Gauge(
            f"{NAMESPACE}_account_requests",
            "Per-credential request counters as reported by CLIProxyAPI since its last restart.",
            ["provider", "account", "auth_index", "result"],
            registry=self.registry,
        )

        self.quota_used = Gauge(
            f"{NAMESPACE}_quota_used_ratio",
            "Fraction (0-1) of a provider subscription window consumed.",
            ["provider", "account", "window", "tier"],
            registry=self.registry,
        )
        self.quota_remaining = Gauge(
            f"{NAMESPACE}_quota_remaining_ratio",
            "Fraction (0-1) of a provider subscription window still available.",
            ["provider", "account", "window", "tier"],
            registry=self.registry,
        )
        self.quota_resets_at = Gauge(
            f"{NAMESPACE}_quota_resets_at_seconds",
            "Unix timestamp at which a provider subscription window resets.",
            ["provider", "account", "window", "tier"],
            registry=self.registry,
        )
        self.limit_percent = Gauge(
            f"{NAMESPACE}_limit_percent",
            "Percent (0-100) consumed of a provider-reported limit.",
            ["provider", "account", "kind", "group", "scope_model", "severity"],
            registry=self.registry,
        )
        self.limit_active = Gauge(
            f"{NAMESPACE}_limit_active",
            "1 while a provider-reported limit is actively constraining traffic.",
            ["provider", "account", "kind", "group", "scope_model"],
            registry=self.registry,
        )
        self.spend_used = Gauge(
            f"{NAMESPACE}_spend_used_dollars",
            "Spend consumed against a provider-reported cap.",
            ["provider", "account"],
            registry=self.registry,
        )
        self.spend_limit = Gauge(
            f"{NAMESPACE}_spend_limit_dollars",
            "Provider-reported spend cap.",
            ["provider", "account"],
            registry=self.registry,
        )
        self.extra_credits_used = Gauge(
            f"{NAMESPACE}_extra_usage_credits_used",
            "Extra-usage credits consumed this period.",
            ["provider", "account"],
            registry=self.registry,
        )
        self.extra_credits_limit = Gauge(
            f"{NAMESPACE}_extra_usage_credits_limit",
            "Extra-usage credit allowance for this period.",
            ["provider", "account"],
            registry=self.registry,
        )
        self.reset_credits = Gauge(
            f"{NAMESPACE}_reset_credits_available",
            "Rate-limit reset credits a provider still grants this period.",
            ["provider", "account"],
            registry=self.registry,
        )

        self.usage_events = Counter(
            f"{NAMESPACE}_exporter_usage_events_total",
            "Usage-queue records consumed by this exporter.",
            registry=self.registry,
        )
        self.collection_errors = Counter(
            f"{NAMESPACE}_exporter_collection_errors_total",
            "Failed collection attempts, by collector.",
            ["collector"],
            registry=self.registry,
        )
        self.last_success = Gauge(
            f"{NAMESPACE}_exporter_last_success_timestamp_seconds",
            "Unix timestamp of the last successful collection, by collector.",
            ["collector"],
            registry=self.registry,
        )
        self.collection_duration = Histogram(
            f"{NAMESPACE}_exporter_collection_duration_seconds",
            "Wall-clock duration of a collection cycle, by collector.",
            ["collector"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
            registry=self.registry,
        )

    def start(self) -> None:
        self._server = start_http_server(
            self.config.port, addr=self.config.host, registry=self.registry
        )
        log.info(
            "prometheus sink listening on http://%s:%d/metrics", self.config.host, self.config.port
        )

    def record_usage(self, events: list[UsageEvent]) -> None:
        for event in events:
            self._record_one(event)
        if events:
            self.usage_events.inc(len(events))

    def _record_one(self, event: UsageEvent) -> None:
        outcome = "error" if event.failed else "success"
        labels: dict[str, str] = {
            "provider": _label(event.provider),
            "model": _label(event.model),
            "source": _label(event.source),
            "auth_index": _label(event.auth_index),
            "auth_type": _label(event.auth_type),
            "outcome": outcome,
        }
        if self.config.include_endpoint_label:
            labels["endpoint"] = _label(event.endpoint)
        if self.config.include_api_key_label:
            labels["api_key"] = _label(event.api_key)
        self.requests.labels(**labels).inc()

        if event.failed:
            self.request_errors.labels(
                provider=_label(event.provider),
                model=_label(event.model),
                auth_index=_label(event.auth_index),
                status_code=_label(event.status_code or 0),
            ).inc()

        for kind, count in event.tokens.as_mapping().items():
            if count:
                self.tokens.labels(
                    provider=_label(event.provider),
                    model=_label(event.model),
                    auth_index=_label(event.auth_index),
                    kind=kind,
                ).inc(count)

        if self.price_book is not None:
            estimate = self.price_book.estimate_cost(event.model, event.tokens)
            if estimate is None:
                self.unpriced_requests.labels(
                    provider=_label(event.provider), model=_label(event.model)
                ).inc()
            elif estimate > 0:
                self.cost.labels(
                    provider=_label(event.provider),
                    model=_label(event.model),
                    auth_index=_label(event.auth_index),
                    currency=self.currency,
                ).inc(estimate)

        if event.latency_ms > 0:
            self.latency.labels(
                provider=_label(event.provider), model=_label(event.model)
            ).observe(event.latency_ms / 1000.0)
        if event.ttft_ms and event.ttft_ms > 0:
            self.ttft.labels(provider=_label(event.provider), model=_label(event.model)).observe(
                event.ttft_ms / 1000.0
            )

        for window, (utilization, _status) in event.rate_limits.items():
            if utilization is None:
                continue
            self.rate_limit_utilization.labels(
                provider=_label(event.provider),
                auth_index=_label(event.auth_index),
                window=window,
            ).set(utilization)

    def record_accounts(self, accounts: list[AccountHealth]) -> None:
        self.account_up.clear()
        self.account_state.clear()
        self.account_requests.clear()
        for account in accounts:
            identity = {
                "provider": _label(account.provider),
                "account": _label(account.account),
                "auth_index": _label(account.auth_index),
            }
            healthy = not (account.disabled or account.unavailable)
            self.account_up.labels(**identity).set(1 if healthy else 0)
            self.account_state.labels(**identity, status=_label(account.status) or "unknown").set(1)
            self.account_requests.labels(**identity, result="success").set(account.success)
            self.account_requests.labels(**identity, result="failed").set(account.failed)

    def record_quota(self, windows: list[QuotaWindow]) -> None:
        self.quota_used.clear()
        self.quota_remaining.clear()
        self.quota_resets_at.clear()
        for window in windows:
            labels = {
                "provider": _label(window.provider),
                "account": _label(window.account),
                "window": _label(window.window),
                "tier": _label(window.tier),
            }
            self.quota_used.labels(**labels).set(window.used_fraction)
            self.quota_remaining.labels(**labels).set(window.remaining_fraction)
            if window.resets_at is not None:
                self.quota_resets_at.labels(**labels).set(window.resets_at.timestamp())

    def record_limits(self, limits: list[ProviderLimit]) -> None:
        self.limit_percent.clear()
        self.limit_active.clear()
        self.spend_used.clear()
        self.spend_limit.clear()
        self.extra_credits_used.clear()
        self.extra_credits_limit.clear()
        self.reset_credits.clear()
        for limit in limits:
            account = {"provider": _label(limit.provider), "account": _label(limit.account)}
            scope = {
                **account,
                "kind": _label(limit.kind),
                "group": _label(limit.group),
                "scope_model": _label(limit.scope_model),
            }
            if limit.percent is not None:
                self.limit_percent.labels(**scope, severity=_label(limit.severity)).set(limit.percent)
            if limit.is_active is not None:
                self.limit_active.labels(**scope).set(1 if limit.is_active else 0)
            if limit.used_dollars is not None:
                self.spend_used.labels(**account).set(limit.used_dollars)
            if limit.limit_dollars is not None:
                self.spend_limit.labels(**account).set(limit.limit_dollars)
            if limit.extra_used_credits is not None:
                self.extra_credits_used.labels(**account).set(limit.extra_used_credits)
            if limit.extra_limit_credits is not None:
                self.extra_credits_limit.labels(**account).set(limit.extra_limit_credits)
            if limit.reset_credits is not None:
                self.reset_credits.labels(**account).set(limit.reset_credits)

    def observe_collection(self, collector: str, duration: float, ok: bool) -> None:
        self.collection_duration.labels(collector=collector).observe(duration)
        if ok:
            self.last_success.labels(collector=collector).set(time.time())
        else:
            self.collection_errors.labels(collector=collector).inc()

    def close(self) -> None:
        server, thread = (self._server if isinstance(self._server, tuple) else (None, None))
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self._server = None
