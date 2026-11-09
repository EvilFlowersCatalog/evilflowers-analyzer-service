"""One extraction run (IP-018), from the analyzer's side.

The catalog sends ``evilflowers_analyzer.extract`` when a PDF is stored. The
analyzer opens the document, starts the run with its page sizes and the jobs it
is about to dispatch, hands the paragraph job to text-service at once, runs the
layout pass and gives each kind of region to its service. Each service reports
its own job; the catalog finishes the run when all of them have.

A run never hangs on the analyzer: every way it can stop reports a failed job,
except a 409 (`RunNotRunning`), which means the catalog no longer wants results.
"""

import logging
from collections.abc import Callable, Sequence
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
FindRegions = Callable[[str, int], dict[str, list[dict]]]

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


def declared_jobs(settings: Settings) -> list[str]:
    """What ``/start`` declares; the run finishes when every one of them has reported."""
    return [paragraphs_job(settings).name] + [job.name for job in region_jobs(settings).values()]


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
        settings: Settings | None = None,
        catalog: CatalogClient | None = None,
        find_regions: FindRegions | None = None,
    ):
        self.settings = settings or get_settings()
        self.run_id = run_id
        self.source = source
        self.output_dir = output_dir
        self.acquisition_id = acquisition_id
        self.pipeline_version = pipeline_version
        self.dispatch = dispatch
        self.catalog = catalog or CatalogClient(run_id, settings=self.settings)
        self.find_regions = find_regions or (lambda path, pages: layout.find_regions(path, pages, self.settings))

    @property
    def path(self) -> str:
        return str(self.settings.source_path(self.source))

    def run(self) -> Outcome:
        with bind(
            run_id=self.run_id,
            acquisition_id=self.acquisition_id,
            pipeline_version=self.pipeline_version,
            source=self.source,
        ):
            outcome, stage = self._run()
        metrics.record_run(outcome, stage)
        return outcome

    def _run(self) -> tuple[Outcome, str]:
        """The outcome, and the stage it was decided at (for the metric)."""
        logger.info("Extracting run=%s acquisition=%s source=%s", self.run_id, self.acquisition_id, self.source)

        try:
            page_sizes = self._page_sizes()
        except Exception as error:
            logger.exception("Cannot open %s", self.path)
            fail(error)
            self._report_failed([ANALYZER_JOB], f"Cannot open the PDF: {error}")
            return "failed", "open"

        try:
            with span("analyzer.catalog.start", **{"evilflowers.document.pages": len(page_sizes)}):
                self.catalog.start(len(page_sizes), page_sizes, declared_jobs(self.settings), self._provenance())
        except RunNotRunning:
            logger.warning("Run %s is not accepting results; skipped", self.run_id)
            return "not_running", "start"
        except Exception as error:
            # A 4xx, or a 5xx after every retry: without a report the run would wait forever.
            logger.exception("Cannot start run %s", self.run_id)
            fail(error)
            self._report_failed([ANALYZER_JOB], f"Cannot start the run: {error}")
            return "failed", "start"

        common = {"run_id": self.run_id, "source": self.source, "output_dir": self.output_dir}
        paragraphs = paragraphs_job(self.settings)
        self.dispatch(paragraphs.task, common, paragraphs.queue)

        jobs = region_jobs(self.settings)
        try:
            with span("analyzer.layout", **{"evilflowers.document.pages": len(page_sizes)}):
                regions = self.find_regions(self.path, len(page_sizes))
        except Exception as error:
            logger.exception("Layout failed for run %s", self.run_id)
            fail(error)
            self._report_failed([job.name for job in jobs.values()], f"Layout failed: {error}")
            return "failed", "layout"

        for kind, job in jobs.items():
            found = regions.get(kind, [])
            # Sent even when empty, so every declared job reports and the run can finish.
            self.dispatch(job.task, {**common, "regions": found}, job.queue)
            metrics.REGIONS.add(len(found), {"kind": kind})
            logger.info("Run %s: %d %s regions to %s", self.run_id, len(found), kind, job.queue)
        return "dispatched", "dispatch"

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
                self.catalog.job(name, failed=True, error=error)
            except RunNotRunning:
                logger.warning("Run %s is not accepting results; job %s not reported", self.run_id, name)
                return
            except Exception:
                logger.exception("Cannot report job %s of run %s as failed", name, self.run_id)
