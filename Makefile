.PHONY: help infra-up infra-down backend-init backend-run clean

help:
	@echo "Available commands:"
	@echo "  make infra-up      - Launch local Docker background database infrastructure"
	@echo "  make infra-down    - Spin down running local database infrastructures"
	@echo "  make backend-init  - Provision Python virtual isolation and compile bindings"
	@echo "  make backend-run   - Spin up local active FastAPI gateway instances"
	@echo "  make clean         - Clear volatile cache lines, pycache trees, and indexes"

infra-up:
	docker compose -f infrastructure/docker-compose.yml up -d

infra-down:
	docker compose -f infrastructure/docker-compose.yml down

backend-init:
	cd backend && python3.11 -m venv .venv
	./backend/.venv/bin/pip install --upgrade pip
	./backend/.venv/bin/pip install fastapi uvicorn celery redis langgraph langchain llama-index qdrant-client mlflow psycopg2-binary

backend-run:
	export $$(cat .env | xargs) && cd backend && .venv/bin/uvicorn api.main:app --reload --port 8000

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.py[co]" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +