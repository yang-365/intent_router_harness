.PHONY: install test lint format serve mock-workflow e2e clean help

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install package with test dependencies in editable mode
	python -m pip install -e '.[test]'

install-all: ## Install with all optional dependencies (deepagent + test)
	python -m pip install -e '.[deepagent,test]'

test: ## Run the full pytest suite
	python -m pytest -q

test-verbose: ## Run pytest with verbose output
	python -m pytest -v

lint: ## Run ruff linter
	python -m ruff check src/ tests/

format: ## Run ruff formatter
	python -m ruff format src/ tests/

format-check: ## Check formatting without changing files
	python -m ruff format --check src/ tests/

serve: ## Start the ASGI server (PORT=8765)
	python -m intent_router_harness serve examples/deepagent-finance-router-harness.toml --port $${PORT:-8765}

mock-workflow: ## Start the mock workflow server (PORT=9876)
	python examples/mock_workflow_server.py --host 127.0.0.1 --port $${PORT:-9876}

e2e: ## Run end-to-end validation (requires running server + mock-workflow)
	python examples/deepagent_e2e_check.py --url http://127.0.0.1:$${PORT:-8765}/api/v1/message

clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info src/*.egg-info .pytest_cache __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
