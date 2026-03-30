"""Logging module for ConDB"""

import logging
import os

LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()

# ANSI colors
COLORS = {
    "DEBUG": "\033[36m",  # cyan
    "INFO": "\033[32m",  # green
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
    "CRITICAL": "\033[35m",  # magenta
    "RESET": "\033[0m",
}


class ColorFormatter(logging.Formatter):
    def format(self, record):
        color = COLORS.get(record.levelname, "")
        reset = COLORS["RESET"]
        # Highlight retriever logs in bright cyan
        if "retriever" in record.name:
            color = "\033[96m"  # bright cyan
        record.levelname = f"{color}{record.levelname}{reset}"
        record.msg = f"{color}{record.msg}{reset}"
        return super().format(record)


handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.root.addHandler(handler)
logging.root.setLevel(getattr(logging, LOG_LEVEL, logging.DEBUG))

# Silence noisy third-party loggers
for name in ["LiteLLM", "anthropic", "asyncio", "httpcore", "httpx", "openai", "urllib3"]:
    logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
