"""The command line, end to end: refusals that protect your wallet, and the full guard workflow.

No test here can reach the network: the Anthropic client is patched to a stand-in model.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from evalharness import cli
from evalharness.dataset import DEFAULT_DIR, load_dataset
from evalharness.execute import META_FILE, RESULTS_FILE, load_results
from evalharness.simulate import SimulatedLlm

_, CASES = load_dataset()
KEY = {"ANTHROPIC_API_KEY": "sk-ant-test-not-real"}


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(cli, "BASELINE_PATH", tmp_path / "baseline.json")
    return tmp_path


class Out(list[str]):
    def __call__(self, line: str) -> None:
        self.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self)


def invoke(*argv: str, env: dict[str, str] | None = None) -> tuple[int, Out]:
    out = Out()
    return cli.main(list(argv), env=env if env is not None else {}, out=out), out


def stand_in_model(monkeypatch: pytest.MonkeyPatch, skill: float = 1.0) -> list[dict[str, Any]]:
    """Make a 'real' run use a simulated model instead of the Anthropic API."""
    created: list[dict[str, Any]] = []

    def fake_llm(
        client: object, model: str, *, effort: str | None, max_tokens: int
    ) -> SimulatedLlm:
        created.append({"model": model, "effort": effort, "max_tokens": max_tokens})
        return SimulatedLlm(CASES, seed=0, skill=skill)

    monkeypatch.setattr(cli, "AnthropicLlm", fake_llm)
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda: object())
    return created


# ------------------------------------------------------------- protecting the wallet
def test_a_real_run_without_credentials_is_refused_and_creates_nothing(sandbox: Path) -> None:
    code, out = invoke("run", "--max-cost-usd", "50")
    assert code == 2 and "ANTHROPIC_API_KEY" in out.text and "--simulate" in out.text
    assert not (sandbox / "runs").exists()


def test_a_real_run_without_a_cost_cap_is_refused_after_showing_the_estimate(sandbox: Path) -> None:
    code, out = invoke("run", "--sample", "10", env=KEY)
    assert code == 2 and "pass --max-cost-usd" in out.text
    assert "Estimated cost for 10 case(s)" in out.text and "per case" in out.text
    assert not (sandbox / "runs").exists()


def test_a_cap_below_the_plausible_minimum_is_refused_unless_forced(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_model(monkeypatch)
    code, out = invoke("run", "--sample", "10", "--max-cost-usd", "0.01", env=KEY)
    assert code == 2 and "below the minimum plausible cost" in out.text
    assert not (sandbox / "runs").exists()
    code, _ = invoke("run", "--sample", "10", "--max-cost-usd", "0.01", "--force", env=KEY)
    assert code in {0, 1}  # allowed through; the engine's own cap then governs


def test_estimate_prints_a_range_and_its_assumptions() -> None:
    code, out = invoke("estimate", "--sample", "30")
    assert code == 0 and "Estimated cost for 30 case(s)" in out.text
    assert "thinking" in out.text and "conservative" in out.text


# ------------------------------------------------------------------- simulated runs
def test_a_simulated_run_writes_everything_and_is_labelled(sandbox: Path) -> None:
    code, out = invoke("run", "--simulate", "--run-id", "s1", "--sample", "40")
    run = sandbox / "runs" / "s1"
    assert code == 0 and "(SIMULATED)" in out.text
    meta = json.loads((run / META_FILE).read_text())
    assert meta["simulated"] is True and meta["n_completed"] == 40 and meta["model"] == "simulated"
    assert len(load_results(run / RESULTS_FILE)) == 40
    md, page = (run / "scorecard.md").read_text(), (run / "scorecard.html").read_text()
    assert "SIMULATED RUN" in md and 'class="banner"' in page and "<svg" in page


def test_a_simulated_run_cannot_become_a_baseline_or_pass_the_guard(sandbox: Path) -> None:
    invoke("run", "--simulate", "--run-id", "s1")
    code, out = invoke("baseline", "--run", str(sandbox / "runs" / "s1"), "--threshold", "0.9")
    assert code == 2 and "simulated" in out.text and not (sandbox / "baseline.json").exists()


def test_check_without_a_baseline_says_how_to_make_one(sandbox: Path) -> None:
    invoke("run", "--simulate", "--run-id", "s1", "--sample", "20")
    code, out = invoke("check", "--run", str(sandbox / "runs" / "s1"))
    assert code == 2 and "make eval-baseline" in out.text


def test_report_rebuilds_a_scorecard_for_an_existing_run(sandbox: Path) -> None:
    invoke("run", "--simulate", "--run-id", "s1", "--sample", "20")
    (sandbox / "runs" / "s1" / "scorecard.md").unlink()
    code, out = invoke("report", "--run", str(sandbox / "runs" / "s1"))
    assert code == 0 and (sandbox / "runs" / "s1" / "scorecard.md").exists()
    assert "scorecard:" in out.text


def test_report_rejects_a_directory_that_is_not_a_run(sandbox: Path) -> None:
    code, out = invoke("report", "--run", str(sandbox))
    assert code == 2 and "not a run directory" in out.text


def test_dataset_regeneration_is_a_no_op_on_unchanged_inputs() -> None:
    before = {p.name: p.read_bytes() for p in DEFAULT_DIR.iterdir()}
    code, out = invoke("dataset")
    assert code == 0 and "wrote 300 cases" in out.text
    assert {p.name: p.read_bytes() for p in DEFAULT_DIR.iterdir()} == before


# ------------------------------------- the whole regression-guard workflow (stand-in model)
def test_baseline_then_guard_passes_for_the_same_model_and_fails_for_a_worse_one(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = stand_in_model(monkeypatch, skill=1.0)

    # 1. a "real" run (the model is a stand-in; the code path is the production one)
    code, out = invoke(
        "run", "--run-id", "good", "--max-cost-usd", "1000", "--concurrency", "6", env=KEY
    )
    assert code == 0, out.text
    assert created[0] == {"model": "claude-opus-5-5", "effort": "medium", "max_tokens": 4096}
    good = sandbox / "runs" / "good"
    assert json.loads((good / META_FILE).read_text())["simulated"] is False

    # 2. commit a baseline from it
    code, out = invoke(
        "baseline", "--run", str(good), "--min-precision", "0.9", "--min-support", "5"
    )
    assert code == 0, out.text
    baseline = json.loads((sandbox / "baseline.json").read_text())
    assert baseline["n_cases"] == 300 and baseline["model"] == "claude-opus-5-5"

    # 3. the same model passes the guard
    code, out = invoke("check", "--run", str(good))
    assert code == 0 and "PASSED" in out.text

    # 4. a regression (the model got much worse) fails it, with a reason
    stand_in_model(monkeypatch, skill=0.55)
    code, out = invoke(
        "run", "--run-id", "worse", "--max-cost-usd", "1000", "--concurrency", "6", env=KEY
    )
    assert code == 0
    code, out = invoke("check", "--run", str(sandbox / "runs" / "worse"))
    assert code == 1 and "FAILED" in out.text and "FAIL" in out.text
    assert "accuracy" in out.text or "auto_precision" in out.text

    # 5. and its scorecard carries the verdict
    scorecard = (sandbox / "runs" / "worse" / "scorecard.md").read_text()
    assert "## Regression guard" in scorecard and "**FAILED**" in scorecard


def test_a_sampled_real_run_cannot_become_a_baseline(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_model(monkeypatch)
    code, _ = invoke("run", "--run-id", "part", "--sample", "30", "--max-cost-usd", "1000", env=KEY)
    assert code == 0
    code, out = invoke("baseline", "--run", str(sandbox / "runs" / "part"), "--threshold", "0.9")
    assert code == 2 and "incomplete" in out.text


def test_a_run_that_trips_the_cost_cap_exits_nonzero_and_can_be_resumed(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_model(monkeypatch)
    args = ["run", "--run-id", "capped", "--sample", "60", "--concurrency", "2", "--force"]
    code, out = invoke(*args, "--max-cost-usd", "0.5", env=KEY)
    first = len(load_results(sandbox / "runs" / "capped" / RESULTS_FILE))
    assert code == 1 and "ABORTED" in out.text and 0 < first < 60
    code, out = invoke(*args, "--max-cost-usd", "1000", env=KEY)  # same run id: resumes
    assert code == 0 and f"resuming: {first} case(s) already done" in out.text
    assert len(load_results(sandbox / "runs" / "capped" / RESULTS_FILE)) == 60


def test_the_parser_rejects_unknown_commands() -> None:
    with pytest.raises(SystemExit):
        cli.main(["nonsense"])


# ------------------------------------------------------------------------- publish
def test_publish_refuses_simulated_and_partial_runs_and_accepts_a_complete_real_one(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = sandbox / "reports"
    invoke("run", "--simulate", "--run-id", "sim")
    code, out = invoke("publish", "--run", str(sandbox / "runs" / "sim"), "--out", str(dest))
    assert code == 2 and "simulated" in out.text and not dest.exists()

    stand_in_model(monkeypatch)
    invoke("run", "--run-id", "part", "--sample", "20", "--max-cost-usd", "1000", env=KEY)
    code, out = invoke("publish", "--run", str(sandbox / "runs" / "part"), "--out", str(dest))
    assert code == 2 and "partial" in out.text and not dest.exists()

    invoke("run", "--run-id", "full", "--max-cost-usd", "1000", "--concurrency", "6", env=KEY)
    code, out = invoke("publish", "--run", str(sandbox / "runs" / "full"), "--out", str(dest))
    assert code == 0 and (dest / "scorecard.md").exists() and (dest / "scorecard.html").exists()
    assert "SIMULATED" not in (dest / "scorecard.md").read_text()
