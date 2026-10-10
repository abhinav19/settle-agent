"""Derive an executable write fix from a result's OWN evidence — never trust model parameters.

The model's ``proposed_fix.parameters`` are untrusted hints. A write is only executable if:
- it targets the case's own trade, and
- every value that will be applied (new SSI, quantity, date) is supported by tool evidence,
  and any value the model supplied agrees with what the evidence supports.
Anything else raises ``FixNotExecutableError`` and the case goes to a human instead.
"""

from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.investigation import (
    ACTION_REQUIRES_WRITE,
    FixAction,
    InvestigationResult,
)
from settleagent.domain.reason_codes import ReasonCategory, ToolName
from settleagent.remediation.models import ExecutableFix, FixNotExecutableError

_Built = tuple[dict[str, str], dict[str, str]]


def _find(result: InvestigationResult, tool: ToolName, key: str) -> Any:
    for e in result.evidence:
        if e.source is tool and key in e.raw:
            return e.raw[key]
    return None


def _decimal(value: Any, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FixNotExecutableError(f"{what} is not a number: {value!r}") from exc


def _agree(given: Any, derived: str, what: str) -> None:
    if given is not None and str(given) != derived:
        raise FixNotExecutableError(
            f"model proposed {what}={given!r} but the evidence supports {derived!r}"
        )


def _amend_ssi(
    event: SettlementFailureEvent, result: InvestigationResult, p: dict[str, Any]
) -> _Built:
    current = _find(result, ToolName.SSI_STORE, "current_ssi")
    instructed = _find(result, ToolName.SSI_STORE, "instructed_ssi")
    if not isinstance(current, dict) or not isinstance(instructed, dict):
        raise FixNotExecutableError("no SSI evidence (instructed vs current) to amend from")
    if _find(result, ToolName.SSI_STORE, "is_current") is not False:
        raise FixNotExecutableError("evidence does not show the trade's SSI is stale")
    new_id, old_id = current.get("ssi_id"), instructed.get("ssi_id")
    if not new_id or not old_id or new_id == old_id:
        raise FixNotExecutableError("SSI evidence does not identify a different current SSI")
    _agree(p.get("new_ssi_id"), str(new_id), "new_ssi_id")
    return (
        {"trade_id": event.trade_id, "new_ssi_id": str(new_id)},
        {"op": "restore_ssi", "trade_id": event.trade_id, "ssi_id": str(old_id)},
    )


def _resubmit(
    event: SettlementFailureEvent, result: InvestigationResult, p: dict[str, Any]
) -> _Built:
    quantity = event.quantity
    if result.reason_category is ReasonCategory.QUANTITY:
        lot = _find(result, ToolName.SECURITY_MASTER, "lot_size")
        if not isinstance(lot, int) or isinstance(lot, bool) or lot < 1:
            raise FixNotExecutableError("no lot-size evidence to correct the quantity")
        quantity = (event.quantity // lot) * lot
        if quantity <= 0:
            raise FixNotExecutableError("quantity rounds down to zero lots")
    _agree(p.get("quantity"), str(quantity), "quantity")
    return (
        {"trade_id": event.trade_id, "quantity": str(quantity)},
        {"op": "cancel_resubmission", "trade_id": event.trade_id},
    )


def _partial_settle(
    event: SettlementFailureEvent, result: InvestigationResult, p: dict[str, Any]
) -> _Built:
    raw = _find(result, ToolName.TRADE_STORE, "securities_available")
    if raw is None:
        raise FixNotExecutableError("no securities-availability evidence")
    available = _decimal(raw, "securities_available")
    lot = _find(result, ToolName.SECURITY_MASTER, "lot_size")
    if isinstance(lot, int) and not isinstance(lot, bool) and lot > 1:
        available = (available // lot) * lot
    if not 0 < available < event.quantity:
        raise FixNotExecutableError(
            f"a partial settlement of {available} is not within (0, {event.quantity})"
        )
    _agree(p.get("quantity"), str(available), "quantity")
    return (
        {"trade_id": event.trade_id, "quantity": str(available)},
        {"op": "cancel_partial_settlement", "trade_id": event.trade_id},
    )


def _rebook(
    event: SettlementFailureEvent, result: InvestigationResult, p: dict[str, Any]
) -> _Built:
    raw = _find(result, ToolName.HOLIDAY_CALENDAR, "next_business_day")
    try:
        new_date = date.fromisoformat(str(raw))
    except ValueError as exc:
        raise FixNotExecutableError("no next-business-day evidence to rebook to") from exc
    if new_date <= event.settlement_date:
        raise FixNotExecutableError("next business day is not after the settlement date")
    _agree(p.get("new_settlement_date"), new_date.isoformat(), "new_settlement_date")
    return (
        {"trade_id": event.trade_id, "new_settlement_date": new_date.isoformat()},
        {
            "op": "restore_settlement_date",
            "trade_id": event.trade_id,
            "settlement_date": event.settlement_date.isoformat(),
        },
    )


_BUILDERS: dict[
    FixAction, Callable[[SettlementFailureEvent, InvestigationResult, dict[str, Any]], _Built]
] = {
    FixAction.AMEND_SSI: _amend_ssi,
    FixAction.RESUBMIT: _resubmit,
    FixAction.PARTIAL_SETTLE: _partial_settle,
    FixAction.REBOOK_SETTLEMENT_DATE: _rebook,
}


def resolve_write_fix(event: SettlementFailureEvent, result: InvestigationResult) -> ExecutableFix:
    """Verify and normalise a write fix, or raise ``FixNotExecutableError``."""
    fix = result.proposed_fix
    if fix is None:
        raise FixNotExecutableError("no proposed fix")
    if not ACTION_REQUIRES_WRITE[fix.action]:
        raise FixNotExecutableError(f"{fix.action} is read-only; nothing to execute")
    claimed = fix.parameters.get("trade_id")
    if claimed is not None and claimed != event.trade_id:
        raise FixNotExecutableError(
            f"fix targets trade {claimed!r}, not this case's trade {event.trade_id!r}"
        )
    parameters, inverse = _BUILDERS[fix.action](event, result, fix.parameters)
    return ExecutableFix(
        case_id=result.case_id,
        trade_id=event.trade_id,
        action=fix.action,
        parameters=parameters,
        inverse=inverse,
    )
