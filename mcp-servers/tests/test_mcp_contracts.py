"""Contract tests: every server over the real MCP protocol (in-process Client)."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import MCPServer

import holiday_calendar.server as hc
import security_master.server as sm
import ssi_store.server as ss
import trade_store.server as ts
from _shared.models import Dataset

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "security-master": {"get_instrument"},
    "ssi-store": {"get_ssi", "get_ssi_for_trade"},
    "holiday-calendar": {"is_business_day", "next_business_day"},
    "trade-store": {"get_trade", "get_counterparty_instruction"},
}
BUILDERS = {
    "security-master": sm.build_server,
    "ssi-store": ss.build_server,
    "holiday-calendar": hc.build_server,
    "trade-store": ts.build_server,
}


def server(name: str, dataset: Dataset) -> MCPServer:
    return BUILDERS[name](dataset)


async def call(srv: MCPServer, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
    async with Client(srv) as client:
        res = await client.call_tool(tool, args)
    if res.is_error:
        text = res.content[0].text  # type: ignore[union-attr]
        return True, json.loads(text[text.index("{") :])
    return False, res.structured_content


@pytest.mark.parametrize("name", sorted(BUILDERS))
async def test_exposes_exactly_the_expected_read_only_tools(name: str, dataset: Dataset) -> None:
    async with Client(server(name, dataset)) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS[name]
    for t in tools:
        assert t.annotations is not None
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False
        assert t.annotations.idempotent_hint is True
        assert t.output_schema is not None, f"{t.name} must declare a typed output"
        assert t.description


async def test_no_write_style_tool_names(dataset: Dataset) -> None:
    forbidden = ("set", "update", "create", "delete", "amend", "write", "post", "put", "submit")
    for name in BUILDERS:
        async with Client(server(name, dataset)) as client:
            for t in (await client.list_tools()).tools:
                assert not t.name.startswith(forbidden), t.name


# ---- security-master ----
async def test_get_instrument_happy_and_deterministic(dataset: Dataset) -> None:
    inst = dataset.instruments[0]
    srv = server("security-master", dataset)
    err1, out1 = await call(srv, "get_instrument", {"isin": inst.isin})
    err2, out2 = await call(srv, "get_instrument", {"isin": inst.isin})
    assert not err1 and out1 == out2
    assert out1["lot_size"] == inst.lot_size and out1["status"] == "ACTIVE"


async def test_get_instrument_errors(dataset: Dataset) -> None:
    srv = server("security-master", dataset)
    assert (await call(srv, "get_instrument", {"isin": "US0000000000"}))[1]["code"] == "NOT_FOUND"
    assert (await call(srv, "get_instrument", {"isin": "bad"}))[1]["code"] == "INVALID_INPUT"


# ---- ssi-store ----
async def test_get_ssi_happy_and_errors(dataset: Dataset) -> None:
    active = next(s for s in dataset.ssis if s.status == "ACTIVE")
    srv = server("ssi-store", dataset)
    err, out = await call(srv, "get_ssi", {"bic": active.bic, "currency": active.currency})
    assert not err and out["ssi_id"] == active.ssi_id and out["status"] == "ACTIVE"
    assert (await call(srv, "get_ssi", {"bic": "AAAAUSXX", "currency": "ZZZ"}))[1][
        "code"
    ] == "NOT_FOUND"
    assert (await call(srv, "get_ssi", {"bic": "x", "currency": "USD"}))[1][
        "code"
    ] == "INVALID_INPUT"


async def test_get_ssi_for_trade(dataset: Dataset) -> None:
    clear_ssim = next(
        s for s in dataset.scenarios if s.difficulty == "clear" and s.event["reason_code"] == "SSIM"
    )
    srv = server("ssi-store", dataset)
    err, out = await call(srv, "get_ssi_for_trade", {"trade_id": clear_ssim.event["trade_id"]})
    assert not err and out["is_current"] is False
    assert out["instructed_ssi"]["status"] == "SUPERSEDED"
    assert out["current_ssi"]["status"] == "ACTIVE"
    assert (await call(srv, "get_ssi_for_trade", {"trade_id": "nope"}))[1]["code"] == "NOT_FOUND"
    assert (await call(srv, "get_ssi_for_trade", {"trade_id": "  "}))[1]["code"] == "INVALID_INPUT"


# ---- holiday-calendar ----
async def test_is_business_day(dataset: Dataset) -> None:
    cal = dataset.calendars[0]
    holiday = sorted(cal.holidays)[0]
    srv = server("holiday-calendar", dataset)
    err, out = await call(srv, "is_business_day", {"market": cal.market, "date": holiday})
    assert not err and out["is_business_day"] is False and out["reason"] == "HOLIDAY"
    assert out["holiday_name"] == cal.holidays[holiday]
    _, sat = await call(srv, "is_business_day", {"market": cal.market, "date": "2026-10-10"})
    assert sat["reason"] == "WEEKEND" and sat["is_business_day"] is False


async def test_next_business_day_skips_weekend_and_holiday(dataset: Dataset) -> None:
    cal = dataset.calendars[0]
    srv = server("holiday-calendar", dataset)
    _, out = await call(srv, "next_business_day", {"market": cal.market, "date": "2026-10-09"})
    nxt = out["next_business_day"]
    assert nxt > "2026-10-09"
    _, check = await call(srv, "is_business_day", {"market": cal.market, "date": nxt})
    assert check["is_business_day"] is True


async def test_calendar_errors(dataset: Dataset) -> None:
    srv = server("holiday-calendar", dataset)
    assert (await call(srv, "is_business_day", {"market": "ZZZZ", "date": "2026-10-09"}))[1][
        "code"
    ] == "NOT_FOUND"
    assert (await call(srv, "is_business_day", {"market": "XNYS", "date": "09/10/2026"}))[1][
        "code"
    ] == "INVALID_INPUT"
    assert (await call(srv, "next_business_day", {"market": "xnys", "date": "2026-10-09"}))[1][
        "code"
    ] == "INVALID_INPUT"


# ---- trade-store ----
async def test_get_trade_and_counterparty(dataset: Dataset) -> None:
    sc = next(
        s for s in dataset.scenarios if s.difficulty == "clear" and s.event["reason_code"] == "NMAT"
    )
    srv = server("trade-store", dataset)
    err, trade = await call(srv, "get_trade", {"trade_id": sc.event["trade_id"]})
    assert not err and trade["isin"] == sc.event["isin"]
    assert "ssi_id" not in trade  # SSI data belongs to ssi-store
    err, cp = await call(srv, "get_counterparty_instruction", {"trade_id": sc.event["trade_id"]})
    assert not err and cp["status"] == "MISMATCH" and cp["mismatch_fields"]


async def test_trade_store_errors(dataset: Dataset) -> None:
    srv = server("trade-store", dataset)
    absent = next(s for s in dataset.scenarios if s.note == "trade absent from trade store")
    assert (await call(srv, "get_trade", {"trade_id": absent.event["trade_id"]}))[1][
        "code"
    ] == "NOT_FOUND"
    assert (await call(srv, "get_counterparty_instruction", {"trade_id": "zzz"}))[1][
        "code"
    ] == "NOT_FOUND"
    assert (await call(srv, "get_trade", {"trade_id": ""}))[1]["code"] == "INVALID_INPUT"


async def test_wrong_argument_type_is_rejected(dataset: Dataset) -> None:
    srv = server("trade-store", dataset)
    async with Client(srv) as client:
        res = await client.call_tool("get_trade", {"trade_id": 123})
    assert res.is_error


async def test_real_stdio_transport_serves_tools(dataset: Dataset, tmp_path: Path) -> None:
    """Spawn an actual server subprocess to prove the entrypoint wiring works end-to-end."""
    from _shared.store import save_dataset

    save_dataset(dataset, tmp_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-I", "-m", "security_master.server"],
        env={"SETTLE_DATA_DIR": str(tmp_path), "PYTHONPATH": str(ROOT)},
        cwd=str(ROOT),
    )
    inst = dataset.instruments[0]
    async with Client(params) as client:
        res = await client.call_tool("get_instrument", {"isin": inst.isin})
    assert not res.is_error and res.structured_content["isin"] == inst.isin


async def test_streamable_http_transport_serves_tools(dataset: Dataset, tmp_path: Path) -> None:
    """Start a real HTTP server process and call it by URL, as the agent does in production."""
    import asyncio
    import socket
    import subprocess

    from _shared.store import save_dataset

    save_dataset(dataset, tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        "SETTLE_DATA_DIR": str(tmp_path),
        "SETTLE_MCP_TRANSPORT": "streamable-http",
        "SETTLE_MCP_PORT": str(port),
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.Popen(  # noqa: S603, ASYNC220
        [sys.executable, "-m", "trade_store.server"],
        env=env,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        trade = dataset.trades[0]
        for _ in range(50):  # wait for the server to accept connections
            try:
                async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                    res = await client.call_tool("get_trade", {"trade_id": trade.trade_id})
                break
            except Exception:  # noqa: BLE001 - not up yet
                await asyncio.sleep(0.2)
        else:
            raise AssertionError("HTTP server did not start")
        assert not res.is_error and res.structured_content["trade_id"] == trade.trade_id
    finally:
        proc.terminate()
        proc.wait(timeout=10)  # noqa: ASYNC222
