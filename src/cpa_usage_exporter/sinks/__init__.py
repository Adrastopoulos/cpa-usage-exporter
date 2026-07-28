"""Sink implementations and the factory that config selects between."""

from __future__ import annotations

from ..config import Config
from ..pricing import PriceBook
from .base import Sink

__all__ = ["Sink", "build_sink"]


def build_sink(config: Config, price_book: PriceBook | None) -> Sink:
    """Instantiate the sink named by ``config.sink``."""
    if config.sink == "prometheus":
        from .prometheus import PrometheusSink

        return PrometheusSink(
            config.prometheus, price_book=price_book, currency=config.pricing.currency
        )
    if config.sink == "bigquery":
        from .bigquery import BigQuerySink

        return BigQuerySink(config.bigquery, price_book=price_book)
    raise ValueError(f"unknown sink: {config.sink!r}")
