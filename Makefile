SHELL := /bin/bash
.DEFAULT_GOAL := help

IMAGE ?= cold-chain-copilot:local
UV ?= uv
NPM ?= npm

.PHONY: help sync lock lint format typecheck test check \
        frontend-install frontend-dev frontend-build \
        serve run setup-rbac ingest-db ingest-sops ingest \
        docker-build docker-run clean

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

sync: ## Install the package and dev tools into .venv from uv.lock
	$(UV) sync --all-extras

lock: ## Re-resolve dependencies into uv.lock
	$(UV) lock

lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Apply Ruff fixes and formatting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## Mypy strict
	$(UV) run mypy

test: ## Pytest (offline unit suite)
	$(UV) run pytest

check: lint typecheck test ## Everything CI runs for the backend

frontend-install: ## Install frontend dependencies from package-lock.json
	cd frontend && $(NPM) ci

frontend-dev: ## Vite dev server on :5173 (proxies /api to :8000)
	cd frontend && $(NPM) run dev

frontend-build: ## Type-check and build the SPA into frontend/dist
	cd frontend && $(NPM) run build

serve: ## Run the API (and SPA, if built) on $$HOST:$$PORT
	$(UV) run fde-copilot serve

run: frontend-build serve ## Build the SPA, then serve everything on one port

setup-rbac: ## Create usr_fde_ro, fde_views, the view, and the audit log (idempotent)
	$(UV) run fde-copilot setup-rbac

ingest-db: ## Generate (if missing) and upsert the 25-row telemetry seed
	$(UV) run fde-copilot ingest-db

ingest-sops: ## Chunk, embed, and upsert data/policy into Pinecone (hash-cached)
	$(UV) run fde-copilot ingest-sops

ingest: setup-rbac ingest-db ingest-sops ## Full data bootstrap in dependency order

docker-build: ## Build the 3-stage production image
	docker build -t $(IMAGE) .

docker-run: ## Run the image with .env on :8000
	docker run --rm -it --env-file .env -e HOST=0.0.0.0 -p 8000:8000 --memory 700m $(IMAGE)

clean: ## Remove caches and build output
	rm -rf .mypy_cache .pytest_cache .ruff_cache dist frontend/dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
