"""The four investigation nodes: plan -> gather -> analyze -> synthesize.

Each node is small, single-purpose and independently testable with a mocked LLM and a fake
tool gateway. All limits (step budget, analyze rounds, extra calls) are enforced here in
code. A node never raises for LLM or tool problems: it records a note and degrades.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from settleagent.agent import prompts
from settleagent.agent.llm import LlmClient, LlmError, LlmReply
from settleagent.agent.playbook import seed_plan, validate_suggestion
from settleagent.agent.result_builder import (
    InvalidModelOutputError,
    build_result,
    escalation_result,
)
from settleagent.agent.state import MARKET_PLACEHOLDER, InvestigationState, PlannedCall
from settleagent.domain.investigation import Evidence
from settleagent.domain.reason_codes import ToolName
from settleagent.tools.gateway import ErrorKind, ToolGateway, ToolOutcome

log = logging.getLogger(__name__)

TOOL_RESERVE = 2
"""Steps kept back so analyze and synthesize can always run after tool calls."""
ANALYZE_RESERVE = 1
_ARG_FIELDS = ("trade_id", "isin", "bic", "currency", "market", "date")
_SUMMARY_FIELDS = 14


@dataclass(frozen=True)
class AgentConfig:
    """Control-plane limits for one run (from settings, not from the prompt)."""

    max_steps: int = 12
    max_analyze_rounds: int = 2
    max_extra_calls: int = 3
    degraded_confidence_cap: float = 0.6
    llm_plan_refine: bool = True


def summarize(outcome: ToolOutcome) -> str:
    """Deterministic one-line statement of what a tool call found."""
    label = f"{outcome.tool.value}.{outcome.name}"
    if not outcome.ok or outcome.data is None:
        return f"{label}: {outcome.error_message or outcome.error}"
    parts = []
    for key, value in outcome.data.items():
        if isinstance(value, dict | list):
            value = json.dumps(value, default=str, sort_keys=True)
        parts.append(f"{key}={value}")
        if len(parts) >= _SUMMARY_FIELDS:
            break
    return f"{label}: " + ", ".join(parts)


def _to_evidence(outcome: ToolOutcome) -> Evidence:
    raw: dict[str, Any] = (
        outcome.data
        if outcome.ok and outcome.data is not None
        else {
            "error": outcome.error.value if outcome.error else None,
            "message": outcome.error_message,
        }
    )
    return Evidence(
        source=outcome.tool, statement=summarize(outcome), raw={"args": outcome.args, **raw}
    )


def _resolve_market(outcomes: list[ToolOutcome]) -> str | None:
    for o in outcomes:
        if o.ok and o.data and o.name in {"get_instrument", "get_trade"} and o.data.get("market"):
            return str(o.data["market"])
    return None


def _suggestions(items: Any, limit: int, taken: set[str]) -> list[PlannedCall]:
    """Validate LLM-suggested calls against the closed catalog; ignore anything else."""
    out: list[PlannedCall] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict) or len(out) >= limit:
            continue
        args = {k: item.get(k) for k in _ARG_FIELDS}
        if item.get("tool") == ToolName.HOLIDAY_CALENDAR.value and not args.get("market"):
            args["market"] = MARKET_PLACEHOLDER
        call = validate_suggestion(str(item.get("tool")), str(item.get("name")), args)
        if call is not None and call.key not in taken:
            taken.add(call.key)
            out.append(call)
    return out


class InvestigationNodes:
    def __init__(self, llm: LlmClient, gateway: ToolGateway, config: AgentConfig) -> None:
        self._llm = llm
        self._gateway = gateway
        self._cfg = config

    # ---------------------------------------------------------------- plan
    async def plan(self, state: InvestigationState) -> dict[str, Any]:
        event = state["event"]
        pending = seed_plan(event)
        notes = [f"prompt_version={prompts.PROMPT_VERSION}"]
        steps, tokens, model = state["steps_used"], state["tokens"], state["model"]

        if self._cfg.llm_plan_refine and steps < self._cfg.max_steps - TOOL_RESERVE:
            steps += 1
            try:
                reply = await self._llm.complete_json(
                    system=prompts.SYSTEM_PROMPT,
                    user=prompts.plan_prompt(event, [c.key for c in pending]),
                    schema=prompts.PLAN_SCHEMA,
                )
            except LlmError as exc:
                notes.append(f"plan refinement skipped: {exc}")
            else:
                tokens += reply.total_tokens
                model = reply.model
                extras = _suggestions(
                    reply.data.get("extra_calls"),
                    self._cfg.max_extra_calls,
                    {c.key for c in pending},
                )
                pending.extend(extras)
                if extras:
                    notes.append(f"llm added {len(extras)} call(s)")
        return {
            "pending": pending,
            "steps_used": steps,
            "tokens": tokens,
            "model": model,
            "notes": state["notes"] + notes,
        }

    # -------------------------------------------------------------- gather
    async def gather(self, state: InvestigationState) -> dict[str, Any]:
        outcomes = list(state["outcomes"])
        evidence = list(state["evidence"])
        missing = list(state["missing"])
        done = list(state["done_keys"])
        steps = state["steps_used"]
        exhausted = state["budget_exhausted"]
        pending = state["pending"]

        for index, call in enumerate(pending):
            if call.key in done:
                continue
            if steps >= self._cfg.max_steps - TOOL_RESERVE:
                exhausted = True
                missing.extend(
                    f"not run (step budget exhausted): {c.key}"
                    for c in pending[index:]
                    if c.key not in done
                )
                break
            args = dict(call.args)
            if args.get("market") == MARKET_PLACEHOLDER:
                market = _resolve_market(outcomes)
                if market is None:
                    done.append(call.key)
                    missing.append(f"skipped {call.tool.value}.{call.name}: market unknown")
                    continue
                args["market"] = market
            outcome = await self._gateway.call(call.tool, call.name, args)
            steps += 1
            done.append(call.key)
            outcomes.append(outcome)
            if outcome.ok or outcome.error is ErrorKind.NOT_FOUND:
                evidence.append(_to_evidence(outcome))  # "not found" is itself a finding
            else:
                missing.append(
                    f"{call.tool.value}.{call.name} failed ({outcome.error}): "
                    f"{outcome.error_message}"
                )
        return {
            "pending": [],
            "done_keys": done,
            "outcomes": outcomes,
            "evidence": evidence,
            "missing": missing,
            "steps_used": steps,
            "budget_exhausted": exhausted,
        }

    # ------------------------------------------------------------- analyze
    async def analyze(self, state: InvestigationState) -> dict[str, Any]:
        if state["steps_used"] >= self._cfg.max_steps - ANALYZE_RESERVE:
            return {
                "sufficient": True,
                "budget_exhausted": True,
                "notes": state["notes"] + ["analyze skipped: step budget"],
            }
        steps = state["steps_used"] + 1
        rounds = state["analyze_rounds"] + 1
        try:
            reply = await self._llm.complete_json(
                system=prompts.SYSTEM_PROMPT,
                user=prompts.analyze_prompt(state["event"], state["evidence"], state["missing"]),
                schema=prompts.ANALYZE_SCHEMA,
            )
        except LlmError as exc:
            return {
                "steps_used": steps,
                "analyze_rounds": rounds,
                "sufficient": True,
                "notes": state["notes"] + [f"analyze failed: {exc}"],
            }
        taken = set(state["done_keys"])
        new_calls = _suggestions(
            reply.data.get("additional_calls"), self._cfg.max_extra_calls, taken
        )
        sufficient = bool(reply.data.get("sufficient", True)) or not new_calls
        return {
            "steps_used": steps,
            "analyze_rounds": rounds,
            "tokens": state["tokens"] + reply.total_tokens,
            "model": reply.model,
            "hypothesis": str(reply.data.get("hypothesis", "")),
            "pending": new_calls,
            "sufficient": sufficient,
        }

    # ---------------------------------------------------------- synthesize
    async def synthesize(self, state: InvestigationState) -> dict[str, Any]:
        notes = list(state["notes"])
        steps, tokens, model = state["steps_used"], state["tokens"], state["model"]

        def fallback(reason: str) -> dict[str, Any]:
            notes.append(f"escalated: {reason}")
            return {
                "result": escalation_result(
                    case_id=state["case_id"],
                    evidence=state["evidence"],
                    model=model,
                    tokens=tokens,
                    trace_id=state["trace_id"],
                ),
                "steps_used": steps,
                "tokens": tokens,
                "model": model,
                "notes": notes,
            }

        if steps >= self._cfg.max_steps:
            return fallback("step budget exhausted before synthesis")

        user = prompts.synthesize_prompt(
            state["event"], state["evidence"], state["missing"], state["hypothesis"]
        )
        reply: LlmReply | None = None
        error = ""
        for attempt in (1, 2):  # one repair attempt
            if steps >= self._cfg.max_steps:
                return fallback("step budget exhausted during repair")
            steps += 1
            try:
                reply = await self._llm.complete_json(
                    system=prompts.SYSTEM_PROMPT, user=user, schema=prompts.SYNTHESIZE_SCHEMA
                )
            except LlmError as exc:
                return fallback(f"llm unavailable: {exc}")
            tokens += reply.total_tokens
            model = reply.model
            try:
                result, adjustments = build_result(
                    reply.data,
                    case_id=state["case_id"],
                    evidence_pool=state["evidence"],
                    missing=state["missing"],
                    model=model,
                    tokens=tokens,
                    trace_id=state["trace_id"],
                    degraded_cap=self._cfg.degraded_confidence_cap,
                )
            except InvalidModelOutputError as exc:
                error = str(exc)
                notes.append(f"synthesis attempt {attempt} invalid: {error}")
                user = (
                    prompts.synthesize_prompt(
                        state["event"], state["evidence"], state["missing"], state["hypothesis"]
                    )
                    + "\n\n"
                    + prompts.repair_prompt(reply.data, error)
                )
                continue
            return {
                "result": result,
                "steps_used": steps,
                "tokens": tokens,
                "model": model,
                "notes": notes + adjustments,
            }
        return fallback(f"invalid model output after repair: {error}")
