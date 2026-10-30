"""Application diagnostics without transport URLs, cookies or login tickets."""

import logging
from logging.handlers import RotatingFileHandler


def configure_logging(data_dir):
    # Transport URLs can contain file download parameters or login tickets.
    # The adapters provide deliberately sanitized errors instead.
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.disabled = True
        logger.propagate = False
        logger.handlers = [logging.NullHandler()]
        logger.setLevel(logging.CRITICAL + 1)
    handler = RotatingFileHandler(data_dir / "assistant.log", maxBytes=2_000_000, backupCount=3)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler, logging.StreamHandler()],
        force=True,
    )
