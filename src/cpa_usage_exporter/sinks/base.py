"""The sink interface.

A sink receives normalized records and is responsible for making them
observable. The interface is deliberately tiny: four ``record_*`` calls plus
lifecycle hooks. Batches arrive once per collection cycle; a sink may buffer,
aggregate or write through as it sees fit.

Sinks must not raise for individual bad records — the run loop treats a raised
exception as a failed cycle and continues on the next tick.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AccountHealth, ProviderLimit, QuotaWindow, UsageEvent


class Sink(ABC):
    """Destination for collected usage, health and quota records."""

    name: str = "sink"

    def start(self) -> None:
        """Called once before the first collection cycle."""

    @abstractmethod
    def record_usage(self, events: list[UsageEvent]) -> None:
        """Per-request records drained from the CPA usage queue."""

    @abstractmethod
    def record_accounts(self, accounts: list[AccountHealth]) -> None:
        """A full snapshot of credential health from the management API."""

    @abstractmethod
    def record_quota(self, windows: list[QuotaWindow]) -> None:
        """Subscription rate-limit windows from provider quota endpoints."""

    @abstractmethod
    def record_limits(self, limits: list[ProviderLimit]) -> None:
        """Limit, spend and credit rows accompanying the quota windows."""

    def flush(self) -> None:
        """Called at the end of each collection cycle."""

    def close(self) -> None:
        """Release resources on shutdown."""
