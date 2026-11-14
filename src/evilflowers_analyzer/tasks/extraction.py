"""``evilflowers_analyzer.extract``: sent by the catalog when a PDF is stored, with keyword arguments.

``**_`` keeps the worker compatible with arguments a newer catalog adds.

The task acknowledges late and is rejected (requeued) when its worker process dies,
and a stopped worker (scale-in, ``REMAP_SIGTERM=SIGQUIT``) cancels it so Redis
requeues it at once: a book whose layout pass was cut short is delivered again.
The catalog's ``/start`` then begins a new attempt of the run and drops what the
earlier pass stored; the tasks of that pass carry its ``attempt`` and are refused
from then on (EvilFlowersCatalog#140). Each delivery is counted (``passes``), and
after ``EXTRACT_MAX_PASSES`` the run is failed instead of started again (#3).
"""

from functools import cache
from typing import Any

from celery import Task, shared_task

from evilflowers_analyzer import passes
from evilflowers_analyzer.pipeline import Extraction, Outcome
from evilflowers_analyzer.settings import get_settings


@cache
def _redis() -> Any:
    """The broker's Redis, where passes are counted; one client per worker process."""
    import redis

    return redis.Redis.from_url(get_settings().redis_url, socket_timeout=5, socket_connect_timeout=5)


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

    redelivered = bool((self.request.delivery_info or {}).get("redelivered"))
    try:
        client = _redis()
    except Exception:
        client = None  # `passes.count` counts this pass as the first rather than fail the book
    return Extraction(
        run_id,
        source,
        output_dir,
        dispatch=dispatch,
        acquisition_id=acquisition_id,
        pipeline_version=pipeline_version,
        passes=passes.count(client, run_id, redelivered),
    ).run()
