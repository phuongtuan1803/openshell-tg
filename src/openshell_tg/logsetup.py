"""Logging for the command line and the approval service.

One rotating file. Every line carries the process role, and anything that looks like a
secret (bot token, API key, bearer token, key=value credential) is replaced before it is
written, so a log can be pasted into a support request.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

from .store import state_dir

MAX_BYTES = 1_000_000
BACKUPS = 3

_SECRETS = (
    re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password|credential)[A-Za-z_]*\s*[=:]\s*)\S+"),
)
MASK = "[hidden]"


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(lambda hit: (hit.group(1) + MASK) if hit.re.groups else MASK, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def LOG_FILE() -> Path:
    return state_dir() / "openshell-tg.log"


def setup_logging(service: bool = False) -> None:
    """Level comes from OPENSHELL_TG_LOG_LEVEL (default INFO; DEBUG adds every openshell call)."""
    level = getattr(logging, os.environ.get("OPENSHELL_TG_LOG_LEVEL", "INFO").upper(), logging.INFO)
    formatter = RedactingFormatter(
        f"%(asctime)s %(levelname)-7s [{'serve' if service else 'cli'}] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    for old in list(root.handlers):
        root.removeHandler(old)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE(), maxBytes=MAX_BYTES, backupCount=BACKUPS)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    if service:
        # The service journal adds its own time stamp.
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(RedactingFormatter("%(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(console)
