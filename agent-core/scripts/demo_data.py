"""Populate the dev database with demo cases so the dashboard has something to show.

!! DEMO ONLY. This does NOT run the real agent. An "oracle" answers from the synthetic
!! dataset's ground truth (model name: ``oracle-demo``), so the cases look plausible but say
!! nothing about real model quality. Use ``make eval`` (Phase 7) to measure the real agent.

Needs DATABASE_URL (migrated database). Uses the real MCP tool logic in-process, the real
confidence gate and the real case processor. No Anthropic API calls, no cost.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # make `tests.doubles` importable (dev tool, not shipped code)

from settleagent.agent.graph import InvestigationAgent  # noqa: E402
from settleagent.agent.nodes import AgentConfig  # noqa: E402
from settleagent.config import get_settings  # noqa: E402
from settleagent.consumer.processor import CaseProcessor  # noqa: E402
from settleagent.domain.events import SettlementFailureEvent  # noqa: E402
from settleagent.gate.policy import default_policy  # noqa: E402
from settleagent.observability.instrumentation import TracedGateway, TracedLlm  # noqa: E402
from settleagent.observability.logging import configure_logging  # noqa: E402
from settleagent.observability.telemetry import setup_telemetry  # noqa: E402
from settleagent.persistence.db import make_engine, make_session_factory  # noqa: E402
from tests.doubles import DATASET, DatasetOracleLlm, real_gateway  # noqa: E402


async def main(count: int) -> None:
    settings = get_settings()
    configure_logging("WARNING", json_logs=False)
    telemetry = setup_telemetry(
        "settleagent-demo",
        settings.otel_exporter_otlp_endpoint,
        enabled=settings.otel_enabled,
        metric_interval_ms=1000,
    )
    engine = make_engine(settings.database_url)
    llm = DatasetOracleLlm()
    llm.model = "oracle-demo"
    processor = CaseProcessor(
        make_session_factory(engine),
        InvestigationAgent(
            TracedLlm(llm, telemetry),
            TracedGateway(real_gateway(), telemetry),
            AgentConfig(),
            telemetry,
        ),
        default_policy(settings.auto_threshold),
        telemetry=telemetry,
    )
    # a spread across difficulties and reason codes
    scenarios = sorted(DATASET.scenarios, key=lambda s: (s.difficulty, s.event["reason_code"]))
    picked = scenarios[:: max(1, len(scenarios) // count)][:count]
    for scenario in picked:
        event = SettlementFailureEvent.model_validate_json(
            json.dumps({**scenario.event, "event_id": str(uuid4())})
        )
        case, _ = await processor.intake(event)
        done = await processor.investigate(case.id)
        trade, code = scenario.event["trade_id"], scenario.event["reason_code"]
        print(f"{trade:>14} {code} -> {done.status.value}")
    await engine.dispose()
    telemetry.shutdown()  # flush spans so the demo cases have traces


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=12)
    asyncio.run(main(parser.parse_args().count))
