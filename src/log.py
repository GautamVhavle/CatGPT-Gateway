"""
Logging setup — file + optional console handlers.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from src.config import Config

# Global flag: when True, suppress console log handlers (for TUI mode)
_suppress_console = False


def suppress_console_logs() -> None:
    """Disable all console log handlers (call before any setup_logging)."""
    global _suppress_console
    _suppress_console = True
    # Also silence already-created loggers' console handlers
    for name in list(logging.Logger.manager.loggerDict):
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            if isinstance(handler, logging.StreamHandler) and handler.stream in (sys.stdout, sys.stderr):
                logger.removeHandler(handler)


def setup_logging(name: str = "chatgpt_scraper", log_file: str | None = None) -> logging.Logger:
    """
    Configure and return a logger that writes to file (and optionally console).

    Args:
        name: Logger name.
        log_file: Optional filename override. Defaults to '{name}_{date}.log'.
    """
    Config.ensure_dirs()

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, Config.LOG_LEVEL.upper(), logging.DEBUG))

    # Prevent duplicate handlers on repeated calls
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── File handler ────────────────────────────────────────────
    if log_file is None:
        date_str = datetime.now().strftime("%Y%m%d")
        log_file = f"{name}_{date_str}.log"

    fh = logging.FileHandler(Config.LOG_DIR / log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)  # Always capture everything in file
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # ── Console handler (disabled in TUI mode) ──────────────────
    if Config.VERBOSE and not _suppress_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    return logger
