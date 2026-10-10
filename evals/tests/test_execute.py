"""The runner against the real agent graph + real in-process MCP servers + the simulated model."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from _shared.store import load_dataset as load_reference
from settleagent.agent.graph import InvestigationAgent
from settleagent.agent.llm import LlmError, LlmReply

from evalharness.dataset import load_dataset
from evalharness.estimate import estimate
from evalharness.execute import (
    META_FILE,
    RESULTS_FILE,
    RunConfig,
    build_agent,
    evaluate_case,
    load_results,
    run_eval,
    stratified_sample,
)
from evalharness.metrics import accuracy, pr_curve
from evalharness.models import UNDETERMINED, EvalCase, RunMeta
from evalharness.simulate import SimulatedLlm

logging.getLogger("mcp").setLevel(logging.CRITICAL)  # expected NOT_FOUNDs are logged as errors

REFERENCE = load_reference()
_, CASES = load_dataset()
CONFIG = RunConfig(model="simulated", simulated=True, concurrency=6)
quiet = lambda _msg: None  # noqa: E731


def agent_for(llm: Any) -> InvestigationAgent:
    return build_agent(llm, REFERENCE)


async def run(tmp: Path, cases: list[EvalCase], llm: Any = None, **cfg: Any) -> RunMeta:
    config = RunConfig(**{**CONFIG.__dict__, **cfg})
    return await run_eval(
        cases,
        agent_for(llm or SimulatedLlm(CASES)),
        config,
        tmp,
        run_id="t",
        dataset_version="1",
        dataset_cases=len(CASES),
        log=quiet,
    )


def by_difficulty(difficulty: str, n: int) -> list[EvalCase]:
    return [c for c in CASES if c.difficulty == difficulty][:n]


# ---------------------------------------------------------------- single case
async def test_a_clear_case_is_scored_with_ground_truth_denormalised(tmp_path: Path) -> None:
    case = next(c for c in CASES if c.difficulty == "clear" and c.reason_code == "SSIM")
    r = await evaluate_case(case, agent_for(SimulatedLlm(CASES)), simulated=True)
    assert r.case_id == case.case_id and r.reason_code == "SSIM" and r.difficulty == "clear"
    assert r.expected_category == case.expected_category and r.auto_resolvable
    assert r.input_tokens > 0 and r.steps_used >= 3 and r.latency_s > 0
    assert r.cost_usd == 0.0  # simulated runs never claim a price
    assert not r.degraded and r.error is None


async def test_write_fix_executability_is_judged_against_the_real_evidence() -> None:
    cases = [c for c in CASES if c.difficulty == "clear" and c.expected_requires_write][:12]
    llm = SimulatedLlm(CASES, skill=1.0)
    right = [
        await evaluate_case(c, agent_for(llm), simulated=True)
        for c in cases
        if llm.verdict(c).category == c.expected_category
    ]
    assert right and all(
        r.executable is True for r in right
    )  # right answers are backed by evidence


async def test_a_crashing_agent_becomes_a_degraded_result_not_an_exception() -> None:
    class Boom:
        async def investigate(self, *a: object, **k: object) -> object:
            raise RuntimeError("kaboom")

    boom: Any = Boom()
    r = await evaluate_case(CASES[0], boom, simulated=False)
    assert r.degraded and r.predicted_category == UNDETERMINED and "kaboom" in (r.error or "")


async def test_an_llm_outage_is_flagged_degraded_so_it_is_not_scored_as_a_wrong_answer() -> None:
    class Down:
        async def complete_json(self, **k: object) -> LlmReply:
            raise LlmError("401")

    r = await evaluate_case(CASES[0], agent_for(Down()), simulated=False)
    assert r.degraded and r.confidence == 0.0 and r.predicted_fix_action is None


# --------------------------------------------------------------------- a run
async def test_a_full_simulated_run_covers_all_cases_and_is_deterministic(tmp_path: Path) -> None:
    meta = await run(tmp_path / "a", list(CASES))
    results = load_results(tmp_path / "a" / RESULTS_FILE)
    assert meta.n_completed == 300 == len(results) and meta.aborted is None and meta.simulated
    assert len({r.case_id for r in results}) == 300
    assert sum(r.degraded for r in results) == 0
    acc = accuracy(results)
    assert acc is not None and 0.7 < acc < 0.97  # noisy, but clearly not random and not perfect
    again = await run(tmp_path / "b", list(CASES))
    first = {r.case_id: (r.predicted_category, r.confidence) for r in results}
    second = {
        r.case_id: (r.predicted_category, r.confidence)
        for r in load_results(tmp_path / "b" / RESULTS_FILE)
    }
    assert first == second and again.n_completed == 300  # concurrency does not change outcomes


async def test_the_simulated_curve_is_non_trivial_and_unsafe_automation_is_caught(
    tmp_path: Path,
) -> None:
    await run(tmp_path, list(CASES))
    results = load_results(tmp_path / RESULTS_FILE)
    curve = pr_curve(results)
    loose, strict = curve[0], curve[95]
    assert loose.auto_count > strict.auto_count > 0  # raising the threshold automates less
    assert (strict.precision or 0) > (loose.precision or 1)  # ...and the automation is safer
    assert (
        loose.unsafe_auto > 0
    )  # at threshold 0 some ambiguous/underdetermined cases get automated


async def test_run_metadata_is_written(tmp_path: Path) -> None:
    meta = await run(tmp_path, by_difficulty("clear", 5))
    on_disk = json.loads((tmp_path / META_FILE).read_text())
    assert (
        on_disk["run_id"] == "t" and on_disk["n_completed"] == 5 and on_disk["model"] == "simulated"
    )
    assert on_disk["simulated"] is True and on_disk["finished_at"] is not None
    assert meta.prompt_version == on_disk["prompt_version"] == "v1"


# ------------------------------------------------------------------ safeguards
async def test_resume_skips_finished_cases_and_never_double_counts(tmp_path: Path) -> None:
    subset = list(CASES[:20])
    await run(tmp_path, subset[:8])
    messages: list[str] = []
    meta = await run_eval(
        subset,
        agent_for(SimulatedLlm(CASES)),
        CONFIG,
        tmp_path,
        run_id="t",
        dataset_version="1",
        dataset_cases=300,
        log=messages.append,
    )
    results = load_results(tmp_path / RESULTS_FILE)
    assert len(results) == 20 == len({r.case_id for r in results}) == meta.n_completed
    assert any("resuming: 8 case(s) already done" in m for m in messages)
    assert sum(1 for m in messages if m.startswith("[")) == 12  # only the 12 new cases ran


async def test_cost_cap_stops_new_cases_and_reports_why(tmp_path: Path) -> None:
    class Priced(SimulatedLlm):
        model = "claude-opus-5-5"  # a real price, so cost accrues even in a unit test

    meta = await run_eval(
        list(CASES),
        agent_for(Priced(CASES)),
        RunConfig(model="claude-opus-5-5", concurrency=2, max_cost_usd=0.20),
        tmp_path,
        run_id="t",
        dataset_version="1",
        dataset_cases=300,
        log=quiet,
    )
    assert meta.aborted and "cost cap reached" in meta.aborted
    assert 3 <= meta.n_completed < 300  # stopped early
    # The guarantee: overshoot is bounded by what was already in flight (concurrency x the most
    # expensive case) — never the rest of the dataset.
    results = load_results(tmp_path / RESULTS_FILE)
    assert meta.total_cost_usd == pytest.approx(sum(r.cost_usd for r in results))
    assert meta.total_cost_usd <= 0.20 + 2 * max(r.cost_usd for r in results)


async def test_circuit_breaker_stops_a_doomed_run_early(tmp_path: Path) -> None:
    class Down:
        async def complete_json(self, **k: object) -> LlmReply:
            raise LlmError("invalid x-api-key")

    meta = await run(tmp_path, list(CASES), llm=Down(), abort_after_degraded=4, concurrency=1)
    assert meta.aborted and "consecutive degraded" in meta.aborted
    assert meta.n_completed == 4  # not 300 failed calls


async def test_interrupt_finishes_gracefully_and_is_resumable(tmp_path: Path) -> None:
    stop = asyncio.Event()
    stop.set()
    meta = await run_eval(
        list(CASES[:10]),
        agent_for(SimulatedLlm(CASES)),
        CONFIG,
        tmp_path,
        run_id="t",
        dataset_version="1",
        dataset_cases=300,
        stop=stop,
        log=quiet,
    )
    assert meta.aborted == "interrupted" and meta.n_completed == 0
    assert (tmp_path / META_FILE).exists()


# -------------------------------------------------------------------- sampling
def test_stratified_sample_is_deterministic_and_spread_out() -> None:
    a, b = stratified_sample(CASES, 24), stratified_sample(CASES, 24)
    assert [c.case_id for c in a] == [c.case_id for c in b] and len(a) == 24
    assert {c.difficulty for c in a} == {"clear", "ambiguous", "underdetermined"}
    assert len({c.reason_code for c in a}) == 8  # every reason code appears
    assert len(stratified_sample(CASES, 10_000)) == 300


# ------------------------------------------------------------------- estimate
async def test_estimate_gives_a_sane_range_and_flags_unknown_prices() -> None:
    e = await estimate(CASES[:60], REFERENCE, "claude-opus-5-5")
    assert 2.5 <= e.calls_per_case <= 6 and e.input_tokens_per_case > 500
    assert 0 < e.cost_per_case_low < e.cost_per_case_high
    assert e.total_low == pytest.approx(e.cost_per_case_low * 60)
    assert e.price_known and any("list price" in a for a in e.assumptions)
    odd = await estimate(CASES[:10], REFERENCE, "mystery-model")
    assert (
        not odd.price_known and odd.cost_per_case_low > e.cost_per_case_low
    )  # priced high on purpose
