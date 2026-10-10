# CLAUDE.md — SettleAgent

> This file is the authoritative engineering guide for the SettleAgent repository.
> Claude Code (and any AI agent working in this repo) must read it fully before
> making changes, and keep it in sync when architecture or conventions change.
> **Build this as a production-grade system, not a prototype.** Every component
> ships with error handling, tests, observability, and documentation.

---

## 1. What this project is

**SettleAgent** is an event-driven, AI-powered system that automatically
investigates **securities settlement failures** and either proposes a remediation
(for human approval) or escalates ambiguous cases to a human operator.

When a trade fails to settle, the custodian/CSD emits a status message
(ISO 20022 `semt.018` / SWIFT MT548) carrying a failure reason code. Today an
operations analyst manually triages each one. SettleAgent automates that triage:
it consumes the failure event, investigates it by querying reference systems
through tools, produces a **structured, evidence-backed root cause with a
confidence score**, and routes the case by confidence.

### The one architectural principle that governs everything

> **Separate reasoning from control.** The LLM is powerful but non-deterministic
> and untrusted. Everything around it — schemas, deterministic routing, read-only
> tools, approval gates, audit trails, human escalation — exists to contain and
> verify it. No LLM output is ever acted on without passing a deterministic gate.

Every design decision in this repo should be justifiable against that principle.
It is also the project's thesis: *the hard part isn't generating output, it's
running it reliably and safely.*

> **Important:** This project runs entirely on **synthetic data** modeled on real
> ISO 20022 / MT548 standards. It never connects to a real trading or settlement
> system. This is a portfolio-grade reference implementation of a pattern, built to
> production standards.

---

## 2. How Claude Code should work in this repo

1. **Read before writing.** Load this file, the target module's existing code, and
   its tests before changing anything.
2. **Plan, then implement in vertical slices.** Prefer a thin end-to-end slice that
   works over a broad half-built layer. Follow the phased plan in §10.
3. **Tests are part of "done."** No feature is complete without unit tests, and no
   cross-component feature is complete without an integration test. Do not lower a
   coverage gate to make a build pass.
4. **Never weaken a safety control to make something pass.** Read-only-by-default
   tools, the approval gate on writes, confidence routing, and timeouts/step-budgets
   are load-bearing. If a test fights them, the test or the design is wrong — ask.
5. **Keep contracts explicit.** All data crossing a boundary (Kafka, MCP tool call,
   API) is a versioned, validated schema. Reject malformed data; never coerce
   silently.
6. **Determinism where it matters.** Routing, gates, and persistence must be
   deterministic and unit-testable without calling the LLM. The LLM is mocked in all
   tests except the eval harness.
7. **Observability is not optional.** Every run emits a trace; every external call is
   a span; every decision is logged with the case id.
8. **Update docs with code.** When you change a schema, command, or component, update
   this file and the README in the same change.
9. **Conventional commits.** `feat:`, `fix:`, `test:`, `refactor:`, `docs:`,
   `chore:`. One logical change per commit.
10. **Ask when a decision is irreversible or ambiguous** (data model changes,
    dependency additions, anything touching the safety controls).

---

## 3. System architecture

```
 ┌──────────────────┐   ISO 20022 / MT548    ┌──────────────────┐
 │  Event Producer  │  synthetic failures    │      Kafka       │
 │ (Spring Boot/Java)├───────────────────────▶│ settlement.fails │
 └──────────────────┘                         └────────┬─────────┘
                                                        │ consume
                                                        ▼
 ┌───────────────────────────────────────────────────────────────┐
 │                     Agent Core (Python)                         │
 │                                                                 │
 │   Consumer ─▶ LangGraph investigation graph ─▶ Structured       │
 │                   │  (Claude reasoning)          result         │
 │                   │                              (Pydantic)     │
 │          read-only│ MCP tool calls                 │            │
 │                   ▼                                ▼            │
 │        ┌──────────────────────┐          ┌──────────────────┐  │
 │        │  MCP Tool Servers     │          │ Confidence Gate   │  │
 │        │ • security-master     │          │ (deterministic)   │  │
 │        │ • ssi-store           │          └────────┬─────────┘  │
 │        │ • holiday-calendar    │         high │        │ low     │
 │        │ • trade-store         │              ▼        ▼         │
 │        └──────────────────────┘    Auto-resolve   Human ops     │
 │                                     (approval-      queue        │
 │                                      gated write)                │
 └───────────────┬──────────────────────────────┬──────────────────┘
                 │                                │
         ┌───────▼────────┐              ┌────────▼─────────┐
         │   FastAPI       │              │   Streamlit       │
         │ (control plane) │              │  ops dashboard    │
         └───────┬────────┘              └──────────────────┘
                 │
         ┌───────▼────────┐        ┌─────────────────────────┐
         │  PostgreSQL     │        │  OpenTelemetry → traces, │
         │ (cases, audit)  │        │  metrics, cost, latency  │
         └────────────────┘        └─────────────────────────┘
```

**Data flow (one case):**
1. Producer publishes a synthetic `SettlementFailureEvent` to Kafka.
2. Consumer pulls it, creates a `Case` row (status `RECEIVED`), starts a run.
3. LangGraph agent investigates: plans → calls MCP tools (read-only) → reasons →
   emits an `InvestigationResult` (root cause, evidence, proposed fix, confidence).
4. The **confidence gate** (deterministic) routes:
   - `confidence >= AUTO_THRESHOLD` → auto-resolution path → **approval-gated write**.
   - else → human ops queue (surfaced in Streamlit).
5. Every step is traced (OpenTelemetry) and persisted (audit trail).

**Why two language ecosystems:** the Spring Boot producer reflects a realistic
upstream (banks emit these messages from JVM systems) and keeps the Kafka contract
honest. The intelligence and control plane are Python. The agent **never** depends
on the producer's internals — only on the Kafka message contract (§6).

---

## 4. Tech stack (pin these versions in lockfiles)

| Layer | Choice | Notes |
|---|---|---|
| Agent orchestration | **LangGraph** | State machine for the investigation; deterministic graph, mockable nodes |
| LLM | **Claude API** (`anthropic` SDK) | Only called inside agent nodes and the eval harness |
| Tooling protocol | **MCP** (Model Context Protocol) | Each reference system is an MCP server; read-only by default |
| Data contracts | **Pydantic v2** | All schemas; strict mode; reject-on-extra |
| Control-plane API | **FastAPI** + Uvicorn | Health, cases, approvals, metrics |
| Dashboard | **Streamlit** | Ops queue, case detail, approve/reject |
| Event producer | **Java 21 + Spring Boot 3** | Synthetic event generation + Kafka producer |
| Messaging | **Apache Kafka** | `confluent-kafka` (Python consumer), Spring Kafka (producer) |
| Persistence | **PostgreSQL 16** | SQLAlchemy 2 + Alembic migrations |
| Observability | **OpenTelemetry** | OTLP exporter; traces + metrics; cost/latency/tool-call spans |
| Packaging | **uv** (Python), **Maven** (wrapper `./mvnw`) (Java) | Reproducible builds |
| Containerization | **Docker + Docker Compose** | Local full-stack; Render for deploy |
| Tests | **pytest**, **pytest-asyncio**, **testcontainers**, **JUnit 5** | See §9 |
| Lint/format | **ruff**, **mypy** (strict), **Spotless** (Java) | CI-enforced |

Python 3.12. Do not add dependencies without updating this table and the lockfile.

---

## 5. Repository structure

```
settleagent/
├── CLAUDE.md                     # this file
├── README.md
├── docker-compose.yml            # kafka, postgres, otel-collector, all services
├── Makefile                      # dev entrypoints (see §11)
├── .github/workflows/ci.yml      # lint, type, test, coverage gates
│
├── producer-service/             # Java / Spring Boot — synthetic event source
│   ├── pom.xml
│   └── src/main/java/.../producer/
│       ├── SettlementEventGenerator.java   # builds synthetic MT548/semt.018
│       ├── ReasonCodeCatalog.java
│       └── KafkaPublisher.java
│   └── src/test/java/...                    # JUnit tests
│
├── agent-core/                   # Python — the brain + control
│   ├── pyproject.toml
│   └── src/settleagent/
│       ├── domain/               # Pydantic schemas (§6) — no logic
│       │   ├── events.py         # SettlementFailureEvent
│       │   ├── investigation.py  # InvestigationResult, Evidence, ProposedFix
│       │   ├── case.py           # Case, CaseStatus, Decision
│       │   └── reason_codes.py   # reason code enum + metadata
│       ├── consumer/             # Kafka consumer, DLQ, idempotency
│       ├── agent/                # LangGraph graph
│       │   ├── state.py          # InvestigationState (graph state)
│       │   ├── nodes.py          # plan / gather / analyze / synthesize
│       │   ├── graph.py          # graph wiring + compiled app
│       │   └── prompts/          # versioned prompt templates
│       ├── tools/                # MCP client wrappers (read-only)
│       ├── gate/                 # confidence gate + routing (deterministic)
│       ├── remediation/          # approval-gated write service
│       ├── persistence/          # SQLAlchemy models, repositories, migrations
│       ├── observability/        # OTel setup, cost tracking, structured logging
│       ├── api/                  # FastAPI app
│       ├── config.py             # pydantic-settings, 12-factor config
│       └── main.py               # wires consumer + graph + services
│   └── tests/                    # unit + integration (§9)
│
├── mcp-servers/                  # Python — four read-only MCP tool servers
│   ├── security_master/
│   ├── ssi_store/
│   ├── holiday_calendar/
│   └── trade_store/
│   └── _shared/                  # fixtures loader, synthetic data generators
│
├── dashboard/                    # Streamlit ops UI
│
├── evals/                        # the 300-case evaluation harness (§9.4)
│   ├── dataset/                  # 300 labelled synthetic cases (versioned)
│   ├── runner.py
│   └── report.py
│
└── data/synthetic/               # generated reference datasets (seeded, reproducible)
```

---

## 6. Domain model & data contracts

All schemas are **Pydantic v2, strict, `extra="forbid"`, frozen where possible**,
and **versioned** (a `schema_version` field). These are the contracts; treat them
as public API.

### 6.1 Kafka message — `SettlementFailureEvent`

```python
class SettlementFailureEvent(BaseModel):
    schema_version: Literal["1.0"]
    event_id: UUID                    # idempotency key
    emitted_at: datetime              # UTC, tz-aware
    trade_id: str
    isin: str                         # instrument identifier
    settlement_date: date
    counterparty_bic: str
    reason_code: ReasonCode           # enum, see 6.3
    quantity: Decimal
    cash_amount: Decimal
    currency: str                     # ISO 4217
    raw_message: str                  # original MT548/semt.018 payload (for audit)
```

Serialized as JSON on topic `settlement.failures`. Key = `trade_id` (ordering per
trade). A schema registry is optional for the portfolio build; validation on consume
is mandatory.

### 6.2 Investigation output — `InvestigationResult`

```python
class Evidence(BaseModel):
    source: ToolName                  # which MCP tool produced it
    statement: str                    # what was found
    raw: dict                         # tool response excerpt (audit)

class ProposedFix(BaseModel):
    action: FixAction                 # enum: AMEND_SSI, RESUBMIT, PARTIAL_SETTLE, ...
    parameters: dict
    requires_write: bool              # if True, must pass approval gate

class InvestigationResult(BaseModel):
    schema_version: Literal["1.0"]
    case_id: UUID
    root_cause: str
    reason_category: ReasonCategory   # derived, deterministic enum
    evidence: list[Evidence]          # non-empty for any non-UNKNOWN cause
    proposed_fix: ProposedFix | None
    confidence: float = Field(ge=0.0, le=1.0)
    model: str                        # model id used
    tokens_used: int
    reasoning_trace_id: str           # ties to OTel trace
```

**Validation rules the agent node must satisfy (enforced in code, not prompt):**
- `confidence < 0.2` ⇒ `root_cause` may be "undetermined" and `proposed_fix` must be
  `None`.
- A non-"undetermined" `root_cause` requires at least one `Evidence`.
- `proposed_fix.requires_write == True` can never auto-execute without an approval.

**Implementation notes (Phase 1):**
- "Undetermined" is expressed deterministically as `reason_category == UNDETERMINED`
  *and* `root_cause == "undetermined"`; the validator requires the two to agree, and an
  undetermined result can never carry a `proposed_fix`.
- `isin` is validated by pattern and Luhn check digit; `emitted_at` must be tz-aware.
- Models are strict: Python callers must pass real types (`UUID`, `Decimal`); wire JSON is
  parsed with `model_validate_json`. Rows rehydrated from the DB use lenient mode (our own
  data) but still forbid extra fields.

### 6.3 Reason codes

Seed `ReasonCode` with a representative enum mapped to ISO 20022 / MT548 semantics.
**These must be reconciled against the official ISO 20022 external code lists**
(`FailingReason1Code` / `PendingReason*Code` used in `semt.018` / MT548). Seed set:

| Code | Meaning | Typical true cause (for eval labels) |
|---|---|---|
| `LACK` | Insufficient securities | short inventory / failed upstream receive |
| `MONY` | Insufficient cash | funding shortfall |
| `NMAT` | Unmatched / mismatch | economic detail mismatch vs counterparty |
| `CMIS` | Counterparty instruction missing | counterparty hasn't instructed |
| `SSIM` | SSI mismatch | wrong/stale standing settlement instruction |
| `DENO` | Denomination/quantity issue | lot-size / denomination break |
| `LATE` | Late instruction | instructed after cut-off |
| `CALD` | Settlement-date on market holiday | holiday/calendar error |

Each code maps to a `ReasonCategory` and a documented investigation playbook (which
tools to consult). Keep `reason_codes.py` as the single source of truth.

---

## 7. Component specifications

### 7.1 Event producer (Spring Boot)
- Generates synthetic, internally-consistent failure events (references must resolve
  in the MCP servers' datasets — see §8 shared generators).
- Modes: **replay** (from a fixtures file, deterministic — used by integration tests
  and the eval harness) and **stream** (randomized rate, for demo).
- Publishes valid JSON matching `SettlementFailureEvent` v1.0. A contract test
  asserts the Java-produced JSON deserializes cleanly into the Python schema.
- Production concerns: ret/backoff on publish, graceful shutdown, health endpoint.

### 7.2 Kafka
- Topics: `settlement.failures` (main), `settlement.failures.dlq` (dead-letter).
- Consumer: manual commit **after** the case is durably persisted (at-least-once).
- Idempotency: dedupe on `event_id` via a unique constraint; re-delivered events are
  no-ops.
- Poison messages (schema-invalid) → DLQ with the validation error attached.

### 7.3 MCP tool servers (read-only)
Four servers, each exposing read-only query tools over its synthetic dataset:
- `security-master`: `get_instrument(isin)` → static data, lot size, status.
- `ssi-store`: `get_ssi(bic, currency)` / `get_ssi_for_trade(trade_id)`.
- `holiday-calendar`: `is_business_day(market, date)`, `next_business_day(...)`.
- `trade-store`: `get_trade(trade_id)`, `get_counterparty_instruction(trade_id)`.

**Implementation notes (Phase 2):**
- Built on the official `mcp` SDK **v2** (`MCPServer`, not v1 `FastMCP`). Each server is a
  pure, deterministic `service.py` over the `Dataset` plus a thin `server.py` wrapper;
  tools declare read-only/idempotent annotations and typed output schemas.
- Errors are a JSON `{code, message}` payload with `code` in `NOT_FOUND | INVALID_INPUT`
  (tool-level `isError`). Dates are ISO strings validated by us, so malformed input gets
  the typed error rather than an SDK validation error.
- `ssi_id` is deliberately absent from `get_trade`; SSI data lives only in `ssi-store`.
- `make seed` writes `data/synthetic/dataset.json` (seed 42, 300 scenarios: 180 clear /
  75 ambiguous / 45 underdetermined, every reason code in every tier). Each scenario
  carries ground truth for the eval harness. A test fails if the committed file drifts
  from the generator, and an agent-core test validates every event against
  `SettlementFailureEvent`.
- *Ambiguous* = two concurrent faults (the reported one plus a second); *underdetermined*
  = no inconsistency, or the trade is absent from the trade store.

Rules:
- **No write tools exist on these servers.** Writes live only in the remediation
  service (§7.6).
- Each tool call is an OTel span with inputs/outputs recorded (audit).
- Each server has **contract tests** asserting its tool schemas and error behavior.
- Deterministic: same dataset + same query ⇒ same result (critical for evals).

### 7.4 The investigation agent (LangGraph)

`InvestigationState` (graph state) carries: the event, accumulated evidence, tool
calls made, step count, and the working hypothesis.

Nodes (each small, single-purpose, independently testable with a mocked LLM):
1. `plan` — from the reason code + event, decide which tools to consult (seeded by
   the reason-code playbook; the LLM refines).
2. `gather` — execute the planned read-only tool calls; attach `Evidence`.
3. `analyze` — reason over evidence; may loop back to `gather` (bounded).
4. `synthesize` — emit a validated `InvestigationResult` with confidence.

**Implementation notes (Phase 3):**
- *Step accounting:* one step per tool call and per LLM call (plan refinement, analyze,
  synthesize, repair). Tool calls stop `TOOL_RESERVE=2` steps before `MAX_STEPS` so analyze
  and synthesize can still run; if synthesis cannot run, a deterministic `UNDETERMINED`,
  confidence-0 result (evidence preserved for the operator) is emitted -> human queue.
- *Closed tool catalog:* the LLM may only add calls from `agent/playbook.ALLOWED_CALLS`
  (read-only); anything else is ignored. Calendar calls take their `market` from earlier
  evidence (`$market` placeholder), so they run last.
- *The model proposes, code disposes* (`agent/result_builder.py`): `requires_write` is derived
  from the action (`ACTION_REQUIRES_WRITE`), never from the model; `ProposedFix` rejects a
  write action declared read-only; fixes are dropped for undetermined / confidence < 0.2;
  confidence is capped at `DEGRADED_CONFIDENCE_CAP` (0.6) when any tool evidence is missing;
  evidence is attached by reference from tool output, so the model cannot fabricate it.
  Invalid output gets one repair attempt, then escalates.
- *Tool gateway* (`tools/gateway.py`) never raises: it returns a `ToolOutcome`. NOT_FOUND is
  evidence (a finding); timeouts/connection errors are retried with exponential backoff,
  trip a per-server circuit breaker, and are recorded as *missing* evidence.
- *Claude API:* structured outputs (`output_config.format` JSON schema) with explicit
  `effort`; no forced `tool_choice` (rejected by current models). Default model
  `claude-opus-5-5` (`ANTHROPIC_MODEL`). `refusal` and truncation are handled as escalations.
- *Locking:* the case row is always locked first (`SELECT ... FOR UPDATE`), then child rows
  (a deadlock found by the concurrency test); a second concurrent run bows out.
- *Delivery:* offset committed after the case is persisted; investigations are dispatched
  with a concurrency bound (backpressure) and non-terminal cases are recovered at startup.
  Poison -> DLQ (error in headers); repeated transient failure -> DLQ after
  `max_attempts`; if the DLQ write fails nothing is committed.
- *Phase-3 outcome mapping:* HUMAN_REVIEW -> `ESCALATED`; AUTO_RESOLVE with a write fix ->
  `AWAITING_APPROVAL` + a PENDING approval (executing it is Phase 4); AUTO_RESOLVE with a
  read-only fix -> `AUTO_RESOLVED`. A write fix can never reach `AUTO_RESOLVED`.

Control rules baked into the graph (not the prompt):
- **Step budget:** hard cap on node iterations / tool calls (config
  `MAX_STEPS`, default 12). On exceed → finalize as low-confidence → human queue.
- **Tool timeout & retry:** each tool call has a timeout and bounded retry; repeated
  failure → degrade gracefully (record missing evidence), never crash the run.
- **Structured output enforced:** `synthesize` must return schema-valid output;
  on invalid LLM output, one repair attempt, else low-confidence escalation.
- The graph is **compiled once** and reused; runs are isolated by `case_id`.

### 7.5 Confidence gate & routing (deterministic — no LLM)
Pure function `route(result, policy) -> Decision`:
- `confidence >= policy.auto_threshold` **and** `proposed_fix is not None` **and**
  fix is in the auto-eligible set → `AUTO_RESOLVE`.
- else → `HUMAN_REVIEW`.
Thresholds come from config/policy, are versioned, and are chosen from the eval
harness's precision/recall curve (§9.4). This module has exhaustive unit tests.

### 7.6 Remediation / write service (approval-gated)
- The **only** place that performs state-changing actions (against synthetic systems).
- A proposed fix with `requires_write=True` creates an `Approval` (status `PENDING`).
- Execution happens **only** after explicit approval (via API/dashboard), is
  idempotent, fully audited, and reversible where possible.
- Auto-resolve for `requires_write=False` fixes (e.g. "no action — holiday, will
  settle next business day") may complete without approval but is still audited.

### 7.7 FastAPI control plane
Endpoints (all documented via OpenAPI):
- `GET /health`, `GET /ready` (liveness/readiness: Kafka, DB, OTel).
- `GET /cases`, `GET /cases/{id}` (with full evidence + audit trail).
- `GET /approvals`, `POST /approvals/{id}/approve`, `POST /approvals/{id}/reject`.
- `GET /metrics/summary` (auto-resolve rate, queue depth, avg confidence, cost).
Auth: API key middleware (even for the portfolio build — demonstrate the control).

### 7.8 Streamlit dashboard
- Ops queue (pending human-review cases, sortable by age/confidence).
- Case detail: event, agent reasoning, evidence, proposed fix, trace link.
- Approve / reject actions (call the API; never touch the DB directly).
- Metrics tab mirroring `/metrics/summary`.

### 7.9 Persistence (PostgreSQL)
Core tables (SQLAlchemy + Alembic):
- `cases` (id, event_id unique, status, created/updated, result JSONB, trace_id).
- `audit_events` (append-only: case_id, actor [`agent`|`system`|user], action,
  payload, ts). **Immutable** — no updates/deletes.
- `approvals` (id, case_id, status, proposed_fix, decided_by, decided_at).
- `tool_calls` (case_id, tool, request, response, latency_ms, ts).
Persistence notes: `approvals.case_id` is unique (one approval per case, which makes
creation idempotent); `audit_events` is made immutable by DB triggers that reject
UPDATE, DELETE and TRUNCATE (tested); status moves are validated against
`domain.case.ALLOWED_TRANSITIONS` under a row lock. Repositories take an injected `now`
and never read the clock; callers own the transaction.
`CaseStatus`: `RECEIVED → INVESTIGATING → (AUTO_RESOLVED | AWAITING_APPROVAL →
RESOLVED | ESCALATED) | FAILED`.

### 7.10 Observability
- One **root span per case**; child spans for each node and each tool call.
- Span attributes: `case_id`, `reason_code`, `confidence`, `tokens`, `cost_usd`,
  `decision`.
- Metrics: cases processed, auto-resolve rate, queue depth, p50/p95 run latency,
  tool error rate, tokens & cost per case.
- **Cost tracking**: compute per-run USD from token usage; expose and alert on budget.
- Structured JSON logs, always including `case_id`.

---

## 8. Production-readiness requirements (apply everywhere)

- **Config:** 12-factor via `pydantic-settings`; no secrets in code; `.env.example`
  documents every variable. Fail fast on missing/invalid config at startup.
- **Resilience:** timeouts on every I/O call; bounded retries with backoff; circuit
  breaker around MCP tools; DLQ for poison messages; graceful shutdown (drain
  in-flight case, commit offsets).
- **Idempotency:** every externally-triggered action is idempotent (event_id,
  approval id).
- **Determinism/testability:** no business logic depends on wall-clock or randomness
  without an injectable clock/seed.
- **Security:** API-key auth; input validation at every boundary; no PII (synthetic
  only); dependency scanning in CI.
- **Health & readiness:** real checks (DB, Kafka, OTel reachable).
- **Backpressure & limits:** bound concurrent in-flight cases; rate-limit LLM calls;
  enforce the per-case step budget and a global cost ceiling.
- **Error taxonomy:** distinguish transient (retry) from permanent (DLQ/escalate)
  failures explicitly.

---

## 9. Testing strategy

**Coverage gates (CI-enforced):** ≥ 90% line coverage on `gate/`, `domain/`,
`remediation/`, `consumer/` (the safety-critical modules); ≥ 80% overall. The LLM is
**mocked in all tests except the eval harness.**

### 9.1 Unit tests (fast, no I/O)
- **Domain/schemas:** valid/invalid payloads; the `InvestigationResult` validation
  rules (confidence↔evidence↔fix invariants); schema-version handling.
- **Confidence gate:** exhaustive routing table — boundaries (`== threshold`), fix
  present/absent, auto-eligible/ineligible, low confidence, missing fix. Property
  test: output is always a valid `Decision`.
- **Reason-code catalog:** every code maps to a category and a playbook.
- **Agent nodes (LLM mocked):** `plan` produces the expected tool set per reason
  code; `gather` attaches evidence and respects tool failures; `analyze` honors the
  step budget; `synthesize` rejects malformed LLM output and repairs/escalates.
- **Remediation:** write requires an approval; execution is idempotent; no-approval
  path for `requires_write=False`; audit row written on every action.
- **Cost/metrics math:** token→USD, aggregation correctness.

### 9.2 Integration tests (testcontainers: Kafka + Postgres)
- **End-to-end happy path:** publish event → case persisted → investigated (mocked
  LLM returns canned results) → routed → correct terminal status + audit trail.
- **High-confidence auto-resolve** with a `requires_write=False` fix completes.
- **High-confidence write fix** parks in `AWAITING_APPROVAL`; approve → `RESOLVED`;
  reject → `ESCALATED`.
- **Low-confidence** → human queue, no write.
- **Idempotency:** duplicate `event_id` is a no-op (one case).
- **Poison message** → DLQ with validation error; pipeline keeps running.
- **Tool outage:** MCP server down → bounded retries → graceful degradation (missing
  evidence, lower confidence), run still finalizes.
- **Cross-language contract test:** Spring Boot-produced JSON deserializes into the
  Python `SettlementFailureEvent` (run in CI).

### 9.3 Component/contract tests
- Each MCP server: tool schema, happy path, not-found, malformed input → typed error.
- FastAPI: each endpoint's success + auth-failure + not-found; OpenAPI schema snapshot.

### 9.4 The evaluation harness (`evals/`) — the headline artifact
- **Dataset:** 300 labelled synthetic cases, versioned, covering every reason code and
  a realistic difficulty mix (clear / ambiguous / underdetermined), each with a known
  correct root cause and whether it is safely auto-resolvable.
- **Runner:** executes the **real agent** (real Claude calls) over the dataset through
  the real MCP servers; records per-case predicted root cause, confidence, decision,
  cost, latency, trace id.
- **Metrics:** root-cause accuracy; **auto-resolution precision & recall as a function
  of the confidence threshold** (the precision/recall curve that *sets* the production
  threshold); escalation rate; cost/latency percentiles; a confusion matrix over
  reason categories.
- **Gate (for the harness, not the LLM):** a regression guard — accuracy and
  auto-resolve precision at the chosen threshold must not drop below committed
  baselines. Prompt or model changes that regress these fail.
- **Report:** `evals/report.py` emits a markdown/HTML scorecard (committed to the
  repo as evidence). This is the thing that proves the system *works*, and the single
  best artifact to show in an interview.

### 9.5 Load/smoke (lightweight)
- A smoke test that streams N events and asserts throughput, bounded queue growth, no
  unhandled errors, and that the cost ceiling is respected.

---

## 10. Implementation plan (phased; each phase ends green & demoable)

> Build vertically. After Phase 3 there is a working end-to-end slice; later phases
> harden and broaden it. Each phase: code + tests + docs, CI green.

- **Phase 0 — Scaffolding.** Monorepo layout, `uv`/Maven projects, Makefile,
  docker-compose (Kafka, Postgres, OTel collector), CI skeleton (lint/type/test
  gates), `.env.example`. *Done when* `make up && make test` runs green on an empty
  pipeline.
- **Phase 1 — Domain & persistence.** All Pydantic schemas (§6), reason-code catalog,
  SQLAlchemy models + first Alembic migration, repositories. Full unit tests.
- **Phase 2 — Synthetic data & MCP servers.** Shared generators producing
  internally-consistent datasets; the four read-only MCP servers + contract tests.
- **Phase 3 — Agent + consumer + gate (the vertical slice).** Kafka consumer with
  idempotency/DLQ; LangGraph graph (nodes, step budget, structured output); confidence
  gate. Integration test: event in → routed decision out (LLM mocked). **Demoable.**
- **Phase 4 — Remediation & approvals.** Approval-gated write service, audit trail,
  auto/human paths. Integration tests for approve/reject/escalate.
- **Phase 5 — API & dashboard.** FastAPI control plane + Streamlit ops UI wired to it.
- **Phase 6 — Observability.** OTel traces/metrics, cost tracking, structured logging,
  health/readiness.
- **Phase 7 — Eval harness.** 300-case dataset, runner, metrics, scorecard; choose the
  production confidence threshold from the curve; commit the baseline + report.
- **Phase 8 — Hardening & deploy.** Resilience pass (timeouts/circuit breakers/
  backpressure/cost ceiling), load/smoke test, Render deployment config, README polish.

---

## 11. Commands (wire these in the Makefile)

```
make up            # docker-compose up: kafka, postgres, otel-collector
make down          # tear down
make install       # uv sync (python) + maven package (java)
make migrate       # alembic upgrade head
make run           # start consumer+agent, api, dashboard, producer (stream mode)
make test          # unit + integration (testcontainers), coverage gates
make test-unit     # fast unit tests only
make lint          # ruff + mypy --strict + spotless
make eval          # run the 300-case eval harness, emit scorecard
make seed          # regenerate synthetic reference datasets (seeded)
```

Every command must work from a clean checkout after `make install && make up`.

---

## 12. Coding standards

- **Python:** 3.12, full type hints, `mypy --strict` clean, `ruff` clean. Small pure
  functions; dependency-inject clocks, randomness, LLM client, and tool clients so
  everything is testable. No business logic in `domain/` (schemas only).
- **Async:** the agent, consumer, and API are async; never block the event loop.
- **Errors:** typed exceptions; never swallow; always log with `case_id`.
- **Java:** Java 21, Spring Boot 3, Spotless-formatted, JUnit 5, constructor injection.
- **Secrets/config:** only via `pydantic-settings` / Spring config; nothing hardcoded.
- **Docs:** public functions and all schemas carry docstrings; update README + this
  file when contracts, commands, or architecture change.
- **Git:** conventional commits; PR-sized logical changes; never commit secrets or the
  real (there is none) data — synthetic only.

---

## 13. Definition of done (per feature)

1. Code implemented to the spec above.
2. Unit tests written; integration test if it crosses a boundary; coverage gates met.
3. `make lint` and `make test` green locally and in CI.
4. Observability added (spans/metrics/logs where relevant).
5. Safety controls intact (read-only tools, approval gate, routing, budgets).
6. README and this file updated if anything public changed.
7. Conventional-commit message describing the change.
