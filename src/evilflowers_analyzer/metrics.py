"""The analyzer's own metrics, next to the kit's standard set (``analyzer_task_duration_seconds``,
``analyzer_job_outcome_total``, ``analyzer_catalog_retries_total``, ``analyzer_deliveries_capped_total``).

Names keep what the dashboards query: ``analyzer.run.outcome`` is ``analyzer_run_outcome_total`` in Mimir.
"""

from evilflowers_worker.observability.metrics import COUNT_BUCKETS, Instrument

#: Seconds: one small layout batch up to the layout pass of a 1000-page book.
LAYOUT_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600)

RUN_OUTCOME = Instrument(
    "counter", "analyzer.run.outcome", "{run}", "Extraction runs the analyzer handled, by outcome and stage"
)
PAGES = Instrument("histogram", "analyzer.document.pages", "{page}", "Pages per document", COUNT_BUCKETS)
LAYOUT_DURATION = Instrument(
    "histogram", "analyzer.layout.duration", "s", "Time of one Docling layout batch, by outcome", LAYOUT_BUCKETS
)
LAYOUT_PAGES = Instrument("counter", "analyzer.layout.pages", "{page}", "Pages through the layout model, by outcome")
REGIONS = Instrument("counter", "analyzer.regions", "{region}", "Layout regions dispatched, by kind")


def record_run(outcome: str, stage: str) -> None:
    """``outcome``: dispatched, failed or not_running; ``stage``: input, passes, open, start, layout or dispatch."""
    RUN_OUTCOME.add(1, {"outcome": outcome, "stage": stage})


def record_layout_batch(pages: int, seconds: float, outcome: str) -> None:
    """``outcome``: ok or failed (the batch was skipped)."""
    LAYOUT_DURATION.record(seconds, {"outcome": outcome})
    LAYOUT_PAGES.add(pages, {"outcome": outcome})
