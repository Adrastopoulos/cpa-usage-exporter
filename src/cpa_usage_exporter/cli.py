"""Command line entrypoint."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import Config, ConfigError, load_config
from .exporter import Exporter, install_signal_handlers

log = logging.getLogger("cpa_usage_exporter")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cpa-usage-exporter",
        description=(
            "Drain the CLIProxyAPI usage queue and poll provider quota, "
            "exporting to Prometheus or BigQuery."
        ),
        epilog=(
            "Every setting can also be supplied as an environment variable prefixed "
            "with CPA_ (for example CPA_MANAGEMENT_KEY, CPA_SINK, CPA_PROMETHEUS_PORT). "
            "Environment variables win over the config file."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help="path to a YAML config file (also settable with CPA_CONFIG_FILE)",
    )
    parser.add_argument(
        "--sink",
        choices=("prometheus", "bigquery"),
        help="override the configured sink",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        help="override the log level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single collection cycle and exit, instead of looping",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration, print a summary and exit",
    )
    parser.add_argument("--version", action="version", version=f"cpa-usage-exporter {__version__}")
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _summary(config: Config) -> str:
    lines = [
        f"sink:             {config.sink}",
        f"interval:         {config.interval_seconds}s",
        f"management url:   {config.cpa.management_url}",
        f"usage queue:      {'on' if config.cpa.collect_usage_queue else 'off'}",
        f"auth files:       {'on' if config.cpa.collect_auth_files else 'off'}",
    ]
    if config.quota.enabled:
        lines.append(
            f"quota polling:    every {config.quota.interval_seconds}s from {config.quota.auth_dir} "
            f"({', '.join(config.quota.providers)})"
        )
    else:
        lines.append("quota polling:    off")
    if config.sink == "prometheus":
        lines.append(f"listen:           http://{config.prometheus.host}:{config.prometheus.port}/metrics")
    else:
        lines.append(
            f"bigquery target:  {config.bigquery.project}.{config.bigquery.dataset} "
            f"(tables {config.bigquery.table_prefix}*)"
        )
    lines.append(
        "pricing:          "
        + (f"on ({config.pricing.path or 'bundled map'})" if config.pricing.enabled else "off")
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.sink:
            config.sink = args.sink
        if args.log_level:
            config.log_level = args.log_level.upper()
        _configure_logging(config.log_level)
        config.validate()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        print(_summary(config))
        return 0

    exporter = Exporter(config)
    if args.once:
        try:
            exporter.sink.start()
            exporter.collect_once()
        finally:
            exporter.close()
        return 0

    install_signal_handlers(exporter)
    exporter.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
