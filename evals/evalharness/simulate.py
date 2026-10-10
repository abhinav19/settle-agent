"""A deterministic stand-in for the model, so the harness can be exercised without spending.

!! SIMULATED runs prove the machinery (runner, metrics, report, guard) works. They say NOTHING
!! about how good Claude is: answers are drawn from ground truth with injected noise. The runner
!! stamps such runs ``simulated=True`` and the guard/baseline code refuses them.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from _shared.generators import CATEGORY, FIX
from settleagent.agent.llm import LlmReply

from evalharness.models import UNDETERMINED, EvalCase

MODEL_NAME = "simulated"
_CATEGORY_TO_CODE = {v: k for k, v in CATEGORY.items()}


@dataclass(frozen=True)
class Verdict:
    category: str
    fix_action: str | None
    confidence: float


def _clamp(x: float) -> float:
    return max(0.01, min(0.99, x))


class SimulatedLlm:
    """Answers each case from its ground truth, wrong with a difficulty-dependent probability.

    Wrong answers tend to be less confident than right ones, but with overlap — so the
    precision/recall curve is non-trivial. ``skill`` (0..1) scales how often it is right,
    which lets tests simulate a model regression.
    """

    model = MODEL_NAME

    def __init__(self, cases: Sequence[EvalCase], seed: int = 0, skill: float = 1.0) -> None:
        self._by_trade = {c.event["trade_id"]: c for c in cases}
        self._seed = seed
        self._skill = skill
        self.calls: list[tuple[str, int]] = []  # (kind, prompt chars) — used by the cost estimator

    def verdict(self, case: EvalCase) -> Verdict:
        rng = random.Random(f"{self._seed}:{case.case_id}")  # noqa: S311 - simulation, not security
        right = rng.random()
        code = case.reason_code
        others = sorted(set(CATEGORY.values()) - {case.expected_category})

        def fix_for(category: str) -> str:
            return FIX[_CATEGORY_TO_CODE[category]][0]

        if case.difficulty == "clear":
            if right < 0.93 * self._skill:
                return Verdict(case.expected_category, FIX[code][0], _clamp(rng.gauss(0.92, 0.06)))
            wrong = rng.choice(others)
            return Verdict(wrong, fix_for(wrong), _clamp(rng.gauss(0.68, 0.18)))
        if case.difficulty == "ambiguous":
            if right < 0.55 * self._skill:
                return Verdict(case.expected_category, FIX[code][0], _clamp(rng.gauss(0.72, 0.14)))
            wrong = case.secondary_category or rng.choice(others)
            return Verdict(wrong, fix_for(wrong), _clamp(rng.gauss(0.62, 0.18)))
        if right < 0.70 * self._skill:  # underdetermined: the right answer is "I can't tell"
            return Verdict(UNDETERMINED, None, _clamp(rng.gauss(0.10, 0.04)))
        invented = CATEGORY[code]
        return Verdict(invented, FIX[code][0], _clamp(rng.gauss(0.60, 0.20)))

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        props = schema["properties"]
        kind = "plan" if "extra_calls" in props else "analyze" if "sufficient" in props else "synth"
        self.calls.append((kind, len(system) + len(user)))
        if kind == "plan":
            data: dict[str, Any] = {"extra_calls": []}
        elif kind == "analyze":
            data = {"sufficient": True, "hypothesis": "simulated", "additional_calls": []}
        else:
            case = self._case_in(user)
            v = self.verdict(case)
            n_evidence = user.count('"index":')
            data = {
                "root_cause": "undetermined"
                if v.category == UNDETERMINED
                else f"simulated {v.category}",
                "reason_category": v.category,
                "evidence_refs": [] if v.category == UNDETERMINED else list(range(n_evidence)),
                "fix": (
                    {"action": v.fix_action, "parameters_json": "{}"} if v.fix_action else None
                ),
                "confidence": v.confidence,
            }
        return LlmReply(data=data, model=self.model, input_tokens=6500, output_tokens=420)

    def _case_in(self, prompt: str) -> EvalCase:
        for trade_id, case in self._by_trade.items():
            if f'"trade_id": "{trade_id}"' in prompt:
                return case
        raise LookupError("prompt does not mention a known trade_id")
