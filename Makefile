.PHONY: install lint test run build up up-obs down logs pull

# ---------- Dev ----------
install:
	pip install -e ".[dev]"

lint:
	ruff check app tests
	ruff format --check app tests

lint-fix:
	ruff check --fix app tests
	ruff format app tests

test:
	pytest tests/ -v --tb=short

test-cov:
	pytest tests/ --cov=app --cov-report=term-missing --cov-report=html

# ---------- Local run (no Docker) ----------
run:
	APP_ENV=dev python -m app.main

serve:
	APP_ENV=dev uvicorn app.web.server:app --reload --port 8000

# ---------- Docker ----------
build:
	docker compose build

up:
	docker compose --profile core up -d

up-obs:
	docker compose --profile core --profile obs up -d

down:
	docker compose --profile core --profile obs down

down-v:
	docker compose --profile core --profile obs down -v

logs:
	docker compose logs -f agent-api

pull:
	docker compose --profile core --profile obs pull

# ---------- DB migrations (placeholder for Phase 2) ----------
db-migrate:
	alembic upgrade head

db-rollback:
	alembic downgrade -1
