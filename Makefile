.PHONY: up down install migrate run replay demo-data test test-unit lint eval seed

PY_PROJECTS := agent-core mcp-servers dashboard
MVN := ./mvnw

up:
	docker compose up -d --wait
	@for t in settlement.failures settlement.failures.dlq; do \
		docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
			--create --if-not-exists --topic $$t --partitions 1 --replication-factor 1 >/dev/null; \
	done

down:
	docker compose down

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
	cd producer-service && $(MVN) -q verify

lint:
	@for p in $(PY_PROJECTS); do (cd $$p && uv run ruff check . && uv run ruff format --check . && uv run mypy .) || exit 1; done
	cd producer-service && $(MVN) -q spotless:check

eval:
	@echo "not implemented yet (Phase 7)"; exit 1

seed:
	cd mcp-servers && uv run python -m _shared.seed
