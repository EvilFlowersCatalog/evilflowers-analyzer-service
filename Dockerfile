# syntax=docker/dockerfile:1.7

# -- build: the virtual environment, from uv.lock -------------------------------------------------------------------
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential git && \
    rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first (cached while only the code changes). torch comes from the CPU index (pyproject.toml); the
# worker kit (evilflowers-worker) from its public git repository at the commit uv.lock pins (hence `git` above).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --all-extras --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-extras --no-editable

# -- runtime -----------------------------------------------------------------------------------------------------------
FROM python:3.12-slim

# Docling's image stack (OpenCV) needs these at runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# The pre-1.0 start command (`celery --workdir src -A worker`) still works; see src/worker.py.
COPY src/worker.py ./src/worker.py

ARG IMAGE_TAG=dev
# No REMAP_SIGTERM: SIGTERM stays Celery's warm shutdown, and the worker itself turns it cold after
# WORKER_SOFT_SHUTDOWN_TIMEOUT (evilflowers_analyzer.worker.cancel_after_grace). With SIGTERM remapped to SIGQUIT,
# Celery's soft shutdown sleeps out the whole timeout and then kills the child before acknowledging a finished book.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    IMAGE_TAG=${IMAGE_TAG} \
    HF_HOME=/models

# Model weights live in a volume, not in the image (IP-018): Docling's layout model is downloaded on first use.
VOLUME ["/models"]

# Prefork with one child: the parent stays responsive to Flower while the child runs a book, and
# telemetry and profiling start in the child (worker_process_init).
# No --queues: the app consumes what its settings name (ANALYZER_QUEUE, or the two standalone queues with
# ANALYZER_STANDALONE=1, #8). A Deployment that wants a subset passes `-Q`, e.g. the digitalizer's interactive replica:
# `-Q evilflowers_analyzer.standalone.interactive`.
CMD ["celery", "-A", "evilflowers_analyzer.worker", "worker", "--loglevel=INFO", "-E", "--concurrency=1"]
