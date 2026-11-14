"""The Celery application: ``celery -A evilflowers_analyzer.worker worker``.

The task name and queue are a contract with the catalog and must not change:

==============================  ==================  ===================
Task                            Queue               Sent by
==============================  ==================  ===================
``evilflowers_analyzer.extract``  ``ANALYZER_QUEUE``  catalog (kwargs)
==============================  ==================  ===================

Nobody waits for a task's result (each job reports to the catalog), so there is no
result backend.
"""

from celery import Celery

from evilflowers_analyzer.observability.signals import connect
from evilflowers_analyzer.settings import Settings, get_settings

TASK_PREFIX = "evilflowers_analyzer"


class CeleryConfig:
    """Shared with the sibling extraction services (text, image, equation) where it can be."""

    def __init__(self, settings: Settings):
        self.broker_url = settings.redis_url
        self.broker_connection_retry_on_startup = True
        # `extract` acknowledges late; Redis must not redeliver a book still in its layout pass.
        self.broker_transport_options = {"visibility_timeout": settings.broker_visibility_timeout}
        # Scale-in: SIGTERM (remapped to SIGQUIT in the image) cancels the book after this grace and its
        # message goes straight back on the queue, instead of after the visibility timeout.
        self.worker_soft_shutdown_timeout = settings.worker_soft_shutdown_timeout
        self.worker_enable_soft_shutdown_on_idle = settings.worker_enable_soft_shutdown_on_idle

        self.task_serializer = "json"
        self.result_serializer = "json"
        self.accept_content = ["json"]
        self.timezone = "Europe/Bratislava"
        self.enable_utc = True

        self.task_default_queue = settings.queue
        self.task_routes = {f"{TASK_PREFIX}.*": {"queue": settings.queue}}
        # A document takes minutes: one at a time per worker process.
        self.worker_prefetch_multiplier = 1
        # Monitoring (Flower, `celery events`).
        self.worker_send_task_events = True
        self.task_send_sent_event = True


def create_app(settings: Settings | None = None) -> Celery:
    settings = settings or get_settings()
    app = Celery("evilflowers_analyzer", include=["evilflowers_analyzer.tasks.extraction"])
    app.config_from_object(CeleryConfig(settings))
    return app


# Traces, metrics, profiles and JSON logs; off unless the pod points them at a collector.
connect()
app = create_app()
