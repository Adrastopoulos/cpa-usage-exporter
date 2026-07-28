"""The collection loop.

Every ``interval_seconds`` the exporter drains the usage queue and refreshes
account health; provider quota is polled on its own slower cadence because those
endpoints are rate-limited and their values move slowly.

Each collector is isolated: a failure is logged, counted and does not abort the
cycle or the process. Draining faster than CPA's queue retention window (60s by
default) is what keeps records from being dropped before they are read.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from types import FrameType

from .config import Config
from .cpa_client import CPAClient
from .pricing import PriceBook
from .providers import QuotaPoller
from .sinks import Sink, build_sink

log = logging.getLogger(__name__)


class Exporter:
    def __init__(
        self,
        config: Config,
        *,
        client: CPAClient | None = None,
        sink: Sink | None = None,
        quota_poller: QuotaPoller | None = None,
    ) -> None:
        self.config = config
        self._stop = threading.Event()

        price_book: PriceBook | None = None
        if config.pricing.enabled:
            price_book = PriceBook.load(
                config.pricing.path, fallback_to_bundled=config.pricing.fallback_to_bundled
            )
            log.info("loaded %d model price entries", len(price_book))

        self.client = client or CPAClient(
            config.cpa.management_url,
            config.cpa.management_key,
            timeout=config.cpa.timeout_seconds,
            verify_tls=config.cpa.verify_tls,
            queue_batch=config.cpa.queue_batch,
            queue_max_batches=config.cpa.queue_max_batches,
        )
        self.sink = sink or build_sink(config, price_book)
        self.quota_poller = quota_poller
        if self.quota_poller is None and config.quota.enabled:
            self.quota_poller = QuotaPoller(
                config.quota.auth_dir,
                providers=config.quota.providers,
                timeout=config.quota.timeout_seconds,
            )
        self._last_quota_poll: float | None = None

    def _observe(self, collector: str, duration: float, ok: bool) -> None:
        observer = getattr(self.sink, "observe_collection", None)
        if callable(observer):
            observer(collector, duration, ok)

    def _run_collector(self, name: str, work: object) -> None:
        started = time.monotonic()
        ok = True
        try:
            work()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - collectors must never kill the loop
            ok = False
            log.warning("collector %s failed: %s", name, exc)
            log.debug("collector %s traceback", name, exc_info=True)
        self._observe(name, time.monotonic() - started, ok)

    def _collect_usage(self) -> None:
        events = self.client.drain_usage_queue()
        if events:
            log.debug("drained %d usage event(s)", len(events))
        self.sink.record_usage(events)

    def _collect_accounts(self) -> None:
        self.sink.record_accounts(self.client.fetch_auth_files())

    def _collect_quota(self) -> None:
        assert self.quota_poller is not None
        windows, limits = self.quota_poller.poll()
        self.sink.record_quota(windows)
        self.sink.record_limits(limits)

    def collect_once(self) -> None:
        """Run one cycle, honouring the quota poller's slower cadence."""
        if self.config.cpa.collect_usage_queue:
            self._run_collector("usage_queue", self._collect_usage)
        if self.config.cpa.collect_auth_files:
            self._run_collector("auth_files", self._collect_accounts)
        if self.quota_poller is not None:
            now = time.monotonic()
            due = (
                self._last_quota_poll is None
                or (now - self._last_quota_poll) >= self.config.quota.interval_seconds
            )
            if due:
                self._run_collector("provider_quota", self._collect_quota)
                self._last_quota_poll = now
        self.sink.flush()

    def run(self) -> None:
        self.sink.start()
        log.info(
            "exporter started: sink=%s interval=%ds quota=%s",
            self.sink.name,
            self.config.interval_seconds,
            "on" if self.quota_poller is not None else "off",
        )
        try:
            while not self._stop.is_set():
                self.collect_once()
                self._stop.wait(self.config.interval_seconds)
        finally:
            self.close()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        for closeable in (self.quota_poller, self.client, self.sink):
            if closeable is None:
                continue
            try:
                closeable.close()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                log.debug("error closing %r", closeable, exc_info=True)


def install_signal_handlers(exporter: Exporter) -> None:
    def handle(signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %d, shutting down", signum)
        exporter.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle)
