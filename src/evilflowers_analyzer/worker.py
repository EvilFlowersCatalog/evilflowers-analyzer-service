"""The Celery application: ``celery -A evilflowers_analyzer.worker worker``.

The task name and queue are a contract with the catalog and must not change:

==============================  ==================  ===================
Task                            Queue               Sent by
==============================  ==================  ===================
``evilflowers_analyzer.extract``  ``ANALYZER_QUEUE``  catalog (kwargs)
==============================  ==================  ===================

Everything else (delivery semantics, scale-in, telemetry, JSON logs) is the kit's ``runtime.create_app``
(evilflowers-worker ``docs/architecture.md`` §3.3, §4).
"""

from celery import Celery

from evilflowers_analyzer.settings import Settings, get_settings
from evilflowers_worker import runtime

SERVICE = "analyzer"
TASK_PREFIX = "evilflowers_analyzer"


def create_app(settings: Settings | None = None) -> Celery:
    settings = settings or get_settings()
    return runtime.create_app(
        SERVICE,
        settings,
        include=["evilflowers_analyzer.tasks.extraction"],
        routes={f"{TASK_PREFIX}.*": settings.queue},
    )


app = create_app()
