"""Async gateway to the read-only MCP tool servers.

Contract: ``call`` NEVER raises for tool or transport problems. It returns a ``ToolOutcome``
so the agent can degrade gracefully (record missing evidence, lower confidence) instead of
crashing the run. Permanent errors (NOT_FOUND, INVALID_INPUT) are returned immediately;
transient ones (timeouts, connection failures) are retried with bounded exponential backoff
and trip a per-server circuit breaker.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from settleagent.domain.reason_codes import ToolName
from settleagent.tools.breaker import CircuitBreaker

log = logging.getLogger(__name__)


class ErrorKind(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    TOOL_ERROR = "TOOL_ERROR"  # server answered with an error we do not recognise
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


# The server answered; these are findings about the data, not infrastructure faults.
PERMANENT: frozenset[ErrorKind] = frozenset(
    {ErrorKind.NOT_FOUND, ErrorKind.INVALID_INPUT, ErrorKind.TOOL_ERROR}
)


class ToolOutcome(BaseModel):
    """Result of one tool call (success, permanent error, or infrastructure failure)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: ToolName
    name: str
    args: dict[str, Any]
    ok: bool
    data: dict[str, Any] | None = None
    error: ErrorKind | None = None
    error_message: str | None = None
    latency_ms: int = 0
    attempts: int = 0

    @property
    def is_infrastructure_failure(self) -> bool:
        """True when the tool could not answer (evidence is missing, not 'not found')."""
        return self.error is not None and self.error not in PERMANENT


class ToolGateway(Protocol):
    async def call(self, tool: ToolName, name: str, args: dict[str, Any]) -> ToolOutcome: ...


class _ToolClient(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


ClientFactory = Callable[[], AbstractAsyncContextManager[_ToolClient]]
Sleep = Callable[[float], Awaitable[None]]


def _parse_error(text: str) -> tuple[ErrorKind, str]:
    """Extract the server's typed ``{code, message}`` payload from an error result."""
    start = text.find("{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
            return ErrorKind(payload["code"]), str(payload.get("message", ""))
        except (ValueError, KeyError, TypeError):
            pass
    return ErrorKind.TOOL_ERROR, text[:500]


class McpToolGateway:
    """Calls MCP servers through per-server client factories (connect per call).

    Connecting per call keeps the gateway stateless and robust to server restarts; the
    factories can target in-process servers (tests/evals), stdio, or streamable HTTP.
    """

    def __init__(
        self,
        endpoints: Mapping[ToolName, ClientFactory],
        *,
        timeout_s: float = 10.0,
        max_attempts: int = 3,
        backoff_s: float = 0.2,
        breaker_threshold: int = 5,
        breaker_cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._endpoints = dict(endpoints)
        self._timeout = timeout_s
        self._max_attempts = max_attempts
        self._backoff = backoff_s
        self._clock = clock
        self._sleep = sleep
        self._breakers = {
            t: CircuitBreaker(breaker_threshold, breaker_cooldown_s, clock) for t in self._endpoints
        }

    async def call(self, tool: ToolName, name: str, args: dict[str, Any]) -> ToolOutcome:
        started = self._clock()

        def done(ok: bool, attempts: int, **kw: Any) -> ToolOutcome:
            ms = int((self._clock() - started) * 1000)
            return ToolOutcome(
                tool=tool, name=name, args=args, ok=ok, latency_ms=ms, attempts=attempts, **kw
            )

        factory = self._endpoints.get(tool)
        breaker = self._breakers.get(tool)
        if factory is None or breaker is None:
            return done(False, 0, error=ErrorKind.UNAVAILABLE, error_message="no endpoint")
        if not breaker.allow():
            return done(False, 0, error=ErrorKind.CIRCUIT_OPEN, error_message="circuit open")

        last_kind = ErrorKind.UNAVAILABLE
        last_msg = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with asyncio.timeout(self._timeout):
                    async with factory() as client:
                        result = await client.call_tool(name, args)
            except TimeoutError:
                last_kind, last_msg = ErrorKind.TIMEOUT, f"timed out after {self._timeout}s"
            except Exception as exc:  # noqa: BLE001 - any transport fault is transient here
                last_kind, last_msg = ErrorKind.UNAVAILABLE, f"{type(exc).__name__}: {exc}"
            else:
                breaker.record_success()  # the server answered, even if with a data error
                if result.is_error:
                    text = "".join(getattr(c, "text", "") for c in result.content)
                    kind, msg = _parse_error(text)
                    return done(False, attempt, error=kind, error_message=msg)
                data = result.structured_content
                if data is None:
                    text = "".join(getattr(c, "text", "") for c in result.content)
                    try:
                        data = json.loads(text)
                    except ValueError:
                        return done(
                            False,
                            attempt,
                            error=ErrorKind.TOOL_ERROR,
                            error_message="non-JSON result",
                        )
                return done(True, attempt, data=dict(data))
            log.warning("tool %s.%s attempt %d failed: %s", tool, name, attempt, last_msg)
            if attempt < self._max_attempts:
                await self._sleep(self._backoff * 2 ** (attempt - 1))

        breaker.record_failure()
        return done(False, self._max_attempts, error=last_kind, error_message=last_msg)
