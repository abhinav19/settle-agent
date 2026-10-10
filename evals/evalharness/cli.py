"""Command-line interface: dataset | estimate | run | report | baseline | check.

Spending real money is opt-in twice over: a real ``run`` needs credentials AND an explicit
``--max-cost-usd``, always prints the estimate first, and refuses a cap below the plausible minimum.
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import anthropic
from _shared.store import load_dataset as load_reference
from settleagent.agent.llm import AnthropicLlm

from evalharness.baseline import Baseline, BaselineError, check, make_baseline
from evalharness.dataset import DEFAULT_DIR, load_dataset, write_dataset
from evalharness.estimate import Estimate, estimate
from evalharness.execute import (
    META_FILE,
    RESULTS_FILE,
    RunConfig,
    build_agent,
    load_results,
    run_eval,
    stratified_sample,
)
from evalharness.html_report import to_html
from evalharness.models import RunMeta
from evalharness.report import build_scorecard, to_markdown
from evalharness.simulate import MODEL_NAME as SIM_MODEL
from evalharness.simulate import SimulatedLlm
from evalharness.threshold import DEFAULT_MIN_PRECISION, DEFAULT_MIN_SUPPORT

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
REPORTS_DIR = ROOT / "reports"
BASELINE_PATH = ROOT / "baseline.json"
DEFAULT_MODEL = "claude-opus-5-5"
Out = Callable[[str], None]


class CliError(Exception):
    """A user-facing refusal (exit code 2)."""


def _has_credentials(env: Mapping[str, str]) -> bool:
    return bool(env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"))


def _print_estimate(e: Estimate, out: Out, cases: int | None = None) -> None:
    n = cases or e.n_cases
    out(f"Estimated cost for {n} case(s) on {e.model}:")
    out(
        f"  per case : ${e.cost_per_case_low:.3f} .. ${e.cost_per_case_high:.3f}"
        f"  ({e.calls_per_case:.1f} LLM calls, ~{e.input_tokens_per_case:,} input tokens)"
    )
    out(f"  total    : ${e.cost_per_case_low * n:.2f} .. ${e.cost_per_case_high * n:.2f}")
    out("  The range is wide because hidden 'thinking' tokens are billed as output and vary.")
    for a in e.assumptions:
        out(f"    - {a}")


def _select(cases: Sequence, args: argparse.Namespace) -> list:  # type: ignore[type-arg]
    chosen = stratified_sample(cases, args.sample) if args.sample else list(cases)
    return chosen[: args.limit] if args.limit else chosen


# ----------------------------------------------------------------------- commands
def cmd_dataset(args: argparse.Namespace, env: Mapping[str, str], out: Out) -> int:
    manifest = write_dataset(load_reference())
    out(f"wrote {manifest.n_cases} cases to {DEFAULT_DIR} (mix {manifest.mix})")
    return 0


def cmd_estimate(args: argparse.Namespace, env: Mapping[str, str], out: Out) -> int:
    _, cases = load_dataset()
    selected = _select(cases, args)
    e = asyncio.run(estimate(selected, load_reference(), args.model))
    _print_estimate(e, out)
    return 0


def cmd_run(args: argparse.Namespace, env: Mapping[str, str], out: Out) -> int:
    logging.getLogger("mcp").setLevel(logging.CRITICAL)  # expected NOT_FOUNDs are not errors here
    manifest, cases = load_dataset()
    selected = _select(cases, args)
    reference = load_reference()
    simulated = args.simulate
    model = SIM_MODEL if simulated else args.model

    if not simulated:
        if not _has_credentials(env):
            raise CliError(
                "no Anthropic credentials: set ANTHROPIC_API_KEY. (Use --simulate to exercise the "
                "harness for free; simulated results are not evidence about the model.)"
            )
        est = asyncio.run(estimate(selected, reference, model))
        _print_estimate(est, out, len(selected))
        if args.max_cost_usd is None:
            raise CliError(
                "a real run spends real money: pass --max-cost-usd N "
                f"(estimate ${est.total_low:.2f}..${est.total_high:.2f}) to proceed"
            )
        if args.max_cost_usd < est.total_low and not args.force:
            raise CliError(
                f"--max-cost-usd {args.max_cost_usd} is below the minimum plausible cost "
                f"${est.total_low:.2f}: the run would abort partway. Raise it, or --force."
            )
        llm = AnthropicLlm(anthropic.AsyncAnthropic(), model, effort=args.effort, max_tokens=4096)
    else:
        llm = SimulatedLlm(cases, seed=args.seed, skill=args.skill)  # type: ignore[assignment]

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = args.run_id or f"{'sim-' if simulated else ''}{stamp}-{model}"
    out_dir = RUNS_DIR / run_id
    stop = asyncio.Event()

    async def go() -> RunMeta:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, stop.set)
        return await run_eval(
            selected,
            build_agent(llm, reference),
            RunConfig(
                model=model,
                effort=None if simulated else args.effort,
                concurrency=args.concurrency,
                max_cost_usd=args.max_cost_usd,
                abort_after_degraded=args.abort_after_degraded,
                simulated=simulated,
            ),
            out_dir,
            run_id=run_id,
            dataset_version=manifest.dataset_version,
            dataset_cases=manifest.n_cases,
            stop=stop,
            log=out,
        )

    meta = asyncio.run(go())
    _write_report(out_dir, args, None, out)
    out(
        f"run {meta.run_id}: {meta.n_completed}/{meta.n_cases} cases, "
        f"spend ${meta.total_cost_usd:.4f}{' (SIMULATED)' if simulated else ''}"
    )
    return 1 if meta.aborted else 0


def _load_run(run_dir: Path) -> tuple[RunMeta, list]:  # type: ignore[type-arg]
    if not (run_dir / META_FILE).exists():
        raise CliError(f"{run_dir} is not a run directory (no {META_FILE})")
    return RunMeta.model_validate_json((run_dir / META_FILE).read_text()), load_results(
        run_dir / RESULTS_FILE
    )


def _load_baseline(path: Path) -> Baseline | None:
    return Baseline.model_validate_json(path.read_text()) if path.exists() else None


def _write_report(
    run_dir: Path, args: argparse.Namespace, baseline_path: Path | None, out: Out
) -> None:
    meta, results = _load_run(run_dir)
    baseline = _load_baseline(baseline_path or BASELINE_PATH)
    guard = check(baseline, meta, results) if baseline and not meta.simulated else None
    card = build_scorecard(
        meta,
        results,
        min_precision=getattr(args, "min_precision", DEFAULT_MIN_PRECISION),
        min_support=getattr(args, "min_support", DEFAULT_MIN_SUPPORT),
        baseline=baseline,
        guard=guard,
    )
    (run_dir / "scorecard.md").write_text(to_markdown(card))
    (run_dir / "scorecard.html").write_text(to_html(card))
    if card.chosen:
        out(
            f"threshold chosen from the curve: {card.chosen.threshold:.2f} "
            f"(precision {card.chosen.precision:.1%}, recall {card.chosen.recall or 0:.1%})"
        )
    else:
        out("no threshold meets the precision bar: do not auto-resolve yet")
    out(f"scorecard: {run_dir / 'scorecard.md'}  and  {run_dir / 'scorecard.html'}")


def cmd_report(args: argparse.Namespace, env: Mapping[str, str], out: Out) -> int:
    _write_report(args.run, args, args.baseline, out)
    return 0


def cmd_baseline(args: argparse.Namespace, env: Mapping[str, str], out: Out) -> int:
    meta, results = _load_run(args.run)
    threshold = args.threshold
    if threshold is None:
        card = build_scorecard(
            meta, results, min_precision=args.min_precision, min_support=args.min_support
        )
        if card.chosen is None:
            raise CliError("no threshold meets the precision bar; pass --threshold to force one")
        threshold = card.chosen.threshold
    try:
        baseline = make_baseline(meta, results, threshold)
    except BaselineError as exc:
        raise CliError(str(exc)) from exc
    args.out.write_text(json.dumps(baseline.model_dump(mode="json"), indent=1) + "\n")
    out(
        f"wrote {args.out}: threshold {threshold:.2f}, accuracy {baseline.accuracy:.3f}, "
        f"auto precision {baseline.auto_precision}. Review it, then commit it."
    )
    return 0


def cmd_publish(args: argparse.Namespace, env: Mapping[str, str], out: Out) -> int:
    """Copy a vetted run's scorecard into evals/reports/ — the committed evidence artifact."""
    meta, results = _load_run(args.run)
    if meta.simulated:
        raise CliError("refusing to publish a simulated run: it is not evidence about the model")
    if meta.aborted or meta.n_cases != meta.dataset_cases or len(results) != meta.n_cases:
        raise CliError("refusing to publish a partial run; publish only a complete real run")
    args.out.mkdir(parents=True, exist_ok=True)
    for name in ("scorecard.md", "scorecard.html"):
        if not (args.run / name).exists():
            raise CliError(f"{args.run / name} is missing; run `make eval-report` first")
        shutil.copyfile(args.run / name, args.out / name)
    out(f"published run {meta.run_id} to {args.out}. Review, then commit it with baseline.json.")
    return 0


def cmd_check(args: argparse.Namespace, env: Mapping[str, str], out: Out) -> int:
    baseline = _load_baseline(args.baseline)
    if baseline is None:
        raise CliError(f"no baseline at {args.baseline}; run `make eval-baseline` after a real run")
    meta, results = _load_run(args.run)
    report = check(baseline, meta, results)
    for f in report.findings:
        out(f"{f.level.upper():5} {f.metric}: {f.message}")
    out("regression guard: " + ("PASSED" if report.passed else "FAILED"))
    return 0 if report.passed else 1


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evals", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def selecting(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--sample", type=int, help="stratified sample of N cases (cost measurement)"
        )
        sp.add_argument("--limit", type=int, help="first N cases after any sampling")

    sub.add_parser(
        "dataset", help="regenerate evals/dataset from the seeded generator"
    ).set_defaults(fn=cmd_dataset)

    sp = sub.add_parser("estimate", help="price a run before spending anything")
    sp.add_argument("--model", default=DEFAULT_MODEL)
    selecting(sp)
    sp.set_defaults(fn=cmd_estimate)

    sp = sub.add_parser("run", help="run the agent over the dataset and write a scorecard")
    sp.add_argument("--model", default=DEFAULT_MODEL)
    sp.add_argument("--effort", default="medium")
    sp.add_argument("--concurrency", type=int, default=4)
    sp.add_argument("--max-cost-usd", type=float, help="REQUIRED for real runs: hard spend cap")
    sp.add_argument("--abort-after-degraded", type=int, default=5)
    sp.add_argument("--run-id")
    sp.add_argument("--force", action="store_true", help="allow a cap below the plausible minimum")
    sp.add_argument("--simulate", action="store_true", help="FREE: use a simulated model")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--skill", type=float, default=1.0, help="simulated model accuracy scale")
    sp.add_argument("--min-precision", type=float, default=DEFAULT_MIN_PRECISION)
    sp.add_argument("--min-support", type=int, default=DEFAULT_MIN_SUPPORT)
    selecting(sp)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("report", help="(re)build a run's scorecard")
    sp.add_argument("--run", type=Path, required=True)
    sp.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    sp.add_argument("--min-precision", type=float, default=DEFAULT_MIN_PRECISION)
    sp.add_argument("--min-support", type=int, default=DEFAULT_MIN_SUPPORT)
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("baseline", help="write baseline.json from a real, complete run")
    sp.add_argument("--run", type=Path, required=True)
    sp.add_argument("--threshold", type=float, help="default: the threshold chosen from the curve")
    sp.add_argument("--out", type=Path, default=BASELINE_PATH)
    sp.add_argument("--min-precision", type=float, default=DEFAULT_MIN_PRECISION)
    sp.add_argument("--min-support", type=int, default=DEFAULT_MIN_SUPPORT)
    sp.set_defaults(fn=cmd_baseline)

    sp = sub.add_parser("publish", help="copy a complete REAL run's scorecard to evals/reports/")
    sp.add_argument("--run", type=Path, required=True)
    sp.add_argument("--out", type=Path, default=REPORTS_DIR)
    sp.set_defaults(fn=cmd_publish)

    sp = sub.add_parser(
        "check", help="regression guard: exit 1 if a run is worse than the baseline"
    )
    sp.add_argument("--run", type=Path, required=True)
    sp.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    sp.set_defaults(fn=cmd_check)
    return p


def main(
    argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, out: Out = print
) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args, os.environ if env is None else env, out))
    except CliError as exc:
        out(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
