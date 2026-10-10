# SettleAgent

**An AI agent that investigates securities settlement failures — safely.**

When a trade fails to settle, a custodian emits a status message (ISO 20022
`semt.018` / SWIFT MT548) with a failure reason code. Operations teams triage these
by hand. SettleAgent automates that triage: it consumes the failure event,
investigates it by querying reference systems, and produces a **structured,
evidence-backed root cause with a confidence score** — then auto-resolves the clear
cases (with approval-gated writes) and escalates the ambiguous ones to a human.

> **Built to a single principle:** *separate reasoning from control.* The LLM does
> the reasoning; deterministic schemas, gates, read-only tools, approval steps, and
> audit trails do the controlling. No model output is ever acted on without passing a
> deterministic gate. The hard part isn't generating output — it's running it
> reliably and safely.

> ⚠️ **Synthetic data only.** SettleAgent runs entirely on synthetic data modeled on
> real ISO 20022 / MT548 standards. It does not connect to any real trading or
> settlement system. It is a production-grade reference implementation of a pattern.

---

## Architecture

```
Spring Boot producer ──▶ Kafka ──▶ Consumer ──▶ LangGraph agent ──▶ Structured result
 (synthetic MT548)      (failures)              (Claude + MCP tools)   (root cause,
                                                      │                 evidence,
                                     read-only MCP ───┤                 confidence)
                                     • security master│                     │
                                     • SSI store       │              Confidence gate
                                     • holiday calendar │             (deterministic)
                                     • trade store      │           high │     │ low
                                                        ▼                ▼     ▼
                                              OpenTelemetry      Auto-resolve  Human
                                              traces/metrics     (approval-    ops
                                                                  gated)      queue
      FastAPI control plane  ·  Streamlit ops dashboard  ·  PostgreSQL (cases + audit)
```

The agent investigates read-only; the **only** place that performs state-changing
actions is an approval-gated remediation service, and every action is audited.

---

## Key features

- **Event-driven** — Kafka pipeline with idempotent consumption and a dead-letter
  queue for poison messages.
- **Agentic investigation** — a LangGraph state machine where Claude plans, gathers
  evidence from MCP tool servers, reasons, and synthesizes a result — under a hard
  step budget and tool timeouts.
- **Structured & validated** — every result is a Pydantic-validated object (root
  cause, evidence trail, proposed fix, confidence); malformed LLM output is rejected,
  not acted on.
- **Governance-first** — read-only tools by default, approval-gated writes,
  deterministic confidence routing, and an immutable audit trail of every decision.
- **Measured** — a 300-case labelled evaluation harness measures root-cause accuracy
  and auto-resolution precision across confidence thresholds; the production threshold
  is chosen from that curve.
- **Observable** — OpenTelemetry traces every case and tool call; per-case cost and
  latency are tracked and budgeted.

---

## Tech stack

Python 3.12 · LangGraph · Claude API · MCP · FastAPI · Pydantic v2 · Streamlit ·
Java 21 / Spring Boot 3 · Apache Kafka · PostgreSQL 16 · SQLAlchemy + Alembic ·
OpenTelemetry · Docker Compose · pytest / testcontainers / JUnit 5.

---

## Quickstart

```bash
# prerequisites: Docker, uv, JDK 21
make install        # install python + java deps
make up             # start kafka, postgres, otel collector
make migrate        # apply database migrations
make seed           # generate synthetic reference datasets
make run            # start producer, agent, API, dashboard
```

Then open the Streamlit ops dashboard (default `http://localhost:8501`) and the
FastAPI docs (`http://localhost:8000/docs`).

```bash
make test           # unit + integration tests with coverage gates
make eval           # run the 300-case evaluation harness and emit a scorecard
```

---

## How it works (one case)

1. The producer publishes a synthetic `SettlementFailureEvent` to Kafka.
2. The consumer persists a case and starts an investigation run.
3. The LangGraph agent plans which reference systems to check (seeded by the reason
   code), queries them read-only via MCP, reasons over the evidence, and emits a
   validated `InvestigationResult` with a confidence score.
4. A deterministic **confidence gate** routes the case: high-confidence with an
   eligible fix → auto-resolution (writes require approval); otherwise → the human
   ops queue.
5. Everything is traced and written to an immutable audit trail.

---

## Evaluation

The `evals/` harness runs the real agent over 300 labelled cases and reports
root-cause accuracy, a precision/recall curve for auto-resolution across confidence
thresholds, an escalation rate, and cost/latency percentiles. The committed scorecard
is the evidence that the system behaves safely — and the chosen confidence threshold
comes directly from the precision/recall curve, not a guess.

---

## Project layout

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture, component specifications,
data contracts, testing strategy, and the phased implementation plan.

```
producer-service/   Java/Spring Boot synthetic event source
agent-core/         Python: domain, consumer, agent, gate, remediation, api, persistence
mcp-servers/        four read-only MCP tool servers
dashboard/          Streamlit ops UI
evals/              300-case evaluation harness + scorecard
```

---

## Status & roadmap

Built in vertical slices: domain & persistence → synthetic data & MCP servers →
agent + consumer + gate (working end-to-end slice) → remediation & approvals → API &
dashboard → observability → evaluation harness → hardening & deploy. See the phased
plan in `CLAUDE.md`.

## License

For portfolio and educational use.
