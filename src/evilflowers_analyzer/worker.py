"""The Celery application: ``celery -A evilflowers_analyzer.worker worker``.

The task names and queues are contracts and must not change:

=========================================  ==============================================  ======================
Task                                       Queue                                           Sent by
=========================================  ==============================================  ======================
``evilflowers_analyzer.extract``           ``ANALYZER_QUEUE``                              catalog (kwargs)
``evilflowers_analyzer.standalone.layout`` ``evilflowers_analyzer.standalone`` and         books digitalizer
                                           ``….standalone.interactive``                    (kwargs, #8)
=========================================  ==============================================  ======================

One image, two Deployments: the catalog's consumes ``ANALYZER_QUEUE``; the digitalizer's sets ``ANALYZER_STANDALONE``
and consumes the two standalone queues (on its own Redis db). Both register both tasks.

Everything else (delivery semantics, scale-in, telemetry, JSON logs) is the kit's ``runtime.create_app``
(evilflowers-worker ``docs/architecture.md`` §3.3, §4).
"""

from celery import Celery

from evilflowers_analyzer.settings import Settings, get_settings
from evilflowers_worker import runtime

SERVICE = "analyzer"
TASK_PREFIX = "evilflowers_analyzer"
STANDALONE_PREFIX = f"{TASK_PREFIX}.standalone"

#: Span names of a standalone task's fields: its run and job are the digitalizer's, not a catalog extraction run.
STANDALONE_ATTRIBUTES = {
    "run_id": "evilflowers.digitalizer.run_id",
    "job": "evilflowers.digitalizer.job",
    "delivery": "evilflowers.job.delivery",
}


def create_app(settings: Settings | None = None) -> Celery:
    settings = settings or get_settings()
    if settings.standalone:
        queues = [settings.standalone_interactive_queue, settings.standalone_queue]
    else:
        queues = [settings.queue]
    return runtime.create_app(
        SERVICE,
        settings,
        include=["evilflowers_analyzer.tasks.extraction", "evilflowers_analyzer.tasks.standalone"],
        queues=queues,
        # The specific pattern first: Celery takes the first glob that matches.
        routes={f"{STANDALONE_PREFIX}.*": settings.standalone_queue, f"{TASK_PREFIX}.*": settings.queue},
        attributes=STANDALONE_ATTRIBUTES if settings.standalone else None,
    )


app = create_app()
