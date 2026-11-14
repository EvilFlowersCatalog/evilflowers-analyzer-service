"""One extraction run (IP-018), from the analyzer's side.

The catalog sends ``evilflowers_analyzer.extract`` when a PDF is stored. The
analyzer opens the document, starts the run with its page sizes and the jobs it
is about to dispatch, hands the paragraph job to text-service at once, runs the
layout pass and gives each kind of region to its service. Each service reports
its own job; the catalog finishes the run when all of them have.

Region jobs go out in one of two ways (``DISPATCH_PER_BATCH``):

- per book (default): one ``figures`` / ``equations`` / ``tables`` task each, after
  the whole layout pass;
- per batch (#2): one task per kind for every layout batch, sent as soon as the
  batch is laid out, each its own job named ``<job>:<first>-<last>`` (``equations:0001-0008``)
  and passed to the service as the ``job`` kwarg to report under. A book's
  equations then spread over all equation pods, and a stopped pod loses one batch.

Every task carries the run's ``attempt`` from ``/start`` (EvilFlowersCatalog#140),
so the catalog refuses the results of a pass that a later one superseded.

A run never hangs on the analyzer: every way it can stop reports a failed job,
except a 409 (`RunNotRunning`), which means the catalog no longer wants results.
After ``EXTRACT_MAX_PASSES`` deliveries of the same message (#3) the analyzer gives
up and fails the run instead of starting it again.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pymupdf

from evilflowers_analyzer import layout
from evilflowers_analyzer.clients.catalog import CatalogClient, RunNotRunning
from evilflowers_analyzer.observability import bind, fail, metrics, span
from evilflowers_analyzer.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: ``dispatched``: every job was sent; ``failed``: the analyzer reported failures;
#: ``not_running``: the catalog refused the run (409).
Outcome = Literal["dispatched", "failed", "not_running"]

#: Sends one task: ``(task name, kwargs, queue)``.
Dispatch = Callable[[str, dict[str, Any], str], None]
#: The layout pass, batch by batch (see ``layout.layout_batches``).
LayoutBatches = Callable[[str, int], Iterable[layout.Batch]]

#: The analyzer's own job, reported only when it fails before anything is dispatched.
ANALYZER_JOB = "analyzer"


@dataclass(frozen=True)
class Job:
    """A job of the run: its name in the catalog, the task that does it and that task's queue."""

    name: str
    task: str
    queue: str


def paragraphs_job(settings: Settings) -> Job:
    """Needs only the text layer, so it does not wait for layout."""
    return Job("paragraphs", "evilflowers_text_worker.paragraphs", settings.text_queue)


def region_jobs(settings: Settings) -> dict[str, Job]:
    """The job for each kind of layout region, in the order they are declared and sent."""
    return {
        "figure": Job("figures", "evilflowers_image.figures", settings.image_queue),
        "equation": Job("equations", "evilflowers_equation.equations", settings.equation_queue),
        "table": Job("tables", "evilflowers_text_worker.tables", settings.text_queue),
    }


def batch_job_name(job: Job, first: int, last: int) -> str:
    """The job of one layout batch: ``equations:0001-0008``."""
    return f"{job.name}:{first:04d}-{last:04d}"


def declared_jobs(settings: Settings, page_count: int | None = None) -> list[str]:
    """What ``/start`` declares; the run finishes when every one of them has reported.

    Per batch, every batch of every kind is a job, empty ones included: the batches
    are known from the page count before layout starts.
    """
    jobs = region_jobs(settings).values()
    if not settings.dispatch_per_batch:
        return [paragraphs_job(settings).name] + [job.name for job in jobs]
    ranges = layout.batches(page_count or 0, settings.layout_batch_pages)
    return [paragraphs_job(settings).name] + [
        batch_job_name(job, first, last) for first, last in ranges for job in jobs
    ]


class Extraction:
    def __init__(
        self,
        run_id: str,
        source: str,
        output_dir: str,
        *,
        dispatch: Dispatch,
        acquisition_id: str | None = None,
        pipeline_version: int | None = None,
        passes: int = 1,
        settings: Settings | None = None,
        catalog: CatalogClient | None = None,
        layout_batches: LayoutBatches | None = None,
    ):
        self.settings = settings or get_settings()
        self.run_id = run_id
        self.source = source
        self.output_dir = output_dir
        self.acquisition_id = acquisition_id
        self.pipeline_version = pipeline_version
        self.passes = passes
        self.dispatch = dispatch
        self.catalog = catalog or CatalogClient(run_id, settings=self.settings)
        self.layout_batches = layout_batches or (lambda path, pages: layout.layout_batches(path, pages, self.settings))
        self.attempt: int | None = None

    @property
    def path(self) -> str:
        return str(self.settings.source_path(self.source))

    def run(self) -> Outcome:
        with bind(
            run_id=self.run_id,
            acquisition_id=self.acquisition_id,
            pipeline_version=self.pipeline_version,
            source=self.source,
            extract_pass=self.passes,
        ):
            outcome, stage = self._run()
        metrics.record_run(outcome, stage)
        return outcome

    def _run(self) -> tuple[Outcome, str]:
        """The outcome, and the stage it was decided at (for the metric)."""
        logger.info(
            "Extracting run=%s acquisition=%s source=%s pass=%d",
            self.run_id,
            self.acquisition_id,
            self.source,
            self.passes,
        )

        if self.passes > self.settings.extract_max_passes:
            # Without this, a book that kills the worker restarts the run (and its paragraphs) forever (#3).
            logger.error("Run %s: giving up after %d passes", self.run_id, self.passes - 1)
            self._report_failed(
                [ANALYZER_JOB],
                f"Gave up after {self.passes - 1} passes: the worker died or was stopped during each of them",
            )
            return "failed", "passes"

        try:
            page_sizes = self._page_sizes()
        except Exception as error:
            logger.exception("Cannot open %s", self.path)
            fail(error)
            self._report_failed([ANALYZER_JOB], f"Cannot open the PDF: {error}")
            return "failed", "open"

        try:
            with span("analyzer.catalog.start", **{"evilflowers.document.pages": len(page_sizes)}):
                self.attempt = self.catalog.start(
                    len(page_sizes), page_sizes, declared_jobs(self.settings, len(page_sizes)), self._provenance()
                )
        except RunNotRunning:
            logger.warning("Run %s is not accepting results; skipped", self.run_id)
            return "not_running", "start"
        except Exception as error:
            # A 4xx, or a 5xx after every retry: without a report the run would wait forever.
            logger.exception("Cannot start run %s", self.run_id)
            fail(error)
            self._report_failed([ANALYZER_JOB], f"Cannot start the run: {error}")
            return "failed", "start"

        paragraphs = paragraphs_job(self.settings)
        self.dispatch(paragraphs.task, self._common(), paragraphs.queue)

        with span("analyzer.layout", **{"evilflowers.document.pages": len(page_sizes)}):
            if self.settings.dispatch_per_batch:
                return self._per_batch(len(page_sizes))
            return self._per_book(len(page_sizes))

    def _per_book(self, page_count: int) -> tuple[Outcome, str]:
        jobs = region_jobs(self.settings)
        regions: dict[str, list[dict]] = {kind: [] for kind in jobs}
        try:
            for _first, _last, batch in self.layout_batches(self.path, page_count):
                for kind in jobs:
                    regions[kind].extend(batch.get(kind, []))
        except Exception as error:
            logger.exception("Layout failed for run %s", self.run_id)
            fail(error)
            self._report_failed([job.name for job in jobs.values()], f"Layout failed: {error}")
            return "failed", "layout"

        for kind, job in jobs.items():
            # Sent even when empty, so every declared job reports and the run can finish.
            self._send(job, kind, regions[kind])
        return "dispatched", "dispatch"

    def _per_batch(self, page_count: int) -> tuple[Outcome, str]:
        jobs = region_jobs(self.settings)
        pending = list(layout.batches(page_count, self.settings.layout_batch_pages))
        try:
            for first, last, batch in self.layout_batches(self.path, page_count):
                for kind, job in jobs.items():
                    # Empty ones too: every batch job was declared and must report.
                    self._send(job, kind, batch.get(kind, []), name=batch_job_name(job, first, last))
                pending.remove((first, last))
        except Exception as error:
            # Batches already sent stay with their services; only the ones never sent are failed here.
            logger.exception("Layout failed for run %s", self.run_id)
            fail(error)
            names = [batch_job_name(job, first, last) for first, last in pending for job in jobs.values()]
            self._report_failed(names, f"Layout failed: {error}")
            return "failed", "layout"
        if pending:
            # The layout pass skipped batches it was expected to yield; their jobs would never report.
            names = [batch_job_name(job, first, last) for first, last in pending for job in jobs.values()]
            self._report_failed(names, "Layout returned no result for these pages")
            return "failed", "layout"
        return "dispatched", "dispatch"

    def _common(self) -> dict[str, Any]:
        common: dict[str, Any] = {"run_id": self.run_id, "source": self.source, "output_dir": self.output_dir}
        if self.attempt is not None:
            common["attempt"] = self.attempt
        return common

    def _send(self, job: Job, kind: str, found: list[dict], *, name: str | None = None) -> None:
        kwargs = {**self._common(), "regions": found}
        if name is not None:
            kwargs["job"] = name
        self.dispatch(job.task, kwargs, job.queue)
        metrics.REGIONS.add(len(found), {"kind": kind})
        logger.info("Run %s: %d %s regions to %s (%s)", self.run_id, len(found), kind, job.queue, name or job.name)

    def _page_sizes(self) -> list[list[float]]:
        with span("analyzer.pdf.open"), pymupdf.open(self.path) as document:
            sizes = [[round(page.rect.width, 2), round(page.rect.height, 2)] for page in document]
        metrics.PAGES.record(len(sizes))
        return sizes

    def _provenance(self) -> dict[str, Any]:
        return {"analyzer": {"image": self.settings.image_tag, **layout.versions()}}

    def _report_failed(self, names: Sequence[str], error: str) -> None:
        """Best effort: the failure is already logged and on the span, a second one must not hide it."""
        for name in names:
            try:
                self.catalog.job(name, failed=True, error=error, attempt=self.attempt)
            except RunNotRunning:
                logger.warning("Run %s is not accepting results; job %s not reported", self.run_id, name)
                return
            except Exception:
                logger.exception("Cannot report job %s of run %s as failed", name, self.run_id)
