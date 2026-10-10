"""Hand-written fakes for the LLM and the tool gateway (no network, no mocks library)."""

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import holiday_calendar.server as hc
import security_master.server as sm
import ssi_store.server as ss
import trade_store.server as ts
from _shared.generators import generate_dataset
from _shared.models import Scenario
from mcp import Client

from settleagent.agent.llm import LlmError, LlmReply
from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.reason_codes import ToolName
from settleagent.tools.gateway import ErrorKind, McpToolGateway, ToolOutcome

Reply = dict[str, Any] | Exception | Callable[[str], dict[str, Any]]


def _kind(schema: dict[str, Any]) -> str:
    props = schema["properties"]
    if "extra_calls" in props:
        return "plan"
    if "sufficient" in props:
        return "analyze"
    return "synthesize"


class RoutedLlm:
    """Replies per node kind. Each entry is a dict, an exception, a callable(user) or a list
    of those consumed in order (the last one repeats)."""

    def __init__(
        self,
        plan: Reply | list[Reply] | None = None,
        analyze: Reply | list[Reply] | None = None,
        synthesize: Reply | list[Reply] | None = None,
        model: str = "fake-model",
    ) -> None:
        self._replies = {
            "plan": self._listify(plan if plan is not None else {"extra_calls": []}),
            "analyze": self._listify(
                analyze
                if analyze is not None
                else {"sufficient": True, "hypothesis": "h", "additional_calls": []}
            ),
            "synthesize": self._listify(synthesize),
        }
        self.model = model
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _listify(r: Any) -> list[Any]:
        if r is None:
            return []
        return r if isinstance(r, list) else [r]

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        kind = _kind(schema)
        self.calls.append((kind, user))
        queue = self._replies[kind]
        if not queue:
            raise AssertionError(f"unexpected {kind} call")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        data = item(user) if callable(item) else item
        return LlmReply(data=data, model=self.model, input_tokens=100, output_tokens=20)

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)


class DownLlm:
    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        raise LlmError("llm down")


class FakeGateway:
    """Scripted gateway: ``responses`` maps ``"tool.name"`` to data / ErrorKind / callable."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: ToolName, name: str, args: dict[str, Any]) -> ToolOutcome:
        key = f"{tool.value}.{name}"
        self.calls.append((key, args))
        r = self.responses.get(key, {"fake": True})
        if callable(r):
            r = r(args)
        if isinstance(r, ErrorKind):
            return ToolOutcome(
                tool=tool, name=name, args=args, ok=False, error=r, error_message=str(r), attempts=1
            )
        return ToolOutcome(tool=tool, name=name, args=args, ok=True, data=r, attempts=1)


DATASET = generate_dataset(42, 300)


def real_gateway(**kw: Any) -> McpToolGateway:
    """Gateway over the four real MCP servers, in-process (full MCP protocol, no network)."""
    return McpToolGateway(
        {
            ToolName.SECURITY_MASTER: lambda: Client(sm.build_server(DATASET)),
            ToolName.SSI_STORE: lambda: Client(ss.build_server(DATASET)),
            ToolName.HOLIDAY_CALENDAR: lambda: Client(hc.build_server(DATASET)),
            ToolName.TRADE_STORE: lambda: Client(ts.build_server(DATASET)),
        },
        **kw,
    )


def oracle_reply(s: Scenario) -> Callable[[str], dict[str, Any]]:
    """Synthesis reply from ground truth, citing every evidence item the tools returned."""

    def reply(user: str) -> dict[str, Any]:
        n = user.count('"index":')
        fix = None
        if s.expected_fix_action:
            fix = {
                "action": s.expected_fix_action,
                "parameters_json": json.dumps({"trade_id": s.event["trade_id"]}),
            }
        undetermined = s.expected_category == "UNDETERMINED"
        return {
            "root_cause": "undetermined" if undetermined else f"Ground truth: {s.note}",
            "reason_category": s.expected_category,
            "evidence_refs": [] if undetermined else list(range(n)),
            "fix": fix,
            "confidence": 0.97 if s.auto_resolvable else (0.05 if undetermined else 0.5),
        }

    return reply


def pick(difficulty: str, code: str, note_contains: str | None = None) -> Scenario:
    return next(
        s
        for s in DATASET.scenarios
        if s.difficulty == difficulty
        and s.event["reason_code"] == code
        and (note_contains is None or note_contains in s.note)
    )


def fresh_event(s: Scenario) -> SettlementFailureEvent:
    """The scenario's event with a new event_id so each test creates its own case."""
    return SettlementFailureEvent.model_validate_json(
        json.dumps({**s.event, "event_id": str(uuid4())})
    )


class DatasetOracleLlm:
    """Answers any dataset scenario from ground truth, keyed by the prompt's trade_id."""

    def __init__(self) -> None:
        self._by_trade = {s.event["trade_id"]: s for s in DATASET.scenarios}
        self.model = "oracle"

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        props = schema["properties"]
        if "extra_calls" in props:
            data: dict[str, Any] = {"extra_calls": []}
        elif "sufficient" in props:
            data = {"sufficient": True, "hypothesis": "oracle", "additional_calls": []}
        else:
            import re

            trade_id = re.search(r'"trade_id": "([^"]+)"', user)
            assert trade_id, "prompt must contain the event's trade_id"
            data = oracle_reply(self._by_trade[trade_id.group(1)])(user)
        return LlmReply(data=data, model=self.model, input_tokens=50, output_tokens=10)
