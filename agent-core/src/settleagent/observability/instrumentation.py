"""Decorators that add spans + metrics around the LLM, the tool gateway and graph nodes.

Composition keeps the instrumented code (and its tests) free of telemetry concerns.
"""

import time
from collections.abc import Coroutine
from typing import Any, Protocol

from opentelemetry.trace import Status, StatusCode

from settleagent.agent.llm import LlmClient, LlmReply
from settleagent.agent.state import InvestigationState
from settleagent.domain.reason_codes import ToolName
from settleagent.observability.pricing import cost_usd
from settleagent.observability.telemetry import Telemetry
from settleagent.tools.gateway import ToolGateway, ToolOutcome


def schema_kind(schema: dict[str, Any]) -> str:
    """Which agent step a structured-output schema belongs to."""
    props = schema.get("properties", {})
    if "extra_calls" in props:
        return "plan"
    if "sufficient" in props:
        return "analyze"
    return "synthesize"


class TracedLlm:
    """Span per LLM call with model, tokens and USD cost; token/cost/latency metrics."""

    def __init__(self, inner: LlmClient, telemetry: Telemetry) -> None:
        self._inner = inner
        self._t = telemetry

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        kind = schema_kind(schema)
        started = time.perf_counter()
        with self._t.span("llm.complete", **{"llm.kind": kind}) as span:
            reply = await self._inner.complete_json(system=system, user=user, schema=schema)
            ms = (time.perf_counter() - started) * 1000
            cost = cost_usd(reply.model, reply.input_tokens, reply.output_tokens)
            span.set_attributes(
                {
                    "llm.model": reply.model,
                    "llm.input_tokens": reply.input_tokens,
                    "llm.output_tokens": reply.output_tokens,
                    "llm.cost_usd": cost,
                }
            )
        attrs = {"model": reply.model, "kind": kind}
        self._t.llm_tokens.add(reply.input_tokens, {**attrs, "type": "input"})
        self._t.llm_tokens.add(reply.output_tokens, {**attrs, "type": "output"})
        self._t.llm_cost.add(cost, attrs)
        self._t.llm_duration.record(ms, attrs)
        return reply


class TracedGateway:
    """Span per tool call (child of the gather node) + call/error/latency metrics."""

    def __init__(self, inner: ToolGateway, telemetry: Telemetry) -> None:
        self._inner = inner
        self._t = telemetry

    async def call(self, tool: ToolName, name: str, args: dict[str, Any]) -> ToolOutcome:
        with self._t.span("tool.call", **{"tool.server": tool.value, "tool.name": name}) as span:
            outcome = await self._inner.call(tool, name, args)
            span.set_attributes(
                {
                    "tool.ok": outcome.ok,
                    "tool.attempts": outcome.attempts,
                    "tool.latency_ms": outcome.latency_ms,
                }
            )
            if outcome.error is not None:
                span.set_attribute("tool.error", outcome.error.value)
            if outcome.is_infrastructure_failure:  # a "not found" is a finding, not a fault
                span.set_status(Status(StatusCode.ERROR, outcome.error_message or "tool failed"))
        outcome_label = "ok" if outcome.ok else (outcome.error.value if outcome.error else "error")
        attrs = {"tool": tool.value, "name": name, "outcome": outcome_label}
        self._t.tool_calls.add(1, {**attrs, "infra_failure": outcome.is_infrastructure_failure})
        self._t.tool_duration.record(outcome.latency_ms, attrs)
        return outcome


class NodeFn(Protocol):
    """A graph node. The parameter name ``state`` matters: LangGraph's stubs require it."""

    def __call__(self, state: InvestigationState) -> Coroutine[Any, Any, dict[str, Any]]: ...


def traced_node(telemetry: Telemetry, name: str, fn: NodeFn) -> NodeFn:
    """Wrap a graph node in an ``agent.<name>`` span."""

    async def wrapper(state: InvestigationState) -> dict[str, Any]:
        with telemetry.span(f"agent.{name}", case_id=str(state["case_id"])) as span:
            update = await fn(state)
            span.set_attribute("agent.steps_used", update.get("steps_used", state["steps_used"]))
            return update

    wrapper.__name__ = name
    return wrapper
