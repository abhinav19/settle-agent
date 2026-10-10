"""Estimate what a real run would cost BEFORE spending anything.

It runs the exact production agent over the dataset with the free simulated model, records the
real prompt sizes and LLM-call counts, and prices them. The one big unknown is hidden "thinking"
output (billed as output; cannot be disabled on current models), so the result is a RANGE.
Treat it as a sanity bound, then measure a real sample (``--sample 10``) before the full run.
"""

import json
from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

from _shared.models import Dataset as ReferenceData
from pydantic import BaseModel, ConfigDict
from settleagent.domain.events import SettlementFailureEvent
from settleagent.observability.pricing import PRICES_PER_MTOK, cost_usd, is_known_model

from evalharness.execute import build_agent
from evalharness.models import EvalCase
from evalharness.simulate import SimulatedLlm

CHARS_PER_TOKEN = 3.0
"""Conservative: JSON, ids and numbers tokenise worse than prose (~4 chars/token)."""
VISIBLE_OUTPUT_TOKENS = {"plan": 120, "analyze": 150, "synth": 350}
THINKING_TOKENS_HIGH = 1500
"""Extra hidden output tokens per call in the high case."""


class Estimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    price_known: bool
    n_cases: int
    calls_per_case: float
    input_tokens_per_case: int
    visible_output_tokens_per_case: int
    cost_per_case_low: float
    cost_per_case_high: float
    total_low: float
    total_high: float
    assumptions: list[str]


def _price_note(model: str) -> str:
    if model not in PRICES_PER_MTOK:
        return "model not in the price table: priced at the most expensive known rate"
    price_in, price_out = (float(x) for x in PRICES_PER_MTOK[model])
    return f"list price per MTok: ${price_in:g} in / ${price_out:g} out"


async def estimate(cases: Sequence[EvalCase], reference: ReferenceData, model: str) -> Estimate:
    llm = SimulatedLlm(cases)
    agent = build_agent(llm, reference)
    for c in cases:
        event = SettlementFailureEvent.model_validate_json(json.dumps(c.event))
        await agent.investigate(event, uuid5(NAMESPACE_URL, c.case_id), "estimate")
    n = len(cases)
    calls = len(llm.calls)
    in_tokens = round(sum(chars for _, chars in llm.calls) / CHARS_PER_TOKEN / n)
    visible = round(sum(VISIBLE_OUTPUT_TOKENS[kind] for kind, _ in llm.calls) / n)
    thinking_high = round(THINKING_TOKENS_HIGH * calls / n)
    low = cost_usd(model, in_tokens, visible)
    high = cost_usd(model, in_tokens, visible + thinking_high)
    return Estimate(
        model=model,
        price_known=is_known_model(model),
        n_cases=n,
        calls_per_case=calls / n,
        input_tokens_per_case=in_tokens,
        visible_output_tokens_per_case=visible,
        cost_per_case_low=low,
        cost_per_case_high=high,
        total_low=low * n,
        total_high=high * n,
        assumptions=[
            f"{CHARS_PER_TOKEN} characters per input token (conservative)",
            f"visible output tokens per call: {VISIBLE_OUTPUT_TOKENS}",
            f"high case adds {THINKING_TOKENS_HIGH} hidden thinking tokens per call "
            "(billed as output; thinking cannot be disabled on current models)",
            "no prompt-cache discount; no retries or repair attempts; list prices",
            _price_note(model),
        ],
    )
