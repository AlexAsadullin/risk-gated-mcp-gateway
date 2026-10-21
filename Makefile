# Risk-gated MCP Gateway (Tradeq I9). Everything runs inside the repository's .venv via uv.
-include .env
export

UV ?= uv

.PHONY: setup lint format typecheck test cov reproduce report serve stdio clean

setup:
	$(UV) venv --python 3.13 .venv
	$(UV) sync

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

format:
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

typecheck:
	$(UV) run mypy src

test:
	$(UV) run pytest --cov --cov-report=term

reproduce:
	$(UV) run gateway reproduce

report:
	$(UV) run gateway report

serve:
	$(UV) run gateway serve

stdio:
	$(UV) run gateway stdio

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
