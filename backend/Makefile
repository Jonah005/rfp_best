.PHONY: install up down migrate seed run worker test lint fmt typecheck
install:   ; pip install -e ".[dev]"
up:        ; docker compose up -d
down:      ; docker compose down
migrate:   ; alembic upgrade head
seed:      ; python -m app.db.seed
run:       ; uvicorn app.main:app --reload --port 8000
worker:    ; celery -A app.workers.celery_app.celery_app worker -l info
beat:      ; celery -A app.workers.celery_app.celery_app beat -l info
test:      ; pytest -q
lint:      ; ruff check app tests alembic
fmt:       ; ruff check --fix app tests alembic && ruff format app tests alembic
typecheck: ; mypy app
