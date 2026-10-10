"""LangGraph wiring. The graph is compiled once and reused; runs are isolated by state."""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from settleagent.agent.llm import LlmClient
from settleagent.agent.nodes import TOOL_RESERVE, AgentConfig, InvestigationNodes
from settleagent.agent.state import InvestigationState, initial_state
from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.investigation import InvestigationResult
from settleagent.observability.instrumentation import traced_node
from settleagent.observability.telemetry import NOOP, Telemetry
from settleagent.tools.gateway import ToolGateway, ToolOutcome

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentRun:
    """What one investigation produced (everything the pipeline needs to persist)."""

    result: InvestigationResult
    outcomes: list[ToolOutcome]
    notes: list[str]
    steps_used: int
    tokens: int
    input_tokens: int
    output_tokens: int
    llm_failures: int
    model: str


def build_graph(nodes: InvestigationNodes, config: AgentConfig, telemetry: Telemetry = NOOP) -> Any:
    """plan -> gather -> analyze -> (gather again | synthesize); bounded by config."""

    def after_gather(state: InvestigationState) -> str:
        return "synthesize" if state["budget_exhausted"] else "analyze"

    def after_analyze(state: InvestigationState) -> str:
        again = (
            not state["sufficient"]
            and bool(state["pending"])
            and not state["budget_exhausted"]
            and state["analyze_rounds"] < config.max_analyze_rounds
            and state["steps_used"] < config.max_steps - TOOL_RESERVE
        )
        return "gather" if again else "synthesize"

    graph = StateGraph(InvestigationState)
    graph.add_node("plan", traced_node(telemetry, "plan", nodes.plan))
    graph.add_node("gather", traced_node(telemetry, "gather", nodes.gather))
    graph.add_node("analyze", traced_node(telemetry, "analyze", nodes.analyze))
    graph.add_node("synthesize", traced_node(telemetry, "synthesize", nodes.synthesize))
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "gather")
    graph.add_conditional_edges(
        "gather", after_gather, {"analyze": "analyze", "synthesize": "synthesize"}
    )
    graph.add_conditional_edges(
        "analyze", after_analyze, {"gather": "gather", "synthesize": "synthesize"}
    )
    graph.add_edge("synthesize", END)
    return graph.compile()


class InvestigationAgent:
    """Compiled-once investigation graph with injected LLM, tool gateway and limits."""

    def __init__(
        self,
        llm: LlmClient,
        gateway: ToolGateway,
        config: AgentConfig | None = None,
        telemetry: Telemetry = NOOP,
    ) -> None:
        self._config = config or AgentConfig()
        self._graph = build_graph(
            InvestigationNodes(llm, gateway, self._config), self._config, telemetry
        )

    async def investigate(
        self, event: SettlementFailureEvent, case_id: UUID, trace_id: str
    ) -> AgentRun:
        final: InvestigationState = await self._graph.ainvoke(
            initial_state(event, case_id, trace_id)
        )
        result = final["result"]
        if result is None:  # defensive: synthesize always sets it
            raise RuntimeError("investigation graph finished without a result")
        return AgentRun(
            result=result,
            outcomes=final["outcomes"],
            notes=final["notes"],
            steps_used=final["steps_used"],
            tokens=final["tokens"],
            input_tokens=final["input_tokens"],
            output_tokens=final["output_tokens"],
            llm_failures=final["llm_failures"],
            model=final["model"],
        )
