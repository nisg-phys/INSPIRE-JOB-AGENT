"""Structured (JSON) request logging.

Logs go to stdout - the same stream uvicorn's own access logs already use.
Locally that's your terminal; once deployed (Phase 8), every candidate host
(Render/Fly/Railway) captures stdout automatically and shows it in its own
log viewer, so this needs no separate logging service to be useful.

Every log line carries a request_id (threaded through a contextvar, not
passed explicitly), so all log lines from one request can be grep'd/filtered
together to reconstruct what happened at each step.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_STANDARD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message",
}


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Fields passed via logger.info(msg, extra={...}) ride along too.
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key == "request_id":
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RequestIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
