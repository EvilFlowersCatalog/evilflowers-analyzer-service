"""``evilflowers_analyzer.extract``: sent by the catalog when a PDF is stored, with keyword arguments.

``**_`` keeps the worker compatible with arguments a newer catalog adds.

The task acknowledges late and is requeued when its worker process dies (``JOB_TASK_OPTIONS``), and a stopped worker
(scale-in) cancels it after ``WORKER_SOFT_SHUTDOWN_TIMEOUT`` so Redis requeues it at once: a book whose layout pass
was cut short is delivered again. The catalog's ``/start`` then begins a new attempt of the run and drops what the
earlier pass stored; the tasks of that pass carry its ``attempt`` and are refused from then on
(EvilFlowersCatalog#140). Each delivery is counted (``runtime.delivery``), and after ``EXTRACT_MAX_PASSES`` the run is
failed instead of started again (#3). A message the analyzer cannot read is reported failed, never retried.
"""

from typing import Any, cast

from celery import Task, shared_task
from pydantic import ValidationError

from evilflowers_analyzer import metrics

# The app's module, so `create_app` has run in this process: a pool child started by *spawn* (not fork) imports only
# the tasks module, and `runtime()` would find nothing set up there.
from evilflowers_analyzer import worker as _worker  # noqa: F401  # isort: skip
from evilflowers_analyzer.pipeline import ANALYZER_JOB, Extraction, Outcome
from evilflowers_analyzer.settings import Settings
from evilflowers_worker.contract import ExtractRequest
from evilflowers_worker.jobs import report_invalid_message
from evilflowers_worker.runtime import JOB_TASK_OPTIONS, CeleryPublisher, runtime


@shared_task(name=ExtractRequest.TASK, bind=True, **JOB_TASK_OPTIONS)
def extract(
    self: Task,
    run_id: str,
    source: str,
    output_dir: str,
    acquisition_id: str | None = None,
    pipeline_version: int | None = None,
    **_: Any,
) -> Outcome:
    """Open the PDF, start the run, send paragraphs to text at once, then the layout regions by kind."""
    current = runtime()
    settings = cast(Settings, current.settings)
    kwargs = {
        "run_id": run_id,
        "source": source,
        "output_dir": output_dir,
        "acquisition_id": acquisition_id,
        "pipeline_version": pipeline_version,
    }
    try:
        request = ExtractRequest.model_validate(kwargs)
    except ValidationError as error:
        metrics.record_run("failed", "input")
        return report_invalid_message(kwargs, error, job=ANALYZER_JOB, settings=settings)
    return Extraction(
        request,
        publisher=CeleryPublisher(self.app),
        delivery=current.delivery(self),
        settings=settings,
    ).run()
