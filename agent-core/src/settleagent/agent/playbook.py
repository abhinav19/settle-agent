"""Playbook: reason code -> concrete read-only tool calls, plus the allowed-call catalog."""

from typing import Any, Final

from settleagent.agent.state import MARKET_PLACEHOLDER, PlannedCall
from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.reason_codes import CATALOG, ToolName

ALLOWED_CALLS: Final[dict[tuple[ToolName, str], tuple[str, ...]]] = {
    (ToolName.SECURITY_MASTER, "get_instrument"): ("isin",),
    (ToolName.SSI_STORE, "get_ssi"): ("bic", "currency"),
    (ToolName.SSI_STORE, "get_ssi_for_trade"): ("trade_id",),
    (ToolName.HOLIDAY_CALENDAR, "is_business_day"): ("market", "date"),
    (ToolName.HOLIDAY_CALENDAR, "next_business_day"): ("market", "date"),
    (ToolName.TRADE_STORE, "get_trade"): ("trade_id",),
    (ToolName.TRADE_STORE, "get_counterparty_instruction"): ("trade_id",),
}
"""Every call the agent may make: the LLM can only choose from this closed set."""


def _call(tool: ToolName, name: str, **args: Any) -> PlannedCall:
    return PlannedCall(tool=tool, name=name, args=args)


def calls_for(tool: ToolName, event: SettlementFailureEvent) -> list[PlannedCall]:
    """The standard calls for one tool server given an event."""
    day = event.settlement_date.isoformat()
    match tool:
        case ToolName.SECURITY_MASTER:
            return [_call(tool, "get_instrument", isin=event.isin)]
        case ToolName.SSI_STORE:
            return [
                _call(tool, "get_ssi_for_trade", trade_id=event.trade_id),
                _call(tool, "get_ssi", bic=event.counterparty_bic, currency=event.currency),
            ]
        case ToolName.TRADE_STORE:
            return [
                _call(tool, "get_trade", trade_id=event.trade_id),
                _call(tool, "get_counterparty_instruction", trade_id=event.trade_id),
            ]
        case ToolName.HOLIDAY_CALENDAR:
            return [
                _call(tool, "is_business_day", market=MARKET_PLACEHOLDER, date=day),
                _call(tool, "next_business_day", market=MARKET_PLACEHOLDER, date=day),
            ]


def seed_plan(event: SettlementFailureEvent) -> list[PlannedCall]:
    """Deterministic plan from the reason-code playbook; calendar calls go last (need market)."""
    tools = list(CATALOG[event.reason_code].playbook)
    tools.sort(key=lambda t: t is ToolName.HOLIDAY_CALENDAR)
    calls: list[PlannedCall] = []
    seen: set[str] = set()
    for tool in tools:
        for call in calls_for(tool, event):
            if call.key not in seen:
                seen.add(call.key)
                calls.append(call)
    return calls


def validate_suggestion(tool: str, name: str, args: dict[str, str | None]) -> PlannedCall | None:
    """Accept an LLM-suggested call only if it is in the closed catalog with all its args."""
    try:
        tool_name = ToolName(tool)
    except ValueError:
        return None
    required = ALLOWED_CALLS.get((tool_name, name))
    if required is None:
        return None
    clean = {k: args.get(k) for k in required}
    if any(not v for v in clean.values()):
        return None
    return PlannedCall(tool=tool_name, name=name, args={k: str(v) for k, v in clean.items()})
