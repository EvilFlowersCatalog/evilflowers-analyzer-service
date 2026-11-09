"""``evilflowers_analyzer.extract``: sent by the catalog when a PDF is stored, with keyword arguments.

``**_`` keeps the worker compatible with arguments a newer catalog adds.

The task acknowledges late and is rejected (requeued) when its worker process dies:
a book whose layout pass was cut short is redelivered. The catalog answers 409
(`RunNotRunning`) to a run that already started or finished, so a second delivery
stops at ``/start`` and dispatches nothing twice.
"""

from typing import Any

from celery import Task, shared_task

from evilflowers_analyzer.pipeline import Extraction, Outcome


@shared_task(name="evilflowers_analyzer.extract", bind=True, acks_late=True, reject_on_worker_lost=True)
def extract(
    self: Task,
    run_id: str,
    source: str,
    output_dir: str,
    acquisition_id: str | None = None,
    pipeline_version: int | None = None,
    **_,
) -> Outcome:
    """Open the PDF, start the run, send paragraphs to text at once, then the layout regions by kind."""

    def dispatch(task: str, kwargs: dict[str, Any], queue: str) -> None:
        # Sent from inside the CONSUMER span: the instrumentation adds a PRODUCER span and
        # puts `traceparent` into the message headers, so the downstream job joins this trace.
        self.app.send_task(task, kwargs=kwargs, queue=queue)

    return Extraction(
        run_id,
        source,
        output_dir,
        dispatch=dispatch,
        acquisition_id=acquisition_id,
        pipeline_version=pipeline_version,
    ).run()
