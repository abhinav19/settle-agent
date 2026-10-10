"""Real readiness checks (database, Kafka, OTel collector) with per-check timeouts."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from confluent_kafka.admin import AdminClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str
    latency_ms: int = 0


ReadinessCheck = Callable[[], Awaitable[CheckResult]]


async def run_checks(
    checks: Mapping[str, ReadinessCheck], timeout_s: float = 3.0
) -> dict[str, CheckResult]:
    """Run all checks concurrently. A slow or raising check is a failed check, never an error."""

    async def one(check: ReadinessCheck) -> CheckResult:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(check(), timeout_s)
        except TimeoutError:
            return CheckResult(False, f"timed out after {timeout_s}s")
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
            return CheckResult(False, f"{type(exc).__name__}: {exc}"[:200])
        return CheckResult(result.ok, result.detail, int((time.monotonic() - started) * 1000))

    names = list(checks)
    results = await asyncio.gather(*(one(checks[n]) for n in names))
    return dict(zip(names, results, strict=True))


def database_check(sessions: async_sessionmaker[AsyncSession]) -> ReadinessCheck:
    async def check() -> CheckResult:
        async with sessions() as s:
            await s.execute(text("SELECT 1"))
        return CheckResult(True, "SELECT 1 ok")

    return check


def kafka_check(client_config: str | dict[str, Any], timeout_s: float = 2.0) -> ReadinessCheck:
    """Brokers reachable? Pass a bootstrap string, or the full (possibly SASL) client config."""
    conf: dict[str, str | int | float | bool] = (
        {"bootstrap.servers": client_config} if isinstance(client_config, str) else client_config
    )

    async def check() -> CheckResult:
        def probe() -> int:
            admin = AdminClient(conf)
            return len(admin.list_topics(timeout=timeout_s).brokers)

        brokers = await asyncio.to_thread(probe)
        return CheckResult(brokers > 0, f"{brokers} broker(s) reachable")

    return check


def tcp_check(endpoint: str, timeout_s: float = 2.0) -> ReadinessCheck:
    """Reachability of an ``http(s)://host:port`` endpoint (used for the OTLP collector)."""
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    host, port = parsed.hostname or "localhost", parsed.port or 4317

    async def check() -> CheckResult:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
        writer.close()
        await writer.wait_closed()
        return CheckResult(True, f"{host}:{port} accepting connections")

    return check
