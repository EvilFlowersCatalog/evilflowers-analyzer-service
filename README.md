# EvilFlowers Analyzer Service

The Celery worker that starts every IP-018 extraction run of
[EvilFlowersCatalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog) (Intelligent Viewer, catalog
`docs/proposals/posts/ip-018-intelligent-viewer.md`). It opens a stored PDF, starts the run on the catalog, runs one
Docling layout pass (CPU, no OCR) and hands each kind of region to the service that enriches it. It writes no files and
posts no regions itself.

## Tasks

The task name, queue and argument shape are a contract with the catalog (`tests/test_worker.py` guards it).

| Task | Queue | Sent by | Kwargs |
|---|---|---|---|
| `evilflowers_analyzer.extract` | `evilflowers_analyzer` | catalog (`apps/extraction/services.py`) | `run_id`, `source`, `output_dir`, `acquisition_id` (logged), `pipeline_version` (logged); unknown kwargs are ignored |

A run goes like this (`src/evilflowers_analyzer/pipeline.py`):

1. Open the PDF with PyMuPDF. `source` is relative to `STORAGE_PATH`.
2. `POST /start` with the page count, page sizes, provenance (image tag, Docling version) and the jobs it is about to
   dispatch: `paragraphs`, `figures`, `equations`, `tables`.
3. Send the paragraph job to text-service at once; it needs only the text layer.
4. Run Docling layout over pages in batches of `LAYOUT_BATCH_PAGES`. A batch that fails is logged and skipped.
5. Send each kind of region to its service. A task goes out even when a kind has no regions, so every declared job
   reports and the run can finish.

| Job | Task | Queue | Kwargs |
|---|---|---|---|
| paragraphs | `evilflowers_text_worker.paragraphs` | `evilflowers_text_worker` | `run_id, source, output_dir` |
| figures | `evilflowers_image.figures` | `evilflowers_image` | `+ regions` |
| equations | `evilflowers_equation.equations` | `evilflowers_equation` | `+ regions` |
| tables | `evilflowers_text_worker.tables` | `evilflowers_text_worker` | `+ regions` |

`regions` is a list of `{page, seq, bbox, caption}`. `bbox` holds page fractions (`x0, y0, x1, y1`) with the origin at
the top left, on the original file's 1-based page. `seq` numbers the regions of one kind on one page in reading order.
Each service reports its own job to `{CATALOG_API_URL}/extraction/v1/runs/{run_id}/…`, and the catalog finishes the
run when all of them have.

A run never waits on the analyzer forever:

- **The PDF cannot be opened, or `/start` is rejected** (a 4xx, or a 5xx after every retry): the analyzer reports job
  `analyzer` as failed, and the catalog fails the run.
- **Layout fails as a whole** (for example, the model cannot load): `figures`, `equations` and `tables` are reported
  failed. `paragraphs` has already been sent.
- **A 409** means the catalog no longer accepts the run (it was reset, started or finished). The task stops quietly.
- **Late acknowledgement.** The task acknowledges late and is requeued if its worker process dies. A redelivered book
  stops at `/start` with a 409, so nothing is dispatched twice. Redis waits `BROKER_VISIBILITY_TIMEOUT` (2 h) before it
  redelivers an unacknowledged message.

## Layout

```
src/
├── worker.py                  shim for the pre-1.0 start command (`celery --workdir src -A worker`)
└── evilflowers_analyzer/
    ├── worker.py              Celery app (`celery -A evilflowers_analyzer.worker worker`), config and routes
    ├── settings.py            typed settings from the environment (pydantic-settings)
    ├── pipeline.py            one run: open → start → dispatch paragraphs → layout → dispatch regions
    ├── layout.py              the Docling layout pass, region geometry
    ├── tasks/                 thin Celery adapter for `evilflowers_analyzer.extract`
    ├── clients/               catalog ingestion API, shared HTTP session
    └── observability/         OpenTelemetry setup, spans, metrics, JSON logs, Pyroscope profiling, Celery signal wiring
```

`clients/`, `observability/`, `settings.py` and `worker.py` have the same shape as in
[evilflowers-text-service](https://github.com/EvilFlowersCatalog/evilflowers-text-service) and the other extraction
workers; keep them in step.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.12. torch comes from the PyTorch CPU index
(`[tool.uv.sources]` in `pyproject.toml`), never the CUDA build. The `otel` extra adds the OpenTelemetry SDK, and the
`profiling` extra adds the Pyroscope SDK.

```bash
make install        # core + dev tools (ruff, pytest): enough for the tests; no model is downloaded
make check          # ruff check, ruff format --check, pytest
make install-all    # everything the image has (also runs the trace-propagation test)
make worker         # a local worker (REDIS_URL, CATALOG_API_URL, CATALOG_API_KEY from .env)
make image          # docker build, tagged evilflowers-analyzer-service:local
docker compose up --build    # Redis + analyzer, ./data as storage, ./models as the model cache
```

Bump dependencies with `uv lock --upgrade-package <name>`. `docling` is not pinned beyond `>=2.30`. Its version goes
into every run's provenance, so a bump is visible per run.

## Configuration

All settings are environment variables. `.env.sample` lists them, and `src/evilflowers_analyzer/settings.py` has their
defaults and meaning. The ones a deployment sets:

- `REDIS_URL`, the catalog's broker
- `STORAGE_PATH`, the catalog's storage root (originals are read here)
- `CATALOG_API_URL` and `CATALOG_API_KEY`, the API key of a catalog user holding `extraction.ingest_extraction`
- `HF_HOME`, Docling's model cache (`/models` in the image, a volume; the layout model is downloaded on first use)
- `LAYOUT_BATCH_PAGES` (16) and `LAYOUT_THREADS` (4), which trade memory and CPU against speed
- `ANALYZER_QUEUE`, `TEXT_QUEUE`, `IMAGE_QUEUE`, `EQUATION_QUEUE`, only when a queue is renamed. The image's `CMD`
  passes `--queues=evilflowers_analyzer`, so renaming the consumed queue also needs a changed command.
- `IMAGE_TAG`, built in by CI

## Observability

The service follows the cluster's telemetry contract (knowledge base, `Cookbooks/Deploy-a-Service` §3). Everything is
off unless its variables are set, so Compose and local runs need nothing.

- **Traces.** The `extract` task gets a CONSUMER span that continues the catalog's trace, because the catalog puts
  `traceparent` into the task headers. Every task it sends gets a PRODUCER span with `traceparent` in its headers, so
  text, image and equation join the same trace (`tests/test_telemetry.py` checks the header). Every catalog call gets
  a CLIENT span. Hand-made spans cover `analyzer.pdf.open`, `analyzer.catalog.start`, `analyzer.layout` (model load
  `analyzer.layout.load` and each `analyzer.layout.batch` with its page range). Spans carry
  `evilflowers.extraction.run_id`, `evilflowers.acquisition_id` and `evilflowers.source`, and a failure the analyzer
  reports itself still marks the task span as failed.

  Configuration is the standard `OTEL_*` environment, including the sampler (`OTEL_TRACES_SAMPLER`,
  `OTEL_TRACES_SAMPLER_ARG`). The endpoint comes from `OTEL_EXPORTER_OTLP_ENDPOINT`, or from `HOST_IP` when only that
  is set. With neither, telemetry is off.
- **Metrics** (Mimir):
  - `analyzer_task_duration_seconds`
  - `analyzer_run_outcome_total{outcome,stage}`, where `outcome` is `dispatched`, `failed` or `not_running`, and
    `stage` is `open`, `start`, `layout` or `dispatch`
  - `analyzer_layout_duration_seconds{outcome}` and `analyzer_layout_pages_total{outcome}`, per layout batch
  - `analyzer_document_pages`
  - `analyzer_regions_total{kind}`
  - `analyzer_catalog_retries_total`
  - process CPU and memory
- **Logs.** Each log line is one JSON object on stdout, carrying `level`, `trace_id`/`span_id`, `task_name`/`task_id`,
  and the task's `run_id`, `acquisition_id` and `source`. `LOG_FORMAT=text` gives Celery's own format for a terminal.
- **Profiles** (Pyroscope). The node's eBPF profiler cannot unwind Python on k3s `control`, so the worker profiles
  itself with the Pyroscope SDK (the `profiling` extra) when `PYROSCOPE_SERVER_ADDRESS` is set. It reads
  `PYROSCOPE_APPLICATION_NAME` (default `analyzer`) and `PYROSCOPE_TAGS` (`k=v` pairs separated by commas). It samples
  with `gil_only=False` because torch runs outside the GIL. Each trace's root span links to its profile.

The SDK, the profiler and their exporter threads start on `worker_process_init`, in the prefork child that runs the
tasks, never in the parent before the fork.

Queries:

```
Tempo:     {resource.service.name="analyzer" && kind=consumer}
Tempo:     {resource.service.name="analyzer" && span.evilflowers.extraction.run_id="<run uuid>"}
Loki:      {service_name="analyzer"} | json | run_id="<run uuid>"
Pyroscope: {service_name="analyzer", namespace="evilflowers-dev"}
```

## Deployment

CI (`.github/workflows/docker-publish.yml`) builds `ghcr.io/evilflowerscatalog/evilflowers-analyzer-service`:

| Event | Tags | Deploy |
|---|---|---|
| push to `develop` | `:develop`, `:sha-<short>` | `rollout restart deployment/analyzer` in `evilflowers-dev`, then a check that every running pod uses the pushed digest |
| push to `main` | `:main`, `:sha-<short>` | none |
| tag `v*.*.*` | `:<version>`, `:sha-<short>` | none |
| pull request | built, not pushed | none |

The Deployment itself (env, labels, volumes, the start command) belongs to the knowledge repo's Ansible role
`evilflowers_k8s`, not to this repo. A push never changes the pod's `env:`.

The start command changed to `celery -A evilflowers_analyzer.worker worker …`. The dev Deployment's `args` still
hold a copy of the old one (`celery --workdir src -A worker worker …`), and the image keeps `src/worker.py` so that
command still loads the app. Remove the shim once the role's `args` are updated
([EvilFlowersCatalog/knowledge#22](https://github.com/EvilFlowersCatalog/knowledge/issues/22), which also covers the
`:develop` pin, the `OTEL_*`/`PYROSCOPE_*` env and the CI runner access). The first push to `develop` must wait for
those, or the deploy job queues without a runner or fails its own digest check.
