"""Central logging configuration."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_file: Path) -> None:
    """Configure file logging for the application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler(
                log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        ],
        force=True,
    )
