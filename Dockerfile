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

# Dependencies first (cached while only the code changes). torch comes from the CPU index (pyproject.toml).
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
# REMAP_SIGTERM: k8s stops a pod with SIGTERM, which Celery treats as a warm shutdown (finish the book, hours).
# As SIGQUIT it is a cold shutdown after WORKER_SOFT_SHUTDOWN_TIMEOUT: the task is cancelled and requeued at once.
# Billiard reads it when Celery is imported, so it lives here, not in Settings.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    IMAGE_TAG=${IMAGE_TAG} \
    HF_HOME=/models \
    REMAP_SIGTERM=SIGQUIT

# Model weights live in a volume, not in the image (IP-018): Docling's layout model is downloaded on first use.
VOLUME ["/models"]

# Prefork with one child: the parent stays responsive to Flower while the child runs a book, and
# telemetry and profiling start in the child (worker_process_init).
CMD ["celery", "-A", "evilflowers_analyzer.worker", "worker", "--loglevel=INFO", "-E", "--concurrency=1", \
     "--queues=evilflowers_analyzer"]
