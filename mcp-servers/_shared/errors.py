"""Typed tool errors. Every server reports failures as a JSON ``{code, message}`` payload."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum

from mcp.server.mcpserver.exceptions import ToolError


class ErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"


class ToolFailure(Exception):
    """Raised by service code; converted to an MCP tool error at the server edge."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_json(self) -> str:
        return json.dumps({"code": self.code.value, "message": self.message})


@contextmanager
def tool_errors() -> Iterator[None]:
    """Convert ``ToolFailure`` into the SDK's ``ToolError`` carrying the typed JSON payload."""
    try:
        yield
    except ToolFailure as exc:
        raise ToolError(exc.as_json()) from exc
