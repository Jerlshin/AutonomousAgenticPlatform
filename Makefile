.PHONY: help infra-up infra-down sandbox-build backend-check backend-run backend-cli clean

help:
	@echo "Available commands:"
	@echo "  make infra-up      - Launch local Docker background database infrastructure"
	@echo "  make infra-down    - Spin down running local database infrastructures"
	@echo "  make sandbox-build - Build the restricted Python execution sandbox image"
	@echo "  make backend-check - Verify the ai_env backend dependency environment"
	@echo "  make backend-run   - Spin up local FastAPI gateway with conda ai_env"
	@echo "  make backend-cli   - Run a sample multi-agent workflow from the CLI"
	@echo "  make clean         - Clear volatile cache lines, pycache trees, and indexes"

infra-up:
	docker compose -f infrastructure/docker-compose.yml up -d

infra-down:
	docker compose -f infrastructure/docker-compose.yml down

sandbox-build:
	docker build -t autonomous-ai-sandbox:latest -f mlops/sandbox.Dockerfile .

backend-check:
	conda run -n ai_env python -c "import fastapi, langgraph, pydantic; print('ai_env backend dependencies ok')"

backend-run:
	cd backend && conda run -n ai_env uvicorn api.main:app --reload --port 8000

backend-cli:
	cd backend && conda run -n ai_env python main.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.py[co]" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
