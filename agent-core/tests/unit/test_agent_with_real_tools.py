"""The compiled graph against the real MCP servers (in-process) with an oracle LLM.

The oracle answers from the scenario's ground truth, so these tests verify the plumbing
(plan -> real tool calls -> evidence -> validated result -> gate), not model accuracy.
"""

import json
from uuid import uuid4

import pytest

from settleagent.agent.graph import InvestigationAgent
from settleagent.agent.nodes import AgentConfig
from settleagent.domain.case import Decision
from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.investigation import ACTION_REQUIRES_WRITE, FixAction
from settleagent.domain.reason_codes import ReasonCategory, ToolName
from settleagent.gate.policy import default_policy
from settleagent.gate.router import route
from tests.doubles import DATASET, RoutedLlm, oracle_reply, real_gateway

gateway = real_gateway
oracle = oracle_reply


@pytest.mark.parametrize("difficulty", ["clear", "ambiguous", "underdetermined"])
async def test_every_scenario_runs_to_a_valid_routed_result(difficulty: str) -> None:
    policy = default_policy(0.9)
    gw = gateway()
    for s in (x for x in DATASET.scenarios if x.difficulty == difficulty):
        agent = InvestigationAgent(RoutedLlm(synthesize=oracle(s)), gw, AgentConfig())
        event = SettlementFailureEvent.model_validate_json(json.dumps(s.event))
        run = await agent.investigate(event, uuid4(), "t")
        r = run.result
        assert run.steps_used <= 12
        assert r.reason_category.value == s.expected_category
        assert not any(o.is_infrastructure_failure for o in run.outcomes)
        if r.proposed_fix:
            assert r.proposed_fix.requires_write == ACTION_REQUIRES_WRITE[r.proposed_fix.action]
        decision = route(r, policy)
        assert (decision is Decision.AUTO_RESOLVE) == s.auto_resolvable, s.scenario_id


async def test_clear_ssim_case_gathers_the_decisive_ssi_evidence() -> None:
    s = next(
        x for x in DATASET.scenarios if x.difficulty == "clear" and x.event["reason_code"] == "SSIM"
    )
    agent = InvestigationAgent(RoutedLlm(synthesize=oracle(s)), gateway(), AgentConfig())
    event = SettlementFailureEvent.model_validate_json(json.dumps(s.event))
    run = await agent.investigate(event, uuid4(), "t")
    ssi_ev = [e for e in run.result.evidence if e.source is ToolName.SSI_STORE]
    assert any(e.raw.get("is_current") is False for e in ssi_ev)
    assert run.result.reason_category is ReasonCategory.STATIC_DATA
    assert run.result.proposed_fix is not None
    assert run.result.proposed_fix.action is FixAction.AMEND_SSI
