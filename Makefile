.PHONY: install install-all lint format typecheck test coverage check worker image

# Core + dev tools: enough for the tests.
install:
	uv sync

# Everything the image has (adds the OpenTelemetry and Pyroscope SDKs).
install-all:
	uv sync --all-extras

lint:
	uv run ruff check
	uv run ruff format --check

format:
	uv run ruff check --fix
	uv run ruff format

typecheck:
	uv run mypy

# tests/test_end_to_end.py needs a Redis (TEST_REDIS_URL, default redis://localhost:6379/15); it skips without one.
test:
	uv run pytest

coverage:
	uv run pytest --cov --cov-report=term-missing:skip-covered

check: lint typecheck test

# A local worker (needs a reachable REDIS_URL; CATALOG_API_URL/CATALOG_API_KEY to report).
worker:
	uv run celery -A evilflowers_analyzer.worker worker --loglevel=INFO -E --concurrency=1 --queues=evilflowers_analyzer

image:
	docker build --build-arg IMAGE_TAG=local@$$(git rev-parse --short HEAD) -t evilflowers-analyzer-service:local .
