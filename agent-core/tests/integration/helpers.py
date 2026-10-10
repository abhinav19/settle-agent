"""Shared helpers for integration tests (Postgres + real in-process MCP tools)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.agent.graph import InvestigationAgent
from settleagent.agent.nodes import AgentConfig
from settleagent.consumer.processor import CaseProcessor
from settleagent.domain.case import Case
from settleagent.gate.policy import default_policy
from settleagent.persistence.repositories import AuditRepository, CaseRepository
from tests.doubles import fresh_event, real_gateway

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def make_processor(
    sessions: async_sessionmaker[AsyncSession],
    llm: Any,
    gateway: Any = None,
    threshold: float = 0.9,
) -> CaseProcessor:
    agent = InvestigationAgent(llm, gateway or real_gateway(), AgentConfig())
    return CaseProcessor(sessions, agent, default_policy(threshold), clock=lambda: NOW)


async def process_scenario(proc: CaseProcessor, scenario: Any) -> UUID:
    case, created = await proc.intake(fresh_event(scenario))
    assert created
    await proc.investigate(case.id)
    return case.id


async def audit_actions(sessions: async_sessionmaker[AsyncSession], case_id: UUID) -> list[str]:
    async with sessions() as s:
        return [e.action for e in await AuditRepository(s).list_for_case(case_id)]


async def load_case(sessions: async_sessionmaker[AsyncSession], case_id: UUID) -> Case:
    async with sessions() as s:
        return await CaseRepository(s).get(case_id)
