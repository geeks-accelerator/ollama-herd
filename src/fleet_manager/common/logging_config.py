"""Structured JSONL logging configuration for Ollama Herd.

Outputs structured JSON log lines to ~/.fleet-manager/logs/ with daily rotation,
while keeping the pretty console output via Rich.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class JSONLFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        # Include extra fields if set (e.g., request_id, node_id)
        for key in ("request_id", "node_id", "model", "queue_key"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        return json.dumps(entry, default=str)


def setup_logging(
    data_dir: str = "~/.fleet-manager",
    log_level: str | None = None,
    console_level: str | None = None,
):
    """Configure root logger with JSONL file handler + console handler.

    Args:
        data_dir: Base directory for logs (logs/ subdirectory is created).
        log_level: Level for JSONL file output (default: DEBUG).
        console_level: Level for console output (default: INFO).
    """
    log_dir = Path(data_dir).expanduser() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    file_lvl_name = (log_level or os.getenv("FLEET_LOG_LEVEL", "DEBUG")).upper()
    con_lvl_name = (console_level or os.getenv("FLEET_CONSOLE_LOG_LEVEL", "INFO")).upper()
    file_level = getattr(logging, file_lvl_name)
    con_level = getattr(logging, con_lvl_name)

    root = logging.getLogger()
    # Don't add duplicate handlers if called more than once
    if any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        return

    root.setLevel(min(file_level, con_level))

    # --- JSONL file handler (daily rotation, keep 30 days) ---
    log_file = log_dir / "herd.jsonl"
    file_handler = TimedRotatingFileHandler(
        str(log_file),
        when="midnight",
        interval=1,
        backupCount=30,
        utc=True,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(file_level)
    file_handler.setFormatter(JSONLFormatter())
    root.addHandler(file_handler)

    # Console handler is typically set up by Rich/uvicorn already,
    # so we just ensure the level is correct
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, TimedRotatingFileHandler
        ):
            handler.setLevel(con_level)

    # Silence noisy third-party loggers that flood the JSONL file.
    # httpcore + aiosqlite account for ~83% of all log lines at DEBUG level.
    for noisy_logger in ("httpcore", "aiosqlite", "httpx", "hpack"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    logging.getLogger(__name__).debug(
        f"JSONL logging configured: {log_file} (level={logging.getLevelName(file_level)}, "
        f"rotation=daily, retention=30d)"
    )
