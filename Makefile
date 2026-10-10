.PHONY: up up-app down install migrate run replay demo-data test test-unit lint seed \
	docker-build smoke contract-update \
	eval eval-dataset eval-estimate eval-sample eval-simulate eval-report eval-baseline eval-publish eval-check

PY_PROJECTS := agent-core mcp-servers dashboard evals
MVN := ./mvnw

up:
	docker compose up -d --wait
	@for t in settlement.failures settlement.failures.dlq; do \
		docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
			--create --if-not-exists --topic $$t --partitions 1 --replication-factor 1 >/dev/null; \
	done

down:
	docker compose --profile app down

up-app:              ## the whole system in containers (needs API_KEY in .env or the environment)
	docker compose --profile app up -d --build --wait --wait-timeout 300

docker-build:        ## build the four application images (also what CI does)
	docker build -f agent-core/Dockerfile -t settleagent/agent-core .
	docker build -f mcp-servers/Dockerfile -t settleagent/mcp-servers .
	docker build -f dashboard/Dockerfile -t settleagent/dashboard .
	docker build -f producer-service/Dockerfile -t settleagent/producer .

smoke:               ## publish N events to the RUNNING stack and check they settle (needs API_KEY)
	cd agent-core && uv run python scripts/smoke.py --count $(or $(SMOKE_COUNT),30) --api-key "$$API_KEY"

contract-update:     ## regenerate the Kafka contract artefacts after a deliberate model change
	cd agent-core && UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/test_contract_schema.py -q
	cd producer-service && ./mvnw -q test -Dtest=EventContractTest -Dupdate.golden=true
	cd agent-core && uv run pytest tests/unit/test_contract_schema.py tests/unit/test_java_contract.py -q

install:
	@for p in $(PY_PROJECTS); do (cd $$p && uv sync) || exit 1; done
	cd producer-service && $(MVN) -q -DskipTests package

migrate:
	cd agent-core && uv run alembic upgrade head

run:
	./scripts/run.sh

demo-data:
	cd agent-core && uv run python scripts/demo_data.py --count 12

replay:
	cd agent-core && uv run python -m settleagent.replay.producer --limit 20

test-unit:
	@for p in $(PY_PROJECTS); do (cd $$p && uv run pytest -m "not integration" -q) || exit 1; done

test:
	cd agent-core && uv run pytest --cov --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=80 -q
	python3 scripts/check_coverage.py agent-core/coverage.json --min 90
	cd mcp-servers && uv run pytest --cov --cov-report=term-missing -q
	cd dashboard && uv run pytest --cov --cov-report=term-missing -q
	cd evals && uv run pytest --cov --cov-report=term-missing -q
	cd producer-service && $(MVN) -q verify

lint:
	@for p in $(PY_PROJECTS); do (cd $$p && uv run ruff check . && uv run ruff format --check . && uv run mypy .) || exit 1; done
	cd producer-service && $(MVN) -q spotless:check

# ---- evaluation harness (see evals/README.md) -------------------------------------------
# `make eval` spends REAL money: it needs ANTHROPIC_API_KEY and an explicit cap, e.g.
#     make eval EVAL_MAX_COST=25
EVAL_RUN ?= $(shell ls -t evals/runs 2>/dev/null | head -1)

eval-dataset:        ## regenerate evals/dataset from the seeded generator
	cd evals && uv run python runner.py dataset

eval-estimate:       ## price a full run BEFORE spending anything (free)
	cd evals && uv run python runner.py estimate

eval-simulate:       ## free end-to-end check of the harness with a SIMULATED model
	cd evals && uv run python runner.py run --simulate --run-id sim-latest

eval-require-cap:
	@test -n "$(EVAL_MAX_COST)" || { \
		echo "A real eval spends real money. Run 'make eval-estimate' first, then e.g."; \
		echo "    make eval-sample EVAL_MAX_COST=3     # measure real cost on 10 cases"; \
		echo "    make eval EVAL_MAX_COST=25           # the full run, hard-capped at \$$25"; \
		exit 1; }

eval-sample: eval-require-cap  ## real run on a 10-case sample to measure actual cost
	cd evals && uv run python runner.py run --sample 10 --max-cost-usd $(EVAL_MAX_COST)

eval: eval-require-cap  ## FULL real run: 300 cases, scorecard, regression guard
	cd evals && uv run python runner.py run --max-cost-usd $(EVAL_MAX_COST)
	$(MAKE) eval-check

eval-report:         ## rebuild the scorecard for EVAL_RUN (default: latest)
	cd evals && uv run python report.py report --run runs/$(EVAL_RUN)

eval-baseline:       ## write evals/baseline.json from a real, complete run (review, then commit)
	cd evals && uv run python report.py baseline --run runs/$(EVAL_RUN)

eval-publish:        ## copy a complete REAL run's scorecard to evals/reports/ (commit it)
	cd evals && uv run python report.py publish --run runs/$(EVAL_RUN)

eval-check:          ## regression guard: fails if EVAL_RUN is worse than the committed baseline
	cd evals && uv run python report.py check --run runs/$(EVAL_RUN)

seed:
	cd mcp-servers && uv run python -m _shared.seed
