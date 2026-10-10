"""Structured JSON logging. Every record carries ``case_id`` (when bound) and the active trace."""

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from opentelemetry import trace

_case_id: ContextVar[str | None] = ContextVar("settleagent_case_id", default=None)

_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}
_OWN = {"case_id", "trace_id", "span_id"}


@contextmanager
def bind_case(case_id: UUID | str) -> Iterator[None]:
    """Attach a case id to every log record emitted (in this task) inside the block."""
    token = _case_id.set(str(case_id))
    try:
        yield
    finally:
        _case_id.reset(token)


def current_case_id() -> str | None:
    return _case_id.get()


class ContextFilter(logging.Filter):
    """Injects case_id and trace/span ids into each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.case_id = _case_id.get()
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None
        record.span_id = format(ctx.span_id, "016x") if ctx.is_valid else None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "case_id": getattr(record, "case_id", None),
            "trace_id": getattr(record, "trace_id", None),
            "span_id": getattr(record, "span_id", None),
        }
        for key, value in record.__dict__.items():  # logger.info(..., extra={...})
            if key not in _STANDARD and key not in _OWN and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, json_logs: bool = True, stream: Any = None) -> None:
    """Idempotently route ALL logging (incl. uvicorn) through one handler."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        JsonFormatter()
        if json_logs
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(case_id)s] %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
