.PHONY: dev lint format test clean

dev:
	cd backend && uvicorn app.main:app --reload --port 8000

lint:
	cd backend && ruff check .

format:
	cd backend && ruff format .
	cd backend && ruff check --fix .

test:
	cd backend && pytest

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +