"""Client for the CLIProxyAPI management API.

Two collectors live here:

* :meth:`CPAClient.drain_usage_queue` — pops per-request records from the usage
  queue. The queue is an in-memory FIFO with a short sliding retention window
  (``redis-usage-queue-retention-seconds``, 60s by default), so records are lost
  if you poll slower than they expire. Draining is destructive: a record is
  removed from the queue as it is read, and only one consumer can get it.
* :meth:`CPAClient.fetch_auth_files` — a point-in-time snapshot of credential
  health from ``/v0/management/auth-files``.

Publishing into the usage queue requires ``usage-statistics-enabled: true`` in
the CPA config; the queue is otherwise present but always empty.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from .models import AccountHealth, UsageEvent

log = logging.getLogger(__name__)


class CPAClientError(RuntimeError):
    """A management API call failed."""


class CPAClient:
    def __init__(
        self,
        management_url: str,
        management_key: str,
        *,
        timeout: float = 30.0,
        verify_tls: bool = True,
        queue_batch: int = 1000,
        queue_max_batches: int = 20,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = management_url.rstrip("/")
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.queue_batch = queue_batch
        self.queue_max_batches = queue_max_batches
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {management_key}",
                "Accept": "application/json",
                "User-Agent": "cpa-usage-exporter",
            }
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(
                url, params=params, timeout=self.timeout, verify=self.verify_tls
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise CPAClientError(f"GET {url} failed: {exc}") from exc
        except ValueError as exc:
            raise CPAClientError(f"GET {url} returned invalid JSON: {exc}") from exc

    def drain_usage_queue(self) -> list[UsageEvent]:
        """Pop usage records until the queue is drained or the batch cap is hit.

        The endpoint answers with a bare JSON array and removes what it returns,
        so we keep requesting until a short read tells us the queue is empty.
        """
        events: list[UsageEvent] = []
        for _ in range(max(1, self.queue_max_batches)):
            payload = self._get("/usage-queue", {"count": self.queue_batch})
            records = _as_record_list(payload)
            for record in records:
                if not isinstance(record, dict):
                    continue
                try:
                    events.append(UsageEvent.from_payload(record))
                except Exception:  # noqa: BLE001 - one bad record must not stall the drain
                    log.warning("skipping malformed usage record", exc_info=True)
            if len(records) < self.queue_batch:
                break
        return events

    def fetch_auth_files(self) -> list[AccountHealth]:
        payload = self._get("/auth-files")
        entries = payload.get("files") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return []
        recorded_at = datetime.now(timezone.utc)
        accounts: list[AccountHealth] = []
        for entry in entries:
            if isinstance(entry, dict):
                accounts.append(AccountHealth.from_payload(entry, recorded_at))
        return accounts

    def close(self) -> None:
        self.session.close()


def _as_record_list(payload: Any) -> list[Any]:
    """Accept the documented bare array, tolerating a wrapped object."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "records", "usage", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []
