.DEFAULT_GOAL := help
.PHONY: help sync hooks lint format format-check typecheck test test-int check db-up db-down migrate

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

sync: ## Install deps + dev tools via uv
	uv sync

hooks: ## Install husky git hooks
	npm install

lint: ## Lint with ruff
	uv run ruff check .

format: ## Auto-format with ruff
	uv run ruff format .

format-check: ## Check formatting without writing
	uv run ruff format --check .

typecheck: ## Strict type-check with mypy
	uv run mypy

test: ## Unit tests + coverage floor
	uv run pytest -m "not integration"

test-int: ## Integration tests (requires Postgres up)
	uv run pytest -m integration

check: format-check lint typecheck test ## Everything the pre-push hook runs

db-up: ## Start Postgres
	docker compose up -d postgres

db-down: ## Stop Postgres
	docker compose down

migrate: ## Apply Alembic migrations
	uv run alembic upgrade head
