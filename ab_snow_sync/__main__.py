"""
Entry point: python -m ab_snow_sync [--dry-run] [--log-level LEVEL]
"""

from __future__ import annotations

import argparse
import sys

from .config import load_settings
from .logging_config import configure_logging
from .sync import run_sync


def main() -> int:
    parser = argparse.ArgumentParser(description="AuditBoard → ServiceNow KB one-way sync")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log intended changes without writing to ServiceNow",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    configure_logging(args.log_level)

    import logging

    log = logging.getLogger(__name__)

    try:
        settings = load_settings()
    except Exception as exc:
        log.error("Configuration error — check your environment variables", extra={"error": str(exc)})
        return 1

    if args.dry_run:
        log.info("DRY-RUN mode enabled — no writes will be made to ServiceNow")

    result = run_sync(settings, dry_run=args.dry_run)
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
