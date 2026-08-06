# Tells make that these names are shortcuts for commands rather than physical files on the computer
.PHONY: dev lint format test clean

# starts the local development backend server. 
dev:
	cd backend && uvicorn app.main:app --reload --port 8000

# checks the backend code for style issues, potential bugs, and formatting violations using ruff (fast python linter)
lint:
	cd backend && ruff check .

# automatically formats the code formatting and auto-fixable linting errors
format:
	cd backend && ruff format .
	cd backend && ruff check --fix .

# runs the backend test suite
test:
	cd backend && pytest

# deletes temporary build and cache folders across the entire project to keep the env clean
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +