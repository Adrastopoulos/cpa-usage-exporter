"""Normalized records handed to sinks.

The wire formats these are parsed from are CLIProxyAPI's usage queue records and
``/v0/management/auth-files`` entries, plus provider quota endpoints. Sinks only
ever see these dataclasses, never raw payloads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _header_first(headers: Any, name: str) -> str | None:
    """Read one value out of a Go ``http.Header`` (name -> list of values)."""
    if not isinstance(headers, dict):
        return None
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == lowered:
                value = candidate
                break
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return _str(value)


def redact_key(key: Any) -> str | None:
    """Keep only the last four characters of an API key."""
    text = _str(key)
    if text is None:
        return None
    return "..." + text[-4:]


# Go marshals time.Time as RFC 3339 with up to nanosecond precision. datetime
# only holds microseconds, and fromisoformat rejects >6 fractional digits before
# Python 3.11, so truncate the fraction ourselves.
_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_timestamp(value: Any) -> datetime | None:
    text = _str(value)
    if text is None:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def epoch_to_datetime(value: Any) -> datetime | None:
    seconds = _float(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


@dataclass(slots=True)
class TokenCounts:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: Any) -> TokenCounts:
        data = payload if isinstance(payload, dict) else {}
        return cls(
            input_tokens=_int(data.get("input_tokens"), 0) or 0,
            output_tokens=_int(data.get("output_tokens"), 0) or 0,
            reasoning_tokens=_int(data.get("reasoning_tokens"), 0) or 0,
            cached_tokens=_int(data.get("cached_tokens"), 0) or 0,
            cache_read_tokens=_int(data.get("cache_read_tokens"), 0) or 0,
            cache_creation_tokens=_int(data.get("cache_creation_tokens"), 0) or 0,
            total_tokens=_int(data.get("total_tokens"), 0) or 0,
        )

    def as_mapping(self) -> dict[str, int]:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "reasoning": self.reasoning_tokens,
            "cached": self.cached_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_creation": self.cache_creation_tokens,
        }


# Response headers Anthropic sets on every proxied request; they expose the live
# unified rate-limit windows without an extra API call.
RATE_LIMIT_HEADERS = {
    "5h": (
        "Anthropic-Ratelimit-Unified-5h-Utilization",
        "Anthropic-Ratelimit-Unified-5h-Status",
    ),
    "7d": (
        "Anthropic-Ratelimit-Unified-7d-Utilization",
        "Anthropic-Ratelimit-Unified-7d-Status",
    ),
}


@dataclass(slots=True)
class UsageEvent:
    """One per-request record drained from the CPA usage queue."""

    timestamp: datetime | None = None
    provider: str = "unknown"
    model: str = "unknown"
    alias: str | None = None
    executor_type: str | None = None
    endpoint: str | None = None
    source: str | None = None
    auth_index: str = ""
    auth_type: str | None = None
    api_key: str | None = None
    request_id: str | None = None
    service_tier: str | None = None
    reasoning_effort: str | None = None
    tokens: TokenCounts = field(default_factory=TokenCounts)
    failed: bool = False
    status_code: int | None = None
    latency_ms: int = 0
    ttft_ms: int | None = None
    rate_limits: dict[str, tuple[float | None, str | None]] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> UsageEvent:
        fail = payload.get("fail") if isinstance(payload.get("fail"), dict) else {}
        headers = payload.get("response_headers")
        rate_limits: dict[str, tuple[float | None, str | None]] = {}
        for window, (util_header, status_header) in RATE_LIMIT_HEADERS.items():
            utilization = _float(_header_first(headers, util_header))
            status = _header_first(headers, status_header)
            if utilization is not None or status is not None:
                rate_limits[window] = (utilization, status)
        return cls(
            timestamp=parse_timestamp(payload.get("timestamp")),
            provider=_str(payload.get("provider")) or "unknown",
            model=_str(payload.get("model")) or "unknown",
            alias=_str(payload.get("alias")),
            executor_type=_str(payload.get("executor_type")),
            endpoint=_str(payload.get("endpoint")),
            source=_str(payload.get("source")),
            auth_index=_str(payload.get("auth_index")) or "",
            auth_type=_str(payload.get("auth_type")),
            api_key=redact_key(payload.get("api_key")),
            request_id=_str(payload.get("request_id")),
            service_tier=_str(payload.get("response_service_tier")) or _str(payload.get("service_tier")),
            reasoning_effort=_str(payload.get("reasoning_effort")),
            tokens=TokenCounts.from_payload(payload.get("tokens")),
            failed=bool(payload.get("failed", False)),
            status_code=_int(fail.get("status_code")) or None,
            latency_ms=_int(payload.get("latency_ms"), 0) or 0,
            ttft_ms=_int(payload.get("ttft_ms")) or None,
            rate_limits=rate_limits,
        )


@dataclass(slots=True)
class AccountHealth:
    """One entry from ``/v0/management/auth-files``."""

    recorded_at: datetime
    auth_index: str
    provider: str = "unknown"
    account: str | None = None
    label: str | None = None
    status: str | None = None
    status_message: str | None = None
    unavailable: bool = False
    disabled: bool = False
    success: int = 0
    failed: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any], recorded_at: datetime) -> AccountHealth:
        provider = _str(payload.get("provider")) or _str(payload.get("type")) or "unknown"
        account = (
            _str(payload.get("email"))
            or _str(payload.get("account"))
            or _str(payload.get("name"))
            or _str(payload.get("id"))
        )
        return cls(
            recorded_at=recorded_at,
            auth_index=_str(payload.get("auth_index")) or "",
            provider=provider,
            account=account,
            label=_str(payload.get("label")),
            status=_str(payload.get("status")),
            status_message=_str(payload.get("status_message")),
            unavailable=bool(payload.get("unavailable", False)),
            disabled=bool(payload.get("disabled", False)),
            success=_int(payload.get("success"), 0) or 0,
            failed=_int(payload.get("failed"), 0) or 0,
        )


@dataclass(slots=True)
class QuotaWindow:
    """A subscription rate-limit window reported by a provider quota endpoint."""

    recorded_at: datetime
    provider: str
    account: str | None
    window: str
    used_fraction: float
    remaining_fraction: float
    tier: str | None = None
    status: str | None = None
    resets_at: datetime | None = None


@dataclass(slots=True)
class ProviderLimit:
    """A limit / spend / credit row reported alongside the quota windows."""

    recorded_at: datetime
    provider: str
    account: str | None
    kind: str
    group: str | None = None
    scope_model: str | None = None
    percent: float | None = None
    severity: str | None = None
    is_active: bool | None = None
    resets_at: datetime | None = None
    used_dollars: float | None = None
    limit_dollars: float | None = None
    extra_used_credits: float | None = None
    extra_limit_credits: float | None = None
    reset_credits: int | None = None
