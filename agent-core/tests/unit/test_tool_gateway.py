import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import security_master.server as sm_server
import trade_store.server as ts_server
from _shared.generators import generate_dataset
from mcp import Client

from settleagent.domain.reason_codes import ToolName
from settleagent.tools.breaker import BreakerState, CircuitBreaker
from settleagent.tools.gateway import ErrorKind, McpToolGateway

DATASET = generate_dataset(42, 300)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def no_sleep(_: float) -> None:
    return None


def inproc_gateway(**kw: Any) -> McpToolGateway:
    return McpToolGateway(
        {
            ToolName.SECURITY_MASTER: lambda: Client(sm_server.build_server(DATASET)),
            ToolName.TRADE_STORE: lambda: Client(ts_server.build_server(DATASET)),
        },
        sleep=no_sleep,
        **kw,
    )


# ---- circuit breaker ----
def state_of(b: CircuitBreaker) -> BreakerState:
    return b.state


def test_breaker_opens_then_half_opens_then_closes() -> None:
    clock = Clock()
    b = CircuitBreaker(threshold=2, cooldown_s=10, clock=clock)
    assert b.allow() and state_of(b) is BreakerState.CLOSED
    b.record_failure()
    assert b.allow()
    b.record_failure()
    assert state_of(b) is BreakerState.OPEN and not b.allow()
    clock.t = 10
    assert state_of(b) is BreakerState.HALF_OPEN
    assert b.allow()  # single probe
    assert not b.allow()
    b.record_success()
    assert state_of(b) is BreakerState.CLOSED and b.allow()


def test_breaker_failed_probe_reopens() -> None:
    clock = Clock()
    b = CircuitBreaker(threshold=1, cooldown_s=5, clock=clock)
    b.record_failure()
    clock.t = 5
    assert b.allow()
    b.record_failure()
    assert state_of(b) is BreakerState.OPEN
    clock.t = 9
    assert state_of(b) is BreakerState.OPEN  # cooldown restarted at t=5


def test_breaker_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(0, 1, Clock())


# ---- gateway over real in-process MCP servers ----
async def test_success_returns_data() -> None:
    inst = DATASET.instruments[0]
    out = await inproc_gateway().call(
        ToolName.SECURITY_MASTER, "get_instrument", {"isin": inst.isin}
    )
    assert out.ok and out.data is not None and out.data["isin"] == inst.isin
    assert out.attempts == 1 and out.error is None


async def test_not_found_is_permanent_and_not_retried() -> None:
    out = await inproc_gateway().call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "nope"})
    assert not out.ok and out.error is ErrorKind.NOT_FOUND
    assert out.attempts == 1 and not out.is_infrastructure_failure


async def test_invalid_input_is_typed() -> None:
    out = await inproc_gateway().call(ToolName.SECURITY_MASTER, "get_instrument", {"isin": "x"})
    assert out.error is ErrorKind.INVALID_INPUT


async def test_unknown_tool_name_is_a_tool_error_not_an_exception() -> None:
    out = await inproc_gateway().call(ToolName.SECURITY_MASTER, "delete_everything", {})
    assert not out.ok and out.error in {ErrorKind.TOOL_ERROR, ErrorKind.INVALID_INPUT}


async def test_unconfigured_server_is_unavailable() -> None:
    out = await inproc_gateway().call(ToolName.SSI_STORE, "get_ssi", {})
    assert out.error is ErrorKind.UNAVAILABLE and out.attempts == 0


# ---- transient failures ----
class FlakyClient:
    def __init__(self, fail_times: int) -> None:
        self.remaining = fail_times
        self.calls = 0

    def factory(self) -> Any:
        @asynccontextmanager
        async def cm() -> Any:
            self.calls += 1
            if self.remaining > 0:
                self.remaining -= 1
                raise ConnectionError("boom")
            yield SimpleNamespace(call_tool=self._call)

        return cm()

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        return SimpleNamespace(is_error=False, structured_content={"ok": 1}, content=[])


async def test_transient_failure_is_retried_with_exponential_backoff() -> None:
    flaky = FlakyClient(fail_times=2)
    sleeps: list[float] = []

    async def record(s: float) -> None:
        sleeps.append(s)

    gw = McpToolGateway(
        {ToolName.TRADE_STORE: flaky.factory}, max_attempts=3, backoff_s=0.5, sleep=record
    )
    out = await gw.call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "x"})
    assert out.ok and out.attempts == 3 and flaky.calls == 3
    assert sleeps == [0.5, 1.0]


async def test_exhausted_retries_degrade_to_unavailable() -> None:
    flaky = FlakyClient(fail_times=99)
    gw = McpToolGateway({ToolName.TRADE_STORE: flaky.factory}, max_attempts=3, sleep=no_sleep)
    out = await gw.call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "x"})
    assert not out.ok and out.error is ErrorKind.UNAVAILABLE and out.attempts == 3
    assert out.is_infrastructure_failure and "ConnectionError" in (out.error_message or "")


async def test_timeout_is_reported() -> None:
    @asynccontextmanager
    async def hang() -> Any:
        await asyncio.sleep(5)
        yield SimpleNamespace()

    gw = McpToolGateway(
        {ToolName.TRADE_STORE: hang}, timeout_s=0.02, max_attempts=2, sleep=no_sleep
    )
    out = await gw.call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "x"})
    assert out.error is ErrorKind.TIMEOUT and out.attempts == 2


async def test_circuit_opens_and_short_circuits_then_recovers() -> None:
    clock = Clock()
    flaky = FlakyClient(fail_times=99)
    gw = McpToolGateway(
        {ToolName.TRADE_STORE: flaky.factory},
        max_attempts=1,
        breaker_threshold=2,
        breaker_cooldown_s=30,
        clock=clock,
        sleep=no_sleep,
    )
    for _ in range(2):
        await gw.call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "x"})
    calls_before = flaky.calls
    out = await gw.call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "x"})
    assert out.error is ErrorKind.CIRCUIT_OPEN and out.attempts == 0
    assert flaky.calls == calls_before  # no connection attempted while open
    clock.t = 31
    flaky.remaining = 0  # server recovered
    healed = await gw.call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "x"})
    assert healed.ok


async def test_data_errors_do_not_trip_the_breaker() -> None:
    gw = inproc_gateway(breaker_threshold=1)
    for _ in range(3):
        out = await gw.call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "nope"})
        assert out.error is ErrorKind.NOT_FOUND


def test_invalid_attempts_rejected() -> None:
    with pytest.raises(ValueError):
        McpToolGateway({}, max_attempts=0)


async def test_non_json_tool_result_is_a_tool_error() -> None:
    @asynccontextmanager
    async def odd() -> Any:
        async def call(name: str, args: dict[str, Any]) -> Any:
            return SimpleNamespace(
                is_error=False, structured_content=None, content=[SimpleNamespace(text="<html>")]
            )

        yield SimpleNamespace(call_tool=call)

    gw = McpToolGateway({ToolName.TRADE_STORE: odd}, sleep=no_sleep)
    out = await gw.call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "x"})
    assert out.error is ErrorKind.TOOL_ERROR and "non-JSON" in (out.error_message or "")


async def test_text_json_result_without_structured_content_is_parsed() -> None:
    @asynccontextmanager
    async def text_only() -> Any:
        async def call(name: str, args: dict[str, Any]) -> Any:
            return SimpleNamespace(
                is_error=False, structured_content=None, content=[SimpleNamespace(text='{"v": 3}')]
            )

        yield SimpleNamespace(call_tool=call)

    gw = McpToolGateway({ToolName.TRADE_STORE: text_only}, sleep=no_sleep)
    out = await gw.call(ToolName.TRADE_STORE, "get_trade", {"trade_id": "x"})
    assert out.ok and out.data == {"v": 3}
