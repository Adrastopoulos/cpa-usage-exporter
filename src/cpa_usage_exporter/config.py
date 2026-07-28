"""Configuration: optional YAML file overlaid with environment variables.

Precedence, lowest to highest: built-in defaults, YAML file, environment.
Nothing here carries a deployment-specific default; every identifier that names
a project, dataset, host or account must be supplied by the operator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

ENV_PREFIX = "CPA_"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised when configuration is missing or self-contradictory."""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"expected a boolean, got {value!r}")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


@dataclass(slots=True)
class CPAConfig:
    """How to reach the CLIProxyAPI management API."""

    management_url: str = "http://127.0.0.1:8317/v0/management"
    management_key: str = ""
    queue_batch: int = 1000
    queue_max_batches: int = 20
    timeout_seconds: float = 30.0
    verify_tls: bool = True
    collect_usage_queue: bool = True
    collect_auth_files: bool = True


@dataclass(slots=True)
class QuotaConfig:
    """Provider subscription-quota polling.

    Credentials are read from the CPA auth directory: each ``*.json`` file holds
    an ``access_token`` plus a ``type`` naming the provider.
    """

    enabled: bool = False
    auth_dir: str = ""
    interval_seconds: int = 300
    timeout_seconds: float = 20.0
    providers: list[str] = field(default_factory=lambda: ["claude", "codex"])


@dataclass(slots=True)
class PrometheusConfig:
    host: str = "0.0.0.0"
    port: int = 9185
    # Streaming histogram buckets, in seconds, for latency and time-to-first-token.
    latency_buckets: list[float] = field(
        default_factory=lambda: [0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300]
    )
    ttft_buckets: list[float] = field(default_factory=lambda: [0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60])
    # Label cardinality guards. Request-scoped identifiers are never labels.
    include_api_key_label: bool = False
    include_endpoint_label: bool = True


@dataclass(slots=True)
class BigQueryConfig:
    project: str = ""
    dataset: str = ""
    location: str = ""
    table_prefix: str = "cliproxy_"
    credentials_file: str = ""


@dataclass(slots=True)
class PricingConfig:
    """Model price map used for cost estimation.

    ``path`` overrides the bundled default map; ``fallback_to_bundled`` keeps the
    bundled entries as a base layer that the operator file overlays.
    """

    enabled: bool = True
    path: str = ""
    fallback_to_bundled: bool = True
    currency: str = "USD"


@dataclass(slots=True)
class Config:
    sink: str = "prometheus"
    interval_seconds: int = 60
    log_level: str = "INFO"
    cpa: CPAConfig = field(default_factory=CPAConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    bigquery: BigQueryConfig = field(default_factory=BigQueryConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)

    def validate(self) -> None:
        if self.sink not in ("prometheus", "bigquery"):
            raise ConfigError(f"unknown sink {self.sink!r}; expected 'prometheus' or 'bigquery'")
        if not self.cpa.management_key:
            raise ConfigError("CPA management key is required (set CPA_MANAGEMENT_KEY)")
        if not self.cpa.management_url:
            raise ConfigError("CPA management URL is required (set CPA_MANAGEMENT_URL)")
        if self.interval_seconds <= 0:
            raise ConfigError("interval_seconds must be positive")
        if self.cpa.queue_batch <= 0:
            raise ConfigError("cpa.queue_batch must be positive")
        if self.quota.enabled and not self.quota.auth_dir:
            raise ConfigError("quota.auth_dir is required when quota polling is enabled")
        if self.sink == "bigquery":
            if not self.bigquery.project:
                raise ConfigError("bigquery.project is required for the bigquery sink")
            if not self.bigquery.dataset:
                raise ConfigError("bigquery.dataset is required for the bigquery sink")


# Every knob, as (dotted path, environment variable, coercion).
_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("sink", "SINK", str),
    ("interval_seconds", "INTERVAL_SECONDS", int),
    ("log_level", "LOG_LEVEL", str),
    ("cpa.management_url", "MANAGEMENT_URL", str),
    ("cpa.management_key", "MANAGEMENT_KEY", str),
    ("cpa.queue_batch", "QUEUE_BATCH", int),
    ("cpa.queue_max_batches", "QUEUE_MAX_BATCHES", int),
    ("cpa.timeout_seconds", "HTTP_TIMEOUT_SECONDS", float),
    ("cpa.verify_tls", "VERIFY_TLS", _as_bool),
    ("cpa.collect_usage_queue", "COLLECT_USAGE_QUEUE", _as_bool),
    ("cpa.collect_auth_files", "COLLECT_AUTH_FILES", _as_bool),
    ("quota.enabled", "QUOTA_ENABLED", _as_bool),
    ("quota.auth_dir", "QUOTA_AUTH_DIR", str),
    ("quota.interval_seconds", "QUOTA_INTERVAL_SECONDS", int),
    ("quota.timeout_seconds", "QUOTA_TIMEOUT_SECONDS", float),
    ("quota.providers", "QUOTA_PROVIDERS", _as_list),
    ("prometheus.host", "PROMETHEUS_HOST", str),
    ("prometheus.port", "PROMETHEUS_PORT", int),
    ("prometheus.latency_buckets", "PROMETHEUS_LATENCY_BUCKETS", lambda v: [float(x) for x in _as_list(v)]),
    ("prometheus.ttft_buckets", "PROMETHEUS_TTFT_BUCKETS", lambda v: [float(x) for x in _as_list(v)]),
    ("prometheus.include_api_key_label", "PROMETHEUS_INCLUDE_API_KEY_LABEL", _as_bool),
    ("prometheus.include_endpoint_label", "PROMETHEUS_INCLUDE_ENDPOINT_LABEL", _as_bool),
    ("bigquery.project", "BIGQUERY_PROJECT", str),
    ("bigquery.dataset", "BIGQUERY_DATASET", str),
    ("bigquery.location", "BIGQUERY_LOCATION", str),
    ("bigquery.table_prefix", "BIGQUERY_TABLE_PREFIX", str),
    ("bigquery.credentials_file", "BIGQUERY_CREDENTIALS_FILE", str),
    ("pricing.enabled", "PRICING_ENABLED", _as_bool),
    ("pricing.path", "PRICING_PATH", str),
    ("pricing.fallback_to_bundled", "PRICING_FALLBACK_TO_BUNDLED", _as_bool),
    ("pricing.currency", "PRICING_CURRENCY", str),
)


def _dig(mapping: Any, path: list[str]) -> tuple[bool, Any]:
    cursor = mapping
    for part in path:
        if not isinstance(cursor, dict) or part not in cursor:
            return False, None
        cursor = cursor[part]
    return True, cursor


def _assign(config: Config, path: list[str], value: Any) -> None:
    target: Any = config
    for part in path[:-1]:
        target = getattr(target, part)
    setattr(target, path[-1], value)


def _unknown_keys(data: Any, spec: Any, prefix: str = "") -> list[str]:
    """Report YAML keys that do not map onto a config field, so typos surface."""
    if not isinstance(data, dict) or not is_dataclass(spec):
        return []
    known = {f.name: f for f in fields(spec)}
    unknown: list[str] = []
    for key, value in data.items():
        if key not in known:
            unknown.append(f"{prefix}{key}")
            continue
        nested = getattr(spec, key)
        if is_dataclass(nested):
            unknown.extend(_unknown_keys(value, nested, f"{prefix}{key}."))
    return unknown


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Build a :class:`Config` from an optional YAML file plus the environment."""
    config = Config()

    file_path = path or os.environ.get(f"{ENV_PREFIX}CONFIG_FILE") or ""
    document: dict[str, Any] = {}
    if file_path:
        resolved = Path(file_path).expanduser()
        if not resolved.is_file():
            raise ConfigError(f"config file not found: {resolved}")
        loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"config file {resolved} must contain a YAML mapping")
        document = loaded
        unknown = _unknown_keys(document, config)
        if unknown:
            raise ConfigError(f"unknown config keys in {resolved}: {', '.join(sorted(unknown))}")

    for dotted, env_suffix, coerce in _FIELDS:
        path_parts = dotted.split(".")
        found, value = _dig(document, path_parts)
        env_value = os.environ.get(ENV_PREFIX + env_suffix)
        if env_value is not None:
            found, value = True, env_value
        if not found or value is None:
            continue
        try:
            _assign(config, path_parts, coerce(value))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid value for {dotted}: {value!r} ({exc})") from exc

    config.sink = config.sink.strip().lower()
    config.log_level = config.log_level.strip().upper()
    config.cpa.management_url = config.cpa.management_url.rstrip("/")
    return config
