"""Run the REAL agent over the dataset and record one ``CaseResult`` per case.

Safeguards, because this is the one component that can spend real money:
* **hard cost cap** — stops launching cases once cumulative spend reaches ``max_cost_usd``
  (in-flight cases finish, so overshoot is bounded by ``concurrency`` x per-case cost);
* **circuit breaker** — stops after N consecutive infrastructure-degraded runs (bad credentials,
  an outage), instead of burning through 300 doomed calls;
* **resumable** — results are appended as they complete; re-running skips finished cases;
* **graceful stop** — ``stop`` lets an interrupt finish in-flight cases and still write run.json.
"""

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import holiday_calendar.server as holiday_calendar
import security_master.server as security_master
import ssi_store.server as ssi_store
import trade_store.server as trade_store
from _shared.models import Dataset as ReferenceData
from mcp import Client
from settleagent.agent import prompts
from settleagent.agent.graph import InvestigationAgent
from settleagent.agent.llm import LlmClient
from settleagent.agent.nodes import AgentConfig
from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.reason_codes import ToolName
from settleagent.observability.pricing import cost_usd
from settleagent.remediation.models import FixNotExecutableError
from settleagent.remediation.resolve import resolve_write_fix
from settleagent.tools.gateway import McpToolGateway

from evalharness.models import UNDETERMINED, CaseResult, EvalCase, RunMeta

RESULTS_FILE = "results.jsonl"
META_FILE = "run.json"


@dataclass(frozen=True)
class RunConfig:
    model: str
    effort: str | None = "medium"
    concurrency: int = 4
    max_cost_usd: float | None = None
    abort_after_degraded: int = 5
    simulated: bool = False


def build_agent(
    llm: LlmClient, reference: ReferenceData, agent_config: AgentConfig | None = None
) -> InvestigationAgent:
    """The production agent graph over the real MCP servers (in-process, full MCP protocol)."""
    gateway = McpToolGateway(
        {
            ToolName.SECURITY_MASTER: lambda: Client(security_master.build_server(reference)),
            ToolName.SSI_STORE: lambda: Client(ssi_store.build_server(reference)),
            ToolName.HOLIDAY_CALENDAR: lambda: Client(holiday_calendar.build_server(reference)),
            ToolName.TRADE_STORE: lambda: Client(trade_store.build_server(reference)),
        }
    )
    return InvestigationAgent(llm, gateway, agent_config or AgentConfig())


def _append_result(path: Path, result: CaseResult) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(result.model_dump(mode="json"), sort_keys=True) + "\n")


def _write_meta(out_dir: Path, meta: RunMeta) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / META_FILE).write_text(json.dumps(meta.model_dump(mode="json"), indent=1) + "\n")


def load_results(path: Path) -> list[CaseResult]:
    if not path.exists():
        return []
    return [CaseResult.model_validate_json(ln) for ln in path.read_text().splitlines() if ln]


async def evaluate_case(
    case: EvalCase,
    agent: InvestigationAgent,
    *,
    simulated: bool,
    clock: Callable[[], float] = time.perf_counter,
) -> CaseResult:
    event = SettlementFailureEvent.model_validate_json(json.dumps(case.event))
    case_uuid = uuid5(NAMESPACE_URL, case.case_id)  # stable id per case
    trace_id = uuid4().hex
    base = {
        "case_id": case.case_id,
        "difficulty": case.difficulty,
        "reason_code": case.reason_code,
        "expected_category": case.expected_category,
        "expected_fix_action": case.expected_fix_action,
        "auto_resolvable": case.auto_resolvable,
        "trace_id": trace_id,
    }
    started = clock()
    try:
        run = await agent.investigate(event, case_uuid, trace_id)
    except Exception as exc:  # noqa: BLE001 - one broken case must not end a paid run
        return CaseResult(
            **base,
            predicted_category=UNDETERMINED,
            predicted_fix_action=None,
            requires_write=None,
            executable=None,
            confidence=0.0,
            root_cause="",
            steps_used=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            latency_s=clock() - started,
            degraded=True,
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
    latency = clock() - started
    result, fix = run.result, run.result.proposed_fix
    executable: bool | None = None
    if fix is not None and fix.requires_write:
        try:
            resolve_write_fix(event, result)
            executable = True
        except FixNotExecutableError:
            executable = False
    return CaseResult(
        **base,
        predicted_category=result.reason_category.value,
        predicted_fix_action=fix.action.value if fix else None,
        requires_write=fix.requires_write if fix else None,
        executable=executable,
        confidence=result.confidence,
        root_cause=result.root_cause,
        steps_used=run.steps_used,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cost_usd=0.0 if simulated else cost_usd(run.model, run.input_tokens, run.output_tokens),
        latency_s=latency,
        degraded=run.llm_failures > 0 or any(o.is_infrastructure_failure for o in run.outcomes),
        notes=run.notes,
    )


def stratified_sample(cases: Sequence[EvalCase], n: int) -> list[EvalCase]:
    """A deterministic sample spread across difficulty x reason code (for cost measurement)."""
    if n >= len(cases):
        return list(cases)
    buckets: dict[tuple[str, str], list[EvalCase]] = {}
    for c in sorted(cases, key=lambda c: c.case_id):
        buckets.setdefault((c.difficulty, c.reason_code), []).append(c)
    chosen: list[EvalCase] = []
    keys = sorted(buckets)
    i = 0
    while len(chosen) < n:  # round-robin over the buckets
        bucket = buckets[keys[i % len(keys)]]
        if bucket:
            chosen.append(bucket.pop(0))
        i += 1
        if not any(buckets.values()):
            break
    return sorted(chosen, key=lambda c: c.case_id)


async def run_eval(
    cases: Sequence[EvalCase],
    agent: InvestigationAgent,
    config: RunConfig,
    out_dir: Path,
    *,
    run_id: str,
    dataset_version: str,
    dataset_cases: int,
    stop: asyncio.Event | None = None,
    log: Callable[[str], None] = print,
) -> RunMeta:
    results_path = out_dir / RESULTS_FILE
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one-off, before any concurrency
    done = {r.case_id: r for r in load_results(results_path)}
    todo = [c for c in cases if c.case_id not in done]
    spent = sum(r.cost_usd for r in done.values())
    started = datetime.now(UTC)
    meta = RunMeta(
        run_id=run_id,
        started_at=started,
        model=config.model,
        prompt_version=prompts.PROMPT_VERSION,
        dataset_version=dataset_version,
        effort=config.effort,
        concurrency=config.concurrency,
        simulated=config.simulated,
        n_cases=len(cases),
        dataset_cases=dataset_cases,
    )
    if done:
        log(f"resuming: {len(done)} case(s) already done, {len(todo)} to go")

    stop = stop or asyncio.Event()
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(config.concurrency)
    aborted: list[str] = []
    state = {"spent": spent, "consecutive_degraded": 0, "n": len(done)}

    async def one(case: EvalCase) -> None:
        async with sem:
            if aborted or stop.is_set():
                return
            result = await evaluate_case(case, agent, simulated=config.simulated)
            async with lock:
                _append_result(results_path, result)
                done[case.case_id] = result
                state["n"] += 1
                state["spent"] += result.cost_usd
                state["consecutive_degraded"] = (
                    state["consecutive_degraded"] + 1 if result.degraded else 0
                )
                log(
                    f"[{state['n']}/{len(cases)}] {case.case_id} {case.reason_code:<4} "
                    f"{case.difficulty:<15} -> {result.predicted_category:<12} "
                    f"conf={result.confidence:.2f} ${result.cost_usd:.4f} {result.latency_s:.1f}s"
                    f"{' DEGRADED' if result.degraded else ''}"
                )
                if (
                    config.max_cost_usd is not None
                    and state["spent"] >= config.max_cost_usd
                    and not aborted
                ):
                    aborted.append(
                        f"cost cap reached: ${state['spent']:.2f} >= ${config.max_cost_usd:.2f}"
                    )
                if state["consecutive_degraded"] >= config.abort_after_degraded and not aborted:
                    aborted.append(
                        f"{state['consecutive_degraded']} consecutive degraded runs "
                        "(credentials? outage?)"
                    )

    await asyncio.gather(*(one(c) for c in todo))
    if stop.is_set() and not aborted and len(done) < len(cases):
        aborted.append("interrupted")
    final = meta.model_copy(
        update={
            "finished_at": datetime.now(UTC),
            "n_completed": len(done),
            "total_cost_usd": state["spent"],
            "aborted": aborted[0] if aborted else None,
        }
    )
    _write_meta(out_dir, final)
    if final.aborted:
        log(f"ABORTED: {final.aborted}. Re-run the same command to resume.")
    return final
