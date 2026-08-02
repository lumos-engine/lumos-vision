"""Logging setup."""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    numeric = getattr(logging, str(level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric)
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)
        _CONFIGURED = True

    for noisy in ("PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
