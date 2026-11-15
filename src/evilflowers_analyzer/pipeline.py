"""One extraction run (IP-018), from the analyzer's side: the coordinator (evilflowers-worker architecture.md §2).

The catalog sends ``evilflowers_analyzer.extract`` (``ExtractRequest``) when a PDF is stored. The analyzer opens the
document, reads its outline, starts the run (``/start``) with its page sizes and the jobs it is about to send, hands
the paragraph job to text-service at once, runs the layout pass and gives each kind of region to its service. Each
service reports its own job; the catalog finishes the run when all of them have.

Region jobs go out in one of two ways (``DISPATCH_PER_BATCH``):

- per book (default): one ``figures`` / ``equations`` / ``tables`` message each, after the whole layout pass;
- per batch (#2): one message per kind for every layout batch, sent as soon as the batch is laid out, each its own job
  (``equations:0001-0008``). A book's equations then spread over all equation pods, and a stopped pod loses one batch.

Every message carries the run's ``attempt`` from ``/start`` (EvilFlowersCatalog#140), so the catalog refuses the
results of a pass that a later one superseded.

A run never hangs on the analyzer: every way it can stop reports a failed job, except a 409 (``RunNotRunning``),
which means the catalog no longer wants results. After ``EXTRACT_MAX_PASSES`` deliveries of the same message (#3) the
analyzer gives up and fails the run instead of starting it again.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pymupdf

from evilflowers_analyzer import layout, metrics, outline
from evilflowers_analyzer.settings import Settings, get_settings
from evilflowers_worker.catalog import CatalogClient, RunNotRunning
from evilflowers_worker.contract import (
    ExtractRequest,
    JobReport,
    LayoutRegion,
    ParagraphsJob,
    RegionsJob,
    Route,
    StartRun,
    batch_job,
)
from evilflowers_worker.deliveries import capped
from evilflowers_worker.observability import annotate, bind, fail, span
from evilflowers_worker.observability import metrics as standard_metrics
from evilflowers_worker.runtime import Publisher

logger = logging.getLogger(__name__)

#: ``dispatched``: every job was sent; ``failed``: the analyzer reported failures;
#: ``not_running``: the catalog refused the run (409).
Outcome = Literal["dispatched", "failed", "not_running"]

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

    @property
    def route(self) -> Route:
        return Route(self.task, self.queue)


def paragraphs_job(settings: Settings) -> Job:
    """Needs only the text layer, so it does not wait for layout."""
    return Job("paragraphs", "evilflowers_text_worker.paragraphs", settings.text_paragraphs_queue)


def region_jobs(settings: Settings) -> dict[str, Job]:
    """The job for each kind of layout region, in the order they are declared and sent."""
    return {
        "figure": Job("figures", "evilflowers_image.figures", settings.image_queue),
        "equation": Job("equations", "evilflowers_equation.equations", settings.equation_queue),
        "table": Job("tables", "evilflowers_text_worker.tables", settings.text_tables_queue),
    }


def declared_jobs(settings: Settings, page_count: int | None = None) -> list[str]:
    """What ``/start`` declares; the run finishes when every one of them has reported.

    Per batch, every batch of every kind is a job, empty ones included: the batches are known from the page count
    before layout starts.
    """
    jobs = region_jobs(settings).values()
    if not settings.dispatch_per_batch:
        return [paragraphs_job(settings).name] + [job.name for job in jobs]
    ranges = layout.batches(page_count or 0, settings.layout_batch_pages)
    batch_jobs = [batch_job(job.name, first, last) for first, last in ranges for job in jobs]
    return [paragraphs_job(settings).name, *batch_jobs]


class Extraction:
    def __init__(
        self,
        request: ExtractRequest,
        *,
        publisher: Publisher,
        delivery: int = 1,
        settings: Settings | None = None,
        catalog: CatalogClient | None = None,
        layout_batches: LayoutBatches | None = None,
    ):
        self.settings = settings or get_settings()
        self.request = request
        self.publisher = publisher
        self.delivery = delivery
        self.catalog = catalog or CatalogClient(request.run_id, settings=self.settings)
        self.layout_batches = layout_batches or (lambda path, pages: layout.layout_batches(path, pages, self.settings))
        self.attempt: int | None = None
        self.outline = outline.Outline("none")

    @property
    def run_id(self) -> str:
        return self.request.run_id

    @property
    def path(self) -> str:
        return str(self.settings.source_path(self.request.source))

    def run(self) -> Outcome:
        with bind(
            run_id=self.run_id,
            acquisition_id=self.request.acquisition_id,
            pipeline_version=self.request.pipeline_version,
            source=self.request.source,
            extract_pass=self.delivery,
            delivery=self.delivery,
        ):
            outcome, stage = self._run()
        metrics.record_run(outcome, stage)
        standard_metrics.record_job(ANALYZER_JOB, outcome)
        return outcome

    def _run(self) -> tuple[Outcome, str]:
        """The outcome, and the stage it was decided at (for the metric)."""
        logger.info(
            "Extracting run=%s acquisition=%s source=%s pass=%d",
            self.run_id,
            self.request.acquisition_id,
            self.request.source,
            self.delivery,
        )

        if capped(self.delivery, self.settings.max_deliveries):
            # Without this, a book that kills the worker restarts the run (and its paragraphs) forever (#3).
            logger.error("Run %s: giving up after %d passes", self.run_id, self.delivery - 1)
            self._report_failed(
                [ANALYZER_JOB],
                f"Gave up after {self.delivery - 1} passes: the worker died or was stopped during each of them",
            )
            return "failed", "passes"

        try:
            page_sizes = self._open()
        except Exception as error:
            logger.exception("Cannot open %s", self.path)
            fail(error)
            self._report_failed([ANALYZER_JOB], f"Cannot open the PDF: {error}")
            return "failed", "open"

        try:
            start = StartRun(
                page_count=len(page_sizes),
                page_sizes=page_sizes,
                jobs=declared_jobs(self.settings, len(page_sizes)),
                provenance=self._provenance(),
            )
            with span("analyzer.catalog.start", **{"evilflowers.document.pages": len(page_sizes)}):
                self.attempt = self.catalog.start(start).attempt
        except RunNotRunning:
            logger.warning("Run %s is not accepting results; skipped", self.run_id)
            return "not_running", "start"
        except Exception as error:
            # A 4xx, a 5xx after every retry, or a start the contract refuses (a job name the catalog would reject):
            # without a report the run would wait forever.
            logger.exception("Cannot start run %s", self.run_id)
            fail(error)
            self._report_failed([ANALYZER_JOB], f"Cannot start the run: {error}")
            return "failed", "start"

        with bind(attempt=self.attempt):
            paragraphs = paragraphs_job(self.settings)
            # The headings the paragraphs fall under (#5). When present (even `[]`) it is authoritative; left out
            # when the analyzer could not read the outline, so text-service reads the bookmarks itself.
            entries = self.outline.entries if self.outline.source != "failed" else None
            self.publisher.publish(paragraphs.route, ParagraphsJob(**self._common(), outline=entries))

            with span("analyzer.layout", **{"evilflowers.document.pages": len(page_sizes)}):
                if self.settings.dispatch_per_batch:
                    return self._per_batch(len(page_sizes))
                return self._per_book(len(page_sizes))

    def _per_book(self, page_count: int) -> tuple[Outcome, str]:
        jobs = region_jobs(self.settings)
        regions: dict[str, list[dict[str, Any]]] = {kind: [] for kind in jobs}
        try:
            for _first, _last, batch in self.layout_batches(self.path, page_count):
                for kind in jobs:
                    regions[kind].extend(batch.get(kind, []))
            # Built before any is sent: a region the contract refuses fails all three, never a part of them.
            messages = {kind: self._message(regions[kind]) for kind in jobs}
        except Exception as error:
            logger.exception("Layout failed for run %s", self.run_id)
            fail(error)
            self._report_failed([job.name for job in jobs.values()], f"Layout failed: {error}")
            return "failed", "layout"

        for kind, job in jobs.items():
            # Sent even when empty, so every declared job reports and the run can finish.
            self._send(job, kind, messages[kind])
        return "dispatched", "dispatch"

    def _per_batch(self, page_count: int) -> tuple[Outcome, str]:
        jobs = region_jobs(self.settings)
        pending = list(layout.batches(page_count, self.settings.layout_batch_pages))
        try:
            for first, last, batch in self.layout_batches(self.path, page_count):
                messages = {
                    kind: self._message(batch.get(kind, []), job=batch_job(job.name, first, last))
                    for kind, job in jobs.items()
                }
                for kind, job in jobs.items():
                    # Empty ones too: every batch job was declared and must report.
                    self._send(job, kind, messages[kind])
                pending.remove((first, last))
        except Exception as error:
            # Batches already sent stay with their services; only the ones never sent are failed here.
            logger.exception("Layout failed for run %s", self.run_id)
            fail(error)
            self._report_failed(self._batch_names(jobs, pending), f"Layout failed: {error}")
            return "failed", "layout"
        if pending:
            # The layout pass skipped batches it was expected to yield; their jobs would never report.
            self._report_failed(self._batch_names(jobs, pending), "Layout returned no result for these pages")
            return "failed", "layout"
        return "dispatched", "dispatch"

    @staticmethod
    def _batch_names(jobs: dict[str, Job], pages: Sequence[tuple[int, int]]) -> list[str]:
        return [batch_job(job.name, first, last) for first, last in pages for job in jobs.values()]

    def _common(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source": self.request.source,
            "output_dir": self.request.output_dir,
            "attempt": self.attempt,
        }

    def _message(self, found: list[dict[str, Any]], *, job: str | None = None) -> RegionsJob:
        return RegionsJob(**self._common(), job=job, regions=[LayoutRegion.model_validate(r) for r in found])

    def _send(self, job: Job, kind: str, message: RegionsJob) -> None:
        self.publisher.publish(job.route, message)
        found = len(message.regions)
        metrics.REGIONS.add(found, {"kind": kind})
        logger.info("Run %s: %d %s regions to %s (%s)", self.run_id, found, kind, job.queue, message.job or job.name)

    def _open(self) -> list[list[float]]:
        """The page sizes; also reads the outline (``self.outline``), which never fails the run."""
        with span("analyzer.pdf.open"), pymupdf.open(self.path) as document:
            sizes = [[round(page.rect.width, 2), round(page.rect.height, 2)] for page in document]
            self.outline = outline.find(document)
            annotate(
                **{
                    "evilflowers.outline.source": self.outline.source,
                    "evilflowers.outline.entries": len(self.outline.entries),
                }
            )
        metrics.PAGES.record(len(sizes))
        logger.info("Run %s: outline from %s, %d entries", self.run_id, self.outline.source, len(self.outline.entries))
        return sizes

    def _provenance(self) -> dict[str, Any]:
        return {
            "analyzer": {
                "image": self.settings.image_tag,
                **layout.versions(),
                # `none` is a book without an outline, `failed` one the analyzer could not read (#5).
                "outline_source": self.outline.source,
                "outline_entries": len(self.outline.entries),
            }
        }

    def _report_failed(self, names: Sequence[str], error: str) -> None:
        """Best effort: the failure is already logged and on the span, a second one must not hide it."""
        report = JobReport(status="failed", error=error)
        for name in names:
            try:
                self.catalog.job(name, report)
            except RunNotRunning:
                logger.warning("Run %s is not accepting results; job %s not reported", self.run_id, name)
                return
            except Exception:
                logger.exception("Cannot report job %s of run %s as failed", name, self.run_id)
