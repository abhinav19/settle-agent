# Evaluation harness

Runs the **real agent** (real Claude calls, real MCP tool servers, the production LangGraph graph)
over 300 labelled synthetic cases and answers the question the rest of the system depends on:

> *At what confidence can the agent's answers be auto-resolved safely?*

The production `AUTO_THRESHOLD` is **chosen from this harness's precision/recall curve**, not guessed.
The default of `0.9` in `.env.example` is a placeholder until you run it.

## Metrics, precisely

| Term | Meaning |
|---|---|
| **root-cause correct** | predicted category == expected (`UNDETERMINED` is the right answer for underdetermined cases) |
| **auto-resolved at threshold *t*** | the agent proposed a fix, is not `UNDETERMINED`, confidence ≥ *t*, a write fix passes remediation's evidence check, and the run was not degraded by an LLM/tool fault |
| **safe auto-resolution** | the case *is* auto-resolvable in the ground truth **and** category **and** fix are right |
| **precision** | safe ÷ auto-resolved — *of what we automate, how much was safe to automate* |
| **recall** | safe ÷ all auto-resolvable cases — *how much safe automation we capture* |
| **unsafe auto** | auto-resolved although the truth says a human should decide (the worst error) |
| **degraded** | an LLM/tool infrastructure fault; scored separately so an outage is never read as a wrong answer |

Threshold selection (`threshold.py`): highest recall subject to **precision ≥ 98 %** *and* **≥ 20 cases**
auto-resolved (so a lucky handful of confident answers can't qualify). If nothing qualifies, the answer is
"do not auto-resolve yet" — never a lower bar. Ties go to the higher threshold (same automation, more margin).

## The dataset

`dataset/v1/` — 300 cases (180 clear · 75 ambiguous · 45 underdetermined), every reason code in every tier,
each with its ground truth. `manifest.json` carries content hashes: editing a label, or changing the
synthetic reference data the labels were derived from, makes loading fail loudly. Regenerate with
`make eval-dataset`; **bump `DATASET_VERSION` if scores will move** (the guard refuses cross-version comparisons).

* *clear* — one fault, matching the reported code → auto-resolvable.
* *ambiguous* — two concurrent faults → a human should decide.
* *underdetermined* — no inconsistency in the data, or the trade is absent → the right answer is `UNDETERMINED`.

## How to spend money safely (the protocol)

A full run on Claude Opus 5.5 is estimated at **≈ $8 – $35**; the spread is hidden "thinking" tokens, which
are billed as output and vary. Do it in this order:

```bash
export ANTHROPIC_API_KEY=...

make eval-estimate                      # free: prices the run from real prompt sizes
make eval-sample EVAL_MAX_COST=3        # ~$0.5–1: 10 stratified cases, MEASURES the real per-case cost
# multiply the measured cost/case by 300, add ~30% headroom for retries, then:
make eval EVAL_MAX_COST=25              # full run + scorecard + regression guard
```

Safeguards (all tested):

* refuses to start without credentials **or** without an explicit `--max-cost-usd`;
* always prints the estimate first, and refuses a cap below the plausible minimum (`--force` overrides);
* **hard cap** — stops launching cases once spend reaches the cap (in-flight cases finish);
* **circuit breaker** — aborts after 5 consecutive degraded runs (bad key, outage) instead of burning 300 failures;
* **resumable** — re-running the same `--run-id` skips finished cases; `Ctrl-C` stops gracefully.

## After a real run

```bash
make eval-report                         # rebuild the scorecard (md + self-contained html with the PR chart)
make eval-baseline                       # write evals/baseline.json at the threshold chosen from the curve
make eval-publish                        # copy the scorecard to evals/reports/  (refuses simulated/partial runs)
git add evals/baseline.json evals/reports  # commit the evidence, after reviewing it
# then set AUTO_THRESHOLD in your .env to the threshold in baseline.json
```

Later, after any prompt or model change: `make eval` runs the guard automatically — or `make eval-check`
against an existing run. It **fails** (exit 1) if accuracy, or auto-resolve **precision at the baseline's
threshold**, drops below the baseline beyond a small tolerance (LLM output isn't bit-deterministic), or if
any extra *unsafe* automation appears. Lost automation (recall) is a warning, not a failure. A run on a
different dataset version, a partial run, a simulated run, or one with >5 % infrastructure faults is rejected.

## Simulated runs (free) — what they are and aren't

`make eval-simulate` runs the whole pipeline with a deterministic stand-in model whose answers are drawn
from ground truth with noise. It proves the *machinery* works (and is how the tests exercise it). It says
**nothing about Claude**: such runs carry a banner on every report, and baselines, `publish` and the guard
all refuse them.

## Layout

```
dataset/v1/        cases.jsonl + manifest.json      (committed, hash-verified)
evalharness/       models · dataset · metrics · threshold · baseline · execute · estimate
                   simulate · report · html_report · cli
runner.py          python runner.py run|estimate|dataset      (spec entry point)
report.py          python report.py report|baseline|check|publish
baseline.json      committed AFTER a real run (absent until then)
reports/           committed scorecard of that run (absent until then)
runs/              local run outputs (git-ignored)
```
