"""Logging setup per SPEC rule 8: rotating file + console at INFO."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("phathom")
    logger.setLevel(logging.INFO)
    if not _configured:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh = RotatingFileHandler(
            log_dir / "phathom.log", maxBytes=2 * 1024 * 1024, backupCount=5
        )
        fh.setFormatter(fmt)
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
        _configured = True
    return logger
