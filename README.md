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
Java 21 / Spring Boot 4 (Maven) · Apache Kafka · PostgreSQL 16 · SQLAlchemy + Alembic ·
OpenTelemetry · Docker Compose · pytest / testcontainers / JUnit 5.

---

## Quickstart

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), JDK 21.

```bash
make install        # python + java deps
make up             # kafka, postgres, otel collector, jaeger  (+ creates the topics)
make migrate        # apply database migrations
make seed           # generate the seeded synthetic reference data
make run            # MCP tool servers + agent + API + dashboard
```

Then open the Streamlit ops dashboard (`http://localhost:8501`), the FastAPI docs
(`http://localhost:8000/docs`), and Jaeger (`http://localhost:16686`; the dashboard links each
case to its trace). Metrics are scrapeable at `http://localhost:8889/metrics`.

**Feed it events**

```bash
make replay                       # Python stand-in producer: 20 seeded failures
cd producer-service && PRODUCER_MODE=replay PRODUCER_LIMIT=20 ./mvnw spring-boot:run   # the Java producer
make demo-data                    # seed demo cases WITHOUT calling Claude (oracle; demo only)
```

**Or run the whole system in containers**

```bash
echo "API_KEY=$(openssl rand -hex 16)" >> .env       # + ANTHROPIC_API_KEY=... for real investigations
make up-app                                           # builds and starts everything
docker compose --profile app run --rm producer        # replay the seeded failures
make smoke SMOKE_COUNT=30                             # publish, wait, and report throughput
```

Without an `ANTHROPIC_API_KEY` the pipeline still runs end to end — the agent degrades gracefully
and escalates every case to a human — which is a useful wiring check but says nothing about
model quality.

**Test and evaluate**

```bash
make lint && make test        # ruff, mypy --strict, Spotless; unit + integration (testcontainers) + JUnit
make eval-estimate            # free: price an evaluation run before spending anything
make eval-simulate            # free: exercise the harness end to end with a SIMULATED model
make eval EVAL_MAX_COST=25    # REAL run: 300 cases, hard-capped spend (see evals/README.md)
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

## Safety controls

The design principle is *separate reasoning from control*. The model proposes; code disposes.

| Control | What it guarantees |
|---|---|
| Read-only tools | The agent can only *read* reference data; there are no write tools on the MCP servers. |
| Deterministic confidence gate | Routing is a pure function of the validated result — no model output is acted on without it. |
| Evidence-verified fixes | A write is re-derived from the case's own tool evidence (right trade, supported values); a model-invented SSI, quantity or date is escalated, never offered. |
| Approval gate | A state-changing fix runs only after an explicit human approval; execution is idempotent, audited and reversible. |
| Structured outputs + repair | Malformed model output gets one repair attempt, then escalates to a human. |
| Step budget, timeouts, circuit breakers | A run cannot loop, hang, or hammer a failing tool server. |
| **Hard cost ceiling** | Once spend reaches `COST_CEILING_USD`, new cases skip the LLM and go to a human (with an audit reason). Alerts at 80 % and 100 %. |
| LLM rate/concurrency limits | Bounded in-flight cases, a concurrency cap and a token-bucket rate on model calls. |
| Idempotency + DLQ | Redelivered events are no-ops; poison messages are dead-lettered with the validation error. |
| Immutable audit trail | `audit_events` and the settlement ledger are append-only, enforced by database triggers. |
| Graceful shutdown | In-flight cases drain up to `SHUTDOWN_GRACE_S`; anything cancelled is recovered on the next start. |

---

## Evaluation

The `evals/` harness runs the real agent over 300 labelled cases and reports root-cause
accuracy, a precision/recall curve for auto-resolution across confidence thresholds, an escalation
rate, and cost/latency percentiles. The production confidence threshold is chosen *from that
curve*, and a committed baseline guards against regressions.

> **Status: not yet run against Claude.** The harness is built and tested, and a full run is
> estimated at roughly $8–35 on Opus 5.5 (`make eval-estimate`). Until it has been run,
> `AUTO_THRESHOLD=0.9` is a placeholder and **no accuracy claim is made for the model**.

---

## Event contract (Java ↔ Python)

The Kafka message `SettlementFailureEvent` v1.0 is defined once, by the Pydantic model; its JSON
Schema is committed in [`contracts/`](./contracts). The Java producer validates its output against
that schema, and a golden file of Java-produced events is parsed by the real strict Python model in
CI — so drift in either language fails a test. After a deliberate change: `make contract-update`.

---

## Deployment

* **Local:** `make up-app` (above). Images: `agent-core` (agent + API), `mcp-servers`,
  `dashboard`, `producer-service` — each has a `Dockerfile`; CI builds them all.
* **Render:** [`render.yaml`](./render.yaml) is a Blueprint for Postgres, the four private MCP
  servers, the agent worker, API, dashboard and an optional demo producer. Render has no managed
  Kafka, so you supply one (Confluent Cloud, Upstash, …) via the `KAFKA_*` variables, which support
  SASL. Hosted defaults keep `COST_CEILING_USD` low. *The blueprint is checked statically by tests
  but has not been deployed from this repository.*

---

## Project layout

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture, component specifications,
data contracts, testing strategy, and the phased implementation plan.

```
producer-service/   Java 21 / Spring Boot 4 synthetic event source (replay + stream)
agent-core/         Python: domain, consumer, agent, gate, remediation, api, persistence, observability
mcp-servers/        four read-only MCP tool servers + the seeded synthetic data generator
dashboard/          Streamlit ops UI (talks to the API only)
evals/              300-case evaluation harness, regression guard, scorecards
contracts/          the Kafka message contract (JSON Schema + Java golden file)
data/synthetic/     seeded reference data and labelled scenarios
render.yaml         Render Blueprint            docker-compose.yml   local infra + `app` profile
```

---

## Status

All eight phases are implemented and tested (domain & persistence → synthetic data & MCP servers →
agent/consumer/gate → remediation & approvals → API & dashboard → observability → evaluation
harness → hardening & deploy); see the phased plan in `CLAUDE.md`.

**Verified:** unit, integration (real Kafka and Postgres via testcontainers), JUnit (including an
embedded-Kafka round trip), a load/smoke test, and the Java↔Python contract. **Not verified:**
behaviour of the real Claude model (see *Evaluation*) and a Render deployment.

## License

For portfolio and educational use.
