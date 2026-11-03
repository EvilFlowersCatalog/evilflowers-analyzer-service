"""Entry point of an extraction run (IP-018).

The catalog sends `evilflowers_analyzer.extract` when a PDF is stored. The
analyzer opens the document, starts the run with its page sizes and the jobs
it is about to dispatch, hands the paragraph job to text-service at once, runs
the layout pass and gives each kind of region to its service. Each service
reports its own job; the catalog finishes the run when all of them have.
"""

import logging
import os

import fitz
from celery import Celery

import layout
import settings
from catalog import Catalog, RunNotRunning

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("analyzer")

#: Job name per region kind, and the task that receives it.
DISPATCH = {
    "figure": ("figures", "evilflowers_image.figures", settings.IMAGE_QUEUE),
    "equation": ("equations", "evilflowers_equation.equations", settings.EQUATION_QUEUE),
    "table": ("tables", "evilflowers_text_worker.tables", settings.TEXT_QUEUE),
}
PARAGRAPHS = ("paragraphs", "evilflowers_text_worker.paragraphs", settings.TEXT_QUEUE)
JOBS = [PARAGRAPHS[0]] + [job for job, _task, _queue in DISPATCH.values()]

app = Celery("evilflowers_analyzer", broker=settings.REDIS_URL)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="Europe/Bratislava",
    enable_utc=True,
    worker_send_task_events=True,
    task_send_sent_event=True,
    # A document takes minutes; one at a time per worker, redelivered if the worker dies.
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)


@app.task(name="evilflowers_analyzer.extract", queue=settings.QUEUE)
def extract(run_id: str, acquisition_id: str, source: str, output_dir: str, pipeline_version: int, **_):
    catalog = Catalog(run_id)
    path = os.path.join(settings.STORAGE_PATH, source)
    logger.info("Extracting run=%s acquisition=%s source=%s", run_id, acquisition_id, source)

    try:
        with fitz.open(path) as document:
            page_sizes = [[round(page.rect.width, 2), round(page.rect.height, 2)] for page in document]
    except Exception as error:
        logger.exception("Cannot open %s", path)
        catalog.job("analyzer", failed=True, error=f"Cannot open the PDF: {error}")
        return

    provenance = {"analyzer": {"image": settings.IMAGE_TAG, **layout.versions()}}
    try:
        catalog.start(len(page_sizes), page_sizes, JOBS, provenance)
    except RunNotRunning:
        logger.warning("Run %s is not accepting results; skipped", run_id)
        return

    common = {"run_id": run_id, "source": source, "output_dir": output_dir}
    # The paragraph index needs only the text layer: it does not wait for layout.
    app.send_task(PARAGRAPHS[1], kwargs=common, queue=PARAGRAPHS[2])

    try:
        regions = layout.find_regions(path, len(page_sizes))
    except Exception as error:
        logger.exception("Layout failed for run %s", run_id)
        for job, _task, _queue in DISPATCH.values():
            catalog.job(job, failed=True, error=f"Layout failed: {error}")
        return

    for kind, (job, task, queue) in DISPATCH.items():
        # Sent even when empty, so every declared job reports and the run can finish.
        app.send_task(task, kwargs={**common, "regions": regions[kind]}, queue=queue)
        logger.info("Run %s: %d %s regions to %s", run_id, len(regions[kind]), kind, queue)
