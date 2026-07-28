"""Provider subscription-quota pollers.

CPA stores one JSON file per credential in its auth directory. Each file carries
a ``type`` naming the provider and an ``access_token`` we can replay against the
provider's own usage endpoint to read the subscription rate-limit windows that
never appear in the usage queue.

Supported today:

* ``claude`` — ``GET https://api.anthropic.com/api/oauth/usage`` with the
  ``anthropic-beta: oauth-2025-04-20`` header. Reports ``five_hour`` and
  ``seven_day`` windows with utilization on a 0-100 scale, plus a ``limits``
  array, ``spend`` and ``extra_usage`` blocks.
* ``codex`` — ``GET https://chatgpt.com/backend-api/wham/usage`` with a
  ``ChatGPT-Account-ID`` header. Reports ``primary_window`` (5h) and
  ``secondary_window`` (7d) with ``used_percent`` on a 0-100 scale and
  ``reset_at`` as epoch seconds.

Both are undocumented endpoints the vendor CLIs use; treat their shapes as
best-effort and expect a poller to degrade to no rows rather than fail hard.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from .models import ProviderLimit, QuotaWindow, epoch_to_datetime, parse_timestamp

log = logging.getLogger(__name__)

ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA_HEADER = "oauth-2025-04-20"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# Provider-native window names mapped onto the two windows both vendors expose.
WINDOW_5H = "5h"
WINDOW_7D = "7d"


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _minor_to_major(payload: Any) -> float | None:
    """Anthropic reports money as an integer count of minor units."""
    if not isinstance(payload, dict):
        return None
    amount = _float(payload.get("amount_minor"))
    return None if amount is None else amount / 100.0


class QuotaPoller:
    """Reads credentials off disk and turns them into quota/limit records."""

    def __init__(
        self,
        auth_dir: str,
        *,
        providers: list[str] | None = None,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ) -> None:
        self.auth_dir = Path(auth_dir).expanduser()
        self.timeout = timeout
        self.session = session or requests.Session()
        available = dict(PROVIDER_FETCHERS)
        if providers:
            wanted = {p.strip().lower() for p in providers}
            unknown = wanted - available.keys()
            if unknown:
                log.warning("ignoring unknown quota providers: %s", ", ".join(sorted(unknown)))
            available = {k: v for k, v in available.items() if k in wanted}
        self.fetchers = available

    def poll(self) -> tuple[list[QuotaWindow], list[ProviderLimit]]:
        recorded_at = datetime.now(timezone.utc)
        windows: list[QuotaWindow] = []
        limits: list[ProviderLimit] = []
        for path, credentials in self._load_credentials():
            provider = str(credentials.get("type") or "").strip().lower()
            fetch = self.fetchers.get(provider)
            token = credentials.get("access_token")
            if fetch is None or not token:
                continue
            account = credentials.get("email") or credentials.get("account_id")
            try:
                payload = fetch(
                    self.session,
                    str(token),
                    str(credentials.get("account_id") or ""),
                    self.timeout,
                )
            except Exception as exc:  # noqa: BLE001 - one bad credential must not stop the rest
                log.warning("quota poll failed for %s (%s): %s", path.name, provider, exc)
                continue
            raw_windows, raw_limits = payload
            for window in raw_windows:
                windows.append(
                    QuotaWindow(
                        recorded_at=recorded_at,
                        provider=provider,
                        account=str(account) if account else None,
                        window=window["window"],
                        used_fraction=window["used_fraction"],
                        remaining_fraction=window["remaining_fraction"],
                        tier=window.get("tier"),
                        status=window.get("status"),
                        resets_at=window.get("resets_at"),
                    )
                )
            for limit in raw_limits:
                limits.append(
                    ProviderLimit(
                        recorded_at=recorded_at,
                        provider=provider,
                        account=str(account) if account else None,
                        kind=limit.get("kind") or "unknown",
                        group=limit.get("group"),
                        scope_model=limit.get("scope_model"),
                        percent=limit.get("percent"),
                        severity=limit.get("severity"),
                        is_active=limit.get("is_active"),
                        resets_at=limit.get("resets_at"),
                        used_dollars=limit.get("used_dollars"),
                        limit_dollars=limit.get("limit_dollars"),
                        extra_used_credits=limit.get("extra_used_credits"),
                        extra_limit_credits=limit.get("extra_limit_credits"),
                        reset_credits=limit.get("reset_credits"),
                    )
                )
        return windows, limits

    def _load_credentials(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self.auth_dir.is_dir():
            log.warning("quota auth dir %s is not a directory", self.auth_dir)
            return []
        found: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.auth_dir.glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("cannot read credential %s: %s", path.name, exc)
                continue
            if isinstance(document, dict):
                found.append((path, document))
        return found

    def close(self) -> None:
        self.session.close()


def _get_json(
    session: requests.Session, url: str, headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    response = session.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _anthropic_window_status(limits: Any, window: str) -> str | None:
    """Borrow a matching limit's severity as the window status, when one names it."""
    hints = {
        WINDOW_5H: ("5h", "five_hour", "five-hour", "fivehour"),
        WINDOW_7D: ("7d", "seven_day", "seven-day", "sevenday"),
    }.get(window, ())
    if not isinstance(limits, list):
        return None
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        kind = str(limit.get("kind") or "").lower()
        if any(hint in kind for hint in hints):
            severity = limit.get("severity")
            if severity is not None:
                return str(severity)
    return None


def _anthropic_limits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for limit in payload.get("limits") or []:
        if not isinstance(limit, dict):
            continue
        scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        rows.append(
            {
                "kind": limit.get("kind"),
                "group": limit.get("group"),
                "scope_model": model.get("display_name"),
                "percent": _float(limit.get("percent")),
                "severity": limit.get("severity"),
                "is_active": None if limit.get("is_active") is None else bool(limit["is_active"]),
                "resets_at": parse_timestamp(limit.get("resets_at")),
            }
        )
    spend = payload.get("spend")
    if isinstance(spend, dict) and spend:
        rows.append(
            {
                "kind": "spend",
                "severity": spend.get("severity"),
                "used_dollars": _minor_to_major(spend.get("used")),
                "limit_dollars": _minor_to_major(spend.get("limit")),
            }
        )
    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra:
        rows.append(
            {
                "kind": "extra_usage",
                "extra_used_credits": _float(extra.get("used_credits")),
                "extra_limit_credits": _float(extra.get("monthly_limit")),
            }
        )
    return rows


def fetch_anthropic_quota(
    session: requests.Session, access_token: str, account_id: str, timeout: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = _get_json(
        session,
        ANTHROPIC_USAGE_URL,
        {
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": ANTHROPIC_BETA_HEADER,
            "Accept": "application/json",
        },
        timeout,
    )
    windows: list[dict[str, Any]] = []
    for window_id, key in ((WINDOW_5H, "five_hour"), (WINDOW_7D, "seven_day")):
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        utilization = _float(block.get("utilization"))
        if utilization is None:
            continue
        used = utilization / 100.0
        windows.append(
            {
                "window": window_id,
                "tier": None,
                "used_fraction": used,
                "remaining_fraction": max(0.0, 1.0 - used),
                "resets_at": parse_timestamp(block.get("resets_at")),
                "status": _anthropic_window_status(payload.get("limits"), window_id),
            }
        )
    return windows, _anthropic_limits(payload)


def _codex_windows(rate_limit: Any, tier: str | None) -> list[dict[str, Any]]:
    if not isinstance(rate_limit, dict):
        return []
    windows: list[dict[str, Any]] = []
    for window_id, key in ((WINDOW_5H, "primary_window"), (WINDOW_7D, "secondary_window")):
        block = rate_limit.get(key)
        if not isinstance(block, dict):
            continue
        used_percent = _float(block.get("used_percent"))
        if used_percent is None:
            continue
        used = used_percent / 100.0
        windows.append(
            {
                "window": window_id,
                "tier": tier,
                "used_fraction": used,
                "remaining_fraction": max(0.0, 1.0 - used),
                "resets_at": epoch_to_datetime(block.get("reset_at")),
                "status": None,
            }
        )
    return windows


def fetch_codex_quota(
    session: requests.Session, access_token: str, account_id: str, timeout: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = _get_json(
        session,
        CODEX_USAGE_URL,
        {
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-ID": account_id,
            "Accept": "application/json",
        },
        timeout,
    )
    plan = payload.get("plan_type")
    windows = _codex_windows(payload.get("rate_limit"), str(plan) if plan else None)
    # Metered add-ons (for example Spark) carry their own nested rate-limit block.
    for extra in payload.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        feature = str(extra.get("metered_feature") or "").strip()
        windows.extend(_codex_windows(extra.get("rate_limit"), feature or None))
    limits: list[dict[str, Any]] = []
    credits = payload.get("rate_limit_reset_credits")
    if isinstance(credits, dict) and credits.get("available_count") is not None:
        limits.append({"kind": "reset_credits", "reset_credits": _int(credits.get("available_count"))})
    return windows, limits


QuotaFetcher = Callable[
    [requests.Session, str, str, float], tuple[list[dict[str, Any]], list[dict[str, Any]]]
]

PROVIDER_FETCHERS: dict[str, QuotaFetcher] = {
    "claude": fetch_anthropic_quota,
    "anthropic": fetch_anthropic_quota,
    "codex": fetch_codex_quota,
}
