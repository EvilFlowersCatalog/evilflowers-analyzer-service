# EvilFlowers Analyzer Service

The Celery worker that starts every IP-018 extraction run of
[EvilFlowersCatalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog) (Intelligent Viewer, catalog
`docs/proposals/posts/ip-018-intelligent-viewer.md`). It opens a stored PDF, starts the run on the catalog, runs one
Docling layout pass (CPU, no OCR) and hands each kind of region to the service that enriches it. It writes no files and
posts no regions itself.

This repository is also the **reference for extraction services**:
- [`docs/architecture.md`](https://github.com/EvilFlowersCatalog/evilflowers-worker/blob/master/docs/architecture.md)
  in evilflowers-worker is the model: roles, the message contract, delivery semantics and the observability contract.
- [evilflowers-worker](https://github.com/EvilFlowersCatalog/evilflowers-worker) is the kit every service builds on:
  contract, catalog client, leaf-job lifecycle, Celery runtime, scale-in and observability. This service pins it to a
  commit (`[tool.uv.sources]` in `pyproject.toml`).

The analyzer itself is only its domain code (layout, outline, the coordinator in `pipeline.py`) on top of the kit.

## Tasks

The task names, queues and argument shapes are contracts (`tests/test_worker.py` and `tests/test_standalone.py`
guard them).

| Task | Queue | Sent by | Kwargs |
|---|---|---|---|
| `evilflowers_analyzer.extract` | `evilflowers_analyzer` | catalog (`apps/extraction/services.py`) | `run_id`, `source`, `output_dir`, `acquisition_id` (logged), `pipeline_version` (logged), `steps` (a step re-run, #7); unknown kwargs are ignored |
| `evilflowers_analyzer.standalone.layout` | `evilflowers_analyzer.standalone`, `….standalone.interactive` | books digitalizer (#8) | `run_id`, `job`, `token`, `output_dir`, `outcome`, `leaves` (`[{leaf, image, mask}]`); see `tasks/standalone.py` |

**Step re-run** (#7). With `steps` (a sorted subset of `equations`, `figures`, `paragraphs`, `tables`) the analyzer
re-runs only those steps of a failed run. Its `/start` carries `steps` and only their jobs, named by the current
batching. It dispatches only those steps, and runs the layout pass only when a region step is named. Until that
`/start` succeeds it reports nothing, and a 409 (`not_running`, `steps_refused`) is a skip.

**Standalone layout** (#8). The same Docling pass, on the digitalizer's leaf images instead of a PDF. It writes
`regions.json` and then an outcome file into the job's directory, and posts nothing to the catalog. It runs in a
second Deployment of the same image with `ANALYZER_STANDALONE=1`, on the digitalizer's Redis db, with no catalog
key. On an image without OCR, Docling keeps only pictures, tables and formulas, so captions are empty.

A run goes like this (`src/evilflowers_analyzer/pipeline.py`):

1. Open the PDF with PyMuPDF. `source` is relative to `STORAGE_PATH`. Read its outline (`outline.py`, #5): the
   bookmarks, or, for a book without any, the headings detected on the text layer (lines set larger than the body
   text, or bold and numbered like `1.2.3`, that do not repeat as running heads).
2. `POST /start` with the page count, page sizes, provenance (image tag, Docling version, `outline_source`:
   `bookmarks`, `headings`, `none` or `failed`, and `outline_entries`) and the jobs it is about to
   dispatch: `paragraphs`, `figures`, `equations`, `tables`. The catalog answers with the run's new `attempt`
   (EvilFlowersCatalog#140), which every dispatched task carries.
3. Send the paragraph job to text-service at once; it needs only the text layer.
4. Run Docling layout over pages in batches of `LAYOUT_BATCH_PAGES`. A batch that fails is logged and skipped.
5. Send each kind of region to its service. A task goes out even when a kind has no regions, so every declared job
   reports and the run can finish.

| Job | Task | Queue | Kwargs |
|---|---|---|---|
| paragraphs | `evilflowers_text_worker.paragraphs` | `evilflowers.text.paragraphs` | `run_id, source, output_dir, attempt, outline` |
| figures | `evilflowers_image.figures` | `evilflowers_image` | `+ regions` |
| equations | `evilflowers_equation.equations` | `evilflowers_equation` | `+ regions` |
| tables | `evilflowers_text_worker.tables` | `evilflowers.text.tables` | `+ regions` |

The two text jobs have their own queues (#4, evilflowers-text-service#13). Before that, both went to the shared
`evilflowers_text_worker`. The text worker still consumes that legacy queue by default, so deploy the text image (and
its k8s args without `--queues=evilflowers_text_worker`, knowledge#48) **before** this analyzer. Drop the legacy queue
once `redis_key_size{key="evilflowers_text_worker"}` is 0.

`outline` is a list of `[level, title, page]` (1-based page), the shape of PyMuPDF's `get_toc(simple=True)`, so
text-service can use it wherever it read the bookmarks. When present it is authoritative, and it is `[]` for a book
with no outline. It is left out when the analyzer could not read the outline (`failed`), so text-service falls back
to the bookmarks.

`attempt` is left out when the catalog returns none. A service sends it back as `?attempt=` with its results, and the
catalog refuses results of a pass that a later `/start` superseded.

**Per batch (`DISPATCH_PER_BATCH=true`, #2).** Instead of one task per kind after the whole layout pass, the analyzer
sends one task per kind for every layout batch, as soon as the batch is laid out. Each is its own job, named
`<job>:<first>-<last>` (`equations:0001-0008`). All of them are declared at `/start`, empty batches included, and the
name goes to the service as the `job` kwarg, which it reports under. A book's equations then spread over every
equation pod, and a stopped pod loses one batch rather than a book. **Keep it off until the equation, image and text
services report under `job`.** Until then they report `equations` once, and the batch jobs never close.

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
  starts the run again: `/start` drops the earlier pass's results and begins a new attempt, so the earlier pass's tasks
  are refused. Redis waits `BROKER_VISIBILITY_TIMEOUT` (12 h, the same for every consumer of the db) before it
  redelivers a message whose worker vanished without a word.
- **Scale-in** (the kit's `evilflowers_worker.shutdown`). The SIGTERM of a stopped pod is Celery's warm shutdown:
  the worker takes no new book and lets the one in hand run. If that book is still running after
  `WORKER_SOFT_SHUTDOWN_TIMEOUT` (15 min), the worker sends itself SIGQUIT (cold shutdown). The book is cancelled, and its message is back on the queue at once, not
  after the visibility timeout. A typical book (3–10 min) finishes within the grace, is acknowledged, and the worker
  exits. An idle worker exits at once. Keep the timeout under the pod's termination grace.

  If the cold shutdown cannot stop the book within `WORKER_CANCEL_TIMEOUT` (30 s), the worker puts the message back
  on the queue itself and exits. On dev the parent's kill of its prefork child was denied (`PermissionError`,
  errno 13, 2026-11-14), and this fallback covers that. The worker is the container's PID 1, so the child goes with it.

  Don't use Celery's own soft shutdown (`REMAP_SIGTERM=SIGQUIT` with `worker_soft_shutdown_timeout`). It sleeps the
  whole timeout in the signal handler even after the book is done, then has the pool kill the child before the book's
  acknowledgement is processed. On dev that kill was denied and the shutdown aborted, so a book that had succeeded at
  21:02 was restored and redone at 21:14. The worker puts SIGTERM back to a warm shutdown even if `REMAP_SIGTERM`
  is set. The kit's `tests/test_runtime.py` checks each case against a real Redis, the denied kill included.
- **Redelivery cap (#3).** Every delivery of the message is counted in Redis
  (`evilflowers:deliveries:analyzer:<task id>`). A fresh message starts at 1, a redelivery adds one, and a deliberate
  requeue on scale-in gives its count back. After `EXTRACT_MAX_PASSES` (5), for example a book that OOM-kills the
  worker every time, the analyzer reports job `analyzer` failed instead of starting the run again.
- **An invalid message** (a kwarg of the wrong type) is reported as job `analyzer` failed and acknowledged. It is never
  retried, since the same message would fail the same way.

## Layout

```
src/
├── worker.py                  shim for the pre-1.0 start command (`celery --workdir src -A worker`)
└── evilflowers_analyzer/
    ├── worker.py              the Celery app: `runtime.create_app("analyzer", …)`
    ├── settings.py            `WorkerSettings` + the queues it sends to and the layout pass
    ├── pipeline.py            the coordinator: open → start → paragraphs → layout → region jobs
    ├── layout.py              the Docling layout pass, batch by batch, region geometry
    ├── outline.py             the outline: bookmarks, else headings detected on the text layer
    ├── metrics.py             the analyzer's own metrics (the standard ones are the kit's)
    └── tasks/                 `evilflowers_analyzer.extract`: validate the message, count the delivery, run
```

Shared code is not copied between services any more. A fix to the catalog client, the delivery cap, scale-in or
telemetry goes into [evilflowers-worker](https://github.com/EvilFlowersCatalog/evilflowers-worker), and every
service gets it by bumping the pinned commit (`rev` in `[tool.uv.sources]`, then `uv lock`).

## Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.12. torch comes from the PyTorch CPU index
(`[tool.uv.sources]` in `pyproject.toml`), never the CUDA build. The `otel` extra adds the OpenTelemetry SDK, and the
`profiling` extra adds the Pyroscope SDK.

The worker kit comes from its git repository at the pinned commit. To work on both at once, point the source at a
local checkout for a while: `evilflowers-worker = { path = "../evilflowers-worker", editable = true }`.

```bash
make install        # core + dev tools (ruff, mypy, pytest): enough for the tests; no model is downloaded
make check          # ruff check, ruff format --check, mypy, pytest
make coverage       # pytest with branch coverage
# tests/test_end_to_end.py runs the production worker against Redis (TEST_REDIS_URL, default
# redis://localhost:6379/15); it skips without one locally and fails without one in CI.
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
- `DISPATCH_PER_BATCH` (off), region jobs per layout batch (see above)
- `WORKER_SOFT_SHUTDOWN_TIMEOUT` (900 s), the scale-in grace, and `WORKER_CANCEL_TIMEOUT` (30 s), how long the
  cancel may take before the worker requeues the book itself
- `EXTRACT_MAX_PASSES` (5), deliveries of one run before it is failed
- `ANALYZER_QUEUE`, `TEXT_TABLES_QUEUE`, `TEXT_PARAGRAPHS_QUEUE`, `IMAGE_QUEUE`, `EQUATION_QUEUE`, only when a queue
  is renamed. The image's `CMD` names no queue, so the worker consumes the one its settings name. The old
  `TEXT_QUEUE` is ignored, so a manifest that still sets it cannot send the text jobs back to the legacy queue.
- `ANALYZER_STANDALONE` (off): consume `evilflowers_analyzer.standalone.interactive` and
  `evilflowers_analyzer.standalone` instead of `ANALYZER_QUEUE` (the digitalizer's Deployment, #8). Its interactive
  replica passes `-Q evilflowers_analyzer.standalone.interactive`. Such a Deployment needs no `CATALOG_API_*`.
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
  - from the kit, the same in every service:
    - `analyzer_job_outcome_total{job,outcome}`
    - `analyzer_catalog_retries_total{route,reason}`, where the label was `path` before the kit
    - `analyzer_deliveries_capped_total`
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
