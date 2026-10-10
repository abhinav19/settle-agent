import json
from typing import Any
from uuid import uuid4

import pytest

from settleagent.agent.graph import InvestigationAgent
from settleagent.agent.llm import LlmError, LlmRefusal
from settleagent.agent.nodes import AgentConfig, InvestigationNodes
from settleagent.agent.playbook import seed_plan, validate_suggestion
from settleagent.agent.state import initial_state
from settleagent.domain.case import Decision
from settleagent.domain.reason_codes import CATALOG, ReasonCategory, ReasonCode, ToolName
from settleagent.gate.policy import default_policy
from settleagent.gate.router import route
from settleagent.tools.gateway import ErrorKind
from tests.doubles import DownLlm, FakeGateway, RoutedLlm
from tests.factories import make_event

GOOD = {
    "root_cause": "Trade references a superseded SSI",
    "reason_category": "STATIC_DATA",
    "evidence_refs": [0],
    "fix": {"action": "AMEND_SSI", "parameters_json": json.dumps({"trade_id": "T-0001"})},
    "confidence": 0.95,
}
GATEWAY_DATA = {
    "trade-store.get_trade": {"market": "XNYS", "trade_id": "T-0001"},
    "security-master.get_instrument": {"market": "XNYS", "lot_size": 1},
}


def agent(llm: Any, gw: FakeGateway | None = None, **cfg: Any) -> InvestigationAgent:
    return InvestigationAgent(llm, gw or FakeGateway(GATEWAY_DATA), AgentConfig(**cfg))


async def run(a: InvestigationAgent, **event_over: Any):  # type: ignore[no-untyped-def]
    return await a.investigate(make_event(**event_over), uuid4(), "trace-1")


# ---------------- plan ----------------
@pytest.mark.parametrize("code", list(ReasonCode))
def test_seed_plan_covers_the_playbook_for_every_reason_code(code: ReasonCode) -> None:
    calls = seed_plan(make_event(reason_code=code.value))
    assert {c.tool for c in calls} == set(CATALOG[code].playbook)
    assert len({c.key for c in calls}) == len(calls)
    # calendar calls need the market, so they must come after the calls that provide it
    cal = [i for i, c in enumerate(calls) if c.tool is ToolName.HOLIDAY_CALENDAR]
    other = [i for i, c in enumerate(calls) if c.tool is not ToolName.HOLIDAY_CALENDAR]
    assert all(c > o for c in cal for o in other)


def test_validate_suggestion_accepts_only_catalog_calls() -> None:
    ok = validate_suggestion("trade-store", "get_trade", {"trade_id": "T1"})
    assert ok is not None and ok.args == {"trade_id": "T1"}
    assert validate_suggestion("trade-store", "delete_trade", {"trade_id": "T1"}) is None
    assert validate_suggestion("evil-server", "get_trade", {"trade_id": "T1"}) is None
    assert validate_suggestion("trade-store", "get_trade", {"trade_id": None}) is None
    assert validate_suggestion("ssi-store", "get_ssi", {"bic": "AAAAUSXX"}) is None


def extra(**k: str | None) -> dict[str, str | None]:
    base: dict[str, str | None] = {
        "tool": "ssi-store",
        "name": "get_ssi_for_trade",
        "trade_id": None,
        "isin": None,
        "bic": None,
        "currency": None,
        "market": None,
        "date": None,
    }
    return {**base, **k}


async def test_llm_extra_calls_are_validated_deduped_and_limited() -> None:
    plan = {
        "extra_calls": [
            extra(trade_id="T-0001"),
            extra(trade_id="T-0001"),  # duplicate of a seed call
            {**extra(), "tool": "rm-rf", "name": "x"},  # not in the catalog
            {**extra(), "name": "get_ssi_for_trade"},  # missing arg
        ]
    }
    llm = RoutedLlm(plan=plan, synthesize=GOOD)
    gw = FakeGateway(GATEWAY_DATA)
    await run(agent(llm, gw))
    called = [k for k, _ in gw.calls]
    assert called.count("ssi-store.get_ssi_for_trade") == 1


async def test_plan_survives_llm_failure() -> None:
    llm = RoutedLlm(plan=RuntimeError("never"), synthesize=GOOD)
    llm._replies["plan"] = [
        __import__("settleagent.agent.llm", fromlist=["LlmError"]).LlmError("x")
    ]
    result = await run(agent(llm))
    assert any("plan refinement skipped" in n for n in result.notes)
    assert result.result.confidence > 0


# ---------------- gather ----------------
async def test_gather_attaches_evidence_and_treats_not_found_as_a_finding() -> None:
    gw = FakeGateway({**GATEWAY_DATA, "ssi-store.get_ssi_for_trade": ErrorKind.NOT_FOUND})
    nodes = InvestigationNodes(RoutedLlm(), gw, AgentConfig())
    event = make_event(reason_code="SSIM")
    state = initial_state(event, uuid4(), "t")
    state["pending"] = seed_plan(event)
    update = await nodes.gather(state)
    by_source = {e.source: e for e in update["evidence"]}
    assert ToolName.TRADE_STORE in by_source and ToolName.SSI_STORE in by_source
    not_found = next(e for e in update["evidence"] if e.raw.get("error") == "NOT_FOUND")
    assert "ssi-store.get_ssi_for_trade" in not_found.statement
    assert update["missing"] == []  # a data finding is not missing evidence
    assert update["steps_used"] == len(gw.calls)


async def test_gather_records_infrastructure_failures_as_missing_not_evidence() -> None:
    gw = FakeGateway({**GATEWAY_DATA, "ssi-store.get_ssi": ErrorKind.TIMEOUT})
    nodes = InvestigationNodes(RoutedLlm(), gw, AgentConfig())
    event = make_event(reason_code="SSIM")
    state = initial_state(event, uuid4(), "t")
    state["pending"] = seed_plan(event)
    update = await nodes.gather(state)
    assert len(update["missing"]) == 1 and "ssi-store.get_ssi" in update["missing"][0]
    assert all("get_ssi:" not in e.statement for e in update["evidence"])


async def test_tool_outage_degrades_confidence_but_run_still_finalises() -> None:
    gw = FakeGateway(
        {**GATEWAY_DATA, "trade-store.get_counterparty_instruction": ErrorKind.UNAVAILABLE}
    )
    llm = RoutedLlm(synthesize={**GOOD, "confidence": 0.99})
    out = await run(agent(llm, gw, degraded_confidence_cap=0.6))
    assert out.result.confidence == 0.6
    assert any("capped" in n for n in out.notes)
    assert route(out.result, default_policy(0.9)) is Decision.HUMAN_REVIEW


async def test_calendar_calls_resolve_market_from_earlier_evidence() -> None:
    gw = FakeGateway(GATEWAY_DATA)
    await run(agent(RoutedLlm(synthesize={**GOOD, "evidence_refs": [0]}), gw), reason_code="CALD")
    cal_calls = [a for k, a in gw.calls if k.startswith("holiday-calendar")]
    assert cal_calls and all(a["market"] == "XNYS" for a in cal_calls)


async def test_unknown_market_skips_calendar_calls_and_records_it() -> None:
    gw = FakeGateway({"trade-store.get_trade": {"no_market": True}})
    out = await run(agent(RoutedLlm(synthesize=GOOD), gw), reason_code="CALD")
    assert not [k for k, _ in gw.calls if k.startswith("holiday-calendar")]
    assert out.result.confidence <= 0.6  # missing evidence cap


# ---------------- analyze / step budget ----------------
async def test_analyze_can_loop_back_for_more_evidence_but_is_bounded() -> None:
    more = {
        "sufficient": False,
        "hypothesis": "maybe the instrument",
        "additional_calls": [
            {
                "tool": "security-master",
                "name": "get_instrument",
                "trade_id": None,
                "isin": "US0378331005",
                "bic": None,
                "currency": None,
                "market": None,
                "date": None,
            }
        ],
    }
    llm = RoutedLlm(analyze=more, synthesize=GOOD)
    gw = FakeGateway(GATEWAY_DATA)
    await run(agent(llm, gw, max_analyze_rounds=2), reason_code="SSIM")
    assert [k for k, _ in gw.calls].count("security-master.get_instrument") == 1  # deduped
    assert llm.count("analyze") <= 2


async def test_step_budget_forces_low_confidence_escalation() -> None:
    llm = RoutedLlm(synthesize=GOOD)
    out = await run(agent(llm, max_steps=3))
    assert out.steps_used <= 3
    assert out.result.reason_category is ReasonCategory.UNDETERMINED
    assert out.result.confidence == 0.0
    assert route(out.result, default_policy(0.0)) is Decision.HUMAN_REVIEW


@pytest.mark.parametrize("max_steps", [1, 2, 3, 5, 8, 12])
async def test_steps_never_exceed_the_budget(max_steps: int) -> None:
    out = await run(agent(RoutedLlm(synthesize=GOOD), max_steps=max_steps))
    assert out.steps_used <= max_steps


# ---------------- synthesize ----------------
async def test_invalid_output_is_repaired_once() -> None:
    llm = RoutedLlm(synthesize=[{**GOOD, "confidence": 7}, GOOD])
    out = await run(agent(llm))
    assert llm.count("synthesize") == 2
    assert out.result.confidence == 0.95
    assert "rejected by validation" in llm.calls[-1][1]  # repair prompt carried the error


async def test_second_invalid_output_escalates_to_a_human() -> None:
    llm = RoutedLlm(synthesize=[{**GOOD, "confidence": 7}, {"nonsense": True}])
    out = await run(agent(llm))
    assert llm.count("synthesize") == 2
    assert out.result.reason_category is ReasonCategory.UNDETERMINED
    assert any("invalid model output after repair" in n for n in out.notes)


async def test_refusal_escalates() -> None:
    out = await run(agent(RoutedLlm(synthesize=LlmRefusal("no"))))
    assert out.result.reason_category is ReasonCategory.UNDETERMINED


async def test_llm_totally_down_still_finalises_with_evidence_for_the_human() -> None:
    out = await run(agent(DownLlm()))
    assert out.result.reason_category is ReasonCategory.UNDETERMINED
    assert out.result.evidence  # the tool evidence is preserved for the operator
    assert out.tokens == 0


async def test_tokens_and_model_are_accounted() -> None:
    llm = RoutedLlm(synthesize=GOOD, model="claude-test")
    out = await run(agent(llm))
    assert out.model == "claude-test"
    assert out.tokens == 120 * (llm.count("plan") + llm.count("analyze") + llm.count("synthesize"))
    assert out.result.tokens_used == out.tokens


async def test_model_cannot_flag_a_write_fix_as_read_only() -> None:
    out = await run(agent(RoutedLlm(synthesize=GOOD)))
    assert out.result.proposed_fix is not None
    assert out.result.proposed_fix.requires_write is True


async def test_runs_are_isolated() -> None:
    a = agent(RoutedLlm(synthesize=GOOD))
    r1, r2 = await __import__("asyncio").gather(run(a), run(a))
    assert r1.result.case_id != r2.result.case_id


def test_initial_state_is_clean() -> None:
    st = initial_state(make_event(), uuid4(), "t")
    assert st["pending"] == [] and st["steps_used"] == 0 and st["result"] is None


def test_nodes_construct_without_graph() -> None:
    InvestigationNodes(RoutedLlm(), FakeGateway(), AgentConfig())


async def test_llm_failures_are_counted_structurally() -> None:
    """Harnesses must distinguish an LLM outage from a wrong answer without parsing notes."""
    healthy = await run(agent(RoutedLlm(synthesize=GOOD)))
    assert healthy.llm_failures == 0
    down = await run(agent(DownLlm()))
    assert down.llm_failures == 3  # plan refinement, analyze and synthesis each failed
    plan_only = await run(agent(RoutedLlm(plan=LlmError("blip"), synthesize=GOOD)))
    assert plan_only.llm_failures == 1 and plan_only.result.confidence > 0
    bad_output = await run(
        agent(RoutedLlm(synthesize=[{**GOOD, "confidence": 7}, {"nonsense": True}]))
    )
    assert bad_output.llm_failures == 0  # invalid output is a model mistake, not an outage
