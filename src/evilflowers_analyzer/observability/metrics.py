"""The worker's metrics, declared once; each is created on first use and is a no-op without OpenTelemetry.

Attributes stay low-cardinality: never a run id or document id (those go on spans and log lines).
In Mimir a name gains its unit and type suffix: ``analyzer.task.duration`` is
``analyzer_task_duration_seconds_bucket``, ``analyzer.regions`` is ``analyzer_regions_total``.
"""

import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from evilflowers_analyzer.observability.otel import SCOPE, metrics

# Seconds. Covers one small layout batch up to the layout pass of a 1000-page book.
DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600)
COUNT_BUCKETS = (0, 1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)

_lock = threading.Lock()


@dataclass(eq=False)
class Instrument:
    kind: Literal["counter", "histogram"]
    name: str
    unit: str
    description: str
    buckets: tuple[float, ...] | None = None
    _created: Any = field(default=None, init=False, repr=False)

    def _get(self):
        if metrics is None:
            return None
        if self._created is None:
            with _lock:
                if self._created is None:
                    # A proxy meter: its instruments become real once `setup()` sets the provider.
                    meter = metrics.get_meter(SCOPE)
                    if self.kind == "counter":
                        self._created = meter.create_counter(self.name, self.unit, self.description)
                    else:
                        self._created = meter.create_histogram(
                            self.name,
                            self.unit,
                            self.description,
                            explicit_bucket_boundaries_advisory=list(self.buckets or DURATION_BUCKETS),
                        )
        return self._created

    def add(self, value: float = 1, attributes: dict[str, Any] | None = None) -> None:
        if (instrument := self._get()) is not None:
            instrument.add(value, attributes or {})

    def record(self, value: float, attributes: dict[str, Any] | None = None) -> None:
        if (instrument := self._get()) is not None:
            instrument.record(value, attributes or {})


TASK_DURATION = Instrument("histogram", "analyzer.task.duration", "s", "Celery task run time")
RUN_OUTCOME = Instrument(
    "counter", "analyzer.run.outcome", "{run}", "Extraction runs the analyzer handled, by outcome and stage"
)
PAGES = Instrument("histogram", "analyzer.document.pages", "{page}", "Pages per document", COUNT_BUCKETS)
LAYOUT_DURATION = Instrument(
    "histogram", "analyzer.layout.duration", "s", "Time of one Docling layout batch, by outcome"
)
LAYOUT_PAGES = Instrument("counter", "analyzer.layout.pages", "{page}", "Pages through the layout model, by outcome")
REGIONS = Instrument("counter", "analyzer.regions", "{region}", "Layout regions dispatched, by kind")
CATALOG_RETRIES = Instrument(
    "counter", "analyzer.catalog.retries", "{retry}", "Retried calls to the catalog ingestion API"
)


def record_run(outcome: str, stage: str) -> None:
    """``outcome``: dispatched, failed or not_running; ``stage``: passes, open, start, layout or dispatch."""
    RUN_OUTCOME.add(1, {"outcome": outcome, "stage": stage})


def record_layout_batch(pages: int, seconds: float, outcome: str) -> None:
    """``outcome``: ok or failed (the batch was skipped)."""
    LAYOUT_DURATION.record(seconds, {"outcome": outcome})
    LAYOUT_PAGES.add(pages, {"outcome": outcome})
