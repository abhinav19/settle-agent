"""The (synthetic) settlement system — the ONLY thing that changes state.

``SettlementSystem`` is the seam: production would be a real custody/settlement API; here the
synthetic implementation records each action in an append-only Postgres ledger. Every call is
idempotent on its key and reversible via the inverse captured at execution time.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.domain.investigation import FixAction
from settleagent.persistence.models import SettlementActionRow
from settleagent.remediation.models import ExecutableFix, ExecutionRecord, SettlementSystemError


class SettlementSystem(Protocol):
    async def execute(self, fix: ExecutableFix, key: str) -> ExecutionRecord: ...
    async def reverse(self, key: str) -> ExecutionRecord: ...


def _record(row: SettlementActionRow, created: bool) -> ExecutionRecord:
    return ExecutionRecord(
        idempotency_key=row.idempotency_key,
        case_id=row.case_id,
        action=FixAction(row.action),
        parameters={k: str(v) for k, v in row.parameters.items()},
        inverse={k: str(v) for k, v in row.inverse.items()},
        executed_at=row.executed_at,
        reversed_at=row.reversed_at,
        created=created,
    )


class SyntheticSettlementSystem:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    async def execute(self, fix: ExecutableFix, key: str) -> ExecutionRecord:
        """Apply the action once per ``key``. A replay returns the original record; reusing a
        key for a DIFFERENT action is a permanent error (it would silently skip a write)."""
        try:
            async with self._sessions() as s, s.begin():
                inserted = (
                    await s.execute(
                        pg_insert(SettlementActionRow)
                        .values(
                            idempotency_key=key,
                            case_id=fix.case_id,
                            action=fix.action.value,
                            parameters=fix.parameters,
                            inverse=fix.inverse,
                            executed_at=self._clock(),
                        )
                        .on_conflict_do_nothing(
                            index_elements=[SettlementActionRow.idempotency_key]
                        )
                        .returning(SettlementActionRow.id)
                    )
                ).scalar_one_or_none()
                row = (
                    await s.execute(
                        select(SettlementActionRow).where(
                            SettlementActionRow.idempotency_key == key
                        )
                    )
                ).scalar_one()
        except (OperationalError, DBAPIError, OSError) as exc:
            raise SettlementSystemError(
                f"settlement system unavailable: {exc}", transient=True
            ) from exc
        if inserted is None and (
            row.action != fix.action.value
            or row.parameters != fix.parameters
            or row.case_id != fix.case_id
        ):
            raise SettlementSystemError(
                f"idempotency key {key} was already used for a different action", transient=False
            )
        return _record(row, created=inserted is not None)

    async def reverse(self, key: str) -> ExecutionRecord:
        """Undo a previously executed action. Reversing twice is a no-op."""
        try:
            async with self._sessions() as s, s.begin():
                row = (
                    await s.execute(
                        select(SettlementActionRow)
                        .where(SettlementActionRow.idempotency_key == key)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise SettlementSystemError(
                        f"no executed action for key {key}", transient=False
                    )
                changed = row.reversed_at is None
                if changed:
                    row.reversed_at = self._clock()
                    await s.flush()
        except (OperationalError, DBAPIError, OSError) as exc:
            raise SettlementSystemError(
                f"settlement system unavailable: {exc}", transient=True
            ) from exc
        return _record(row, created=changed)
