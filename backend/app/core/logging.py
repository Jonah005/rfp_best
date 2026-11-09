"""Structured logging. Secrets and credentials are never logged."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_REDACT_KEYS = {
    "password",
    "password_hash",
    "secret",
    "secret_key",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "deepseek_api_key",
    "authorization",
}


def _redact(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(*, json_output: bool = True, level: int = logging.INFO) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        # Emit through stdlib logging rather than structlog's default
        # PrintLoggerFactory, which writes straight to stdout and bypasses
        # every stdlib handler. That bypass meant the application's own events
        # — model_call results, app_error detail, the reason behind a 502 —
        # existed only in the terminal window and never reached a log file, so
        # a failure could not be diagnosed after the fact.
        logger_factory=structlog.stdlib.LoggerFactory(),
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer()
            if json_output
            # colors=False because this same rendered string is what gets
            # written to run_local.log; ANSI escapes make the file unreadable
            # in an editor, and the console is perfectly legible without them.
            else structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
