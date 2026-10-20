"""Logging configuration for ledgerline-api.

Sets up standard-library logging with a consistent line format for the
service and its request handlers.
"""

import logging
import os
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(level: str | None = None) -> logging.Logger:
    """Configure root logging and return the service logger."""
    resolved_level = (level or os.getenv("LEDGERLINE_LOG_LEVEL", "INFO")).upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT))

    logging.basicConfig(
        level=getattr(logging, resolved_level, logging.INFO),
        handlers=[handler],
        force=True,
    )

    # Keep noisy third-party loggers at a sensible level.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return logging.getLogger("ledgerline")


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ledgerline namespace."""
    return logging.getLogger(f"ledgerline.{name}")