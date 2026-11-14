"""One run end to end, with the catalog, the broker and the layout model replaced by fakes."""

import pytest
import requests

from conftest import FakeCatalog, write_pdf
from evilflowers_analyzer.clients.catalog import RunNotRunning
from evilflowers_analyzer.pipeline import Extraction, declared_jobs

REGIONS = {
    "figure": [{"page": 1, "seq": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}, "caption": "Fig. 1"}],
    "table": [],
    "equation": [{"page": 2, "seq": 0, "bbox": {"x0": 0.2, "y0": 0.6, "x1": 0.8, "y1": 0.7}, "caption": None}],
}
COMMON = {"run_id": "run-1", "source": "catalogs/c/e/book.pdf", "output_dir": "catalogs/c/e/extraction/a/v2"}


@pytest.fixture
def pdf(settings):
    return write_pdf(settings.storage_path / "catalogs/c/e/book.pdf", [(595, 842), (420.5, 595.25)])


def one_batch(path, pages):
    """The whole document as one layout batch."""
    yield 1, pages, REGIONS


def by_page(path, pages):
    """One batch per page, each with the regions found on it."""
    for page in range(1, pages + 1):
        yield page, page, {kind: [r for r in items if r["page"] == page] for kind, items in REGIONS.items()}


def run(settings, catalog, layout_batches=None, source="catalogs/c/e/book.pdf", passes=1):
    sent: list[tuple[str, dict, str]] = []
    outcome = Extraction(
        "run-1",
        source,
        "catalogs/c/e/extraction/a/v2",
        dispatch=lambda task, kwargs, queue: sent.append((task, kwargs, queue)),
        acquisition_id="acq-1",
        pipeline_version=2,
        passes=passes,
        settings=settings,
        catalog=catalog,
        layout_batches=layout_batches or one_batch,
    ).run()
    return outcome, sent


def test_starts_the_run_and_dispatches_every_job(settings, catalog, pdf):
    outcome, sent = run(settings, catalog)

    assert outcome == "dispatched"
    assert catalog.started == [
        {
            "page_count": 2,
            "page_sizes": [[595.0, 842.0], [420.5, 595.25]],
            "jobs": ["paragraphs", "figures", "equations", "tables"],
            "provenance": catalog.started[0]["provenance"],
        }
    ]
    assert catalog.started[0]["provenance"]["analyzer"]["image"] == "test"
    assert catalog.jobs == []

    assert sent == [
        ("evilflowers_text_worker.paragraphs", COMMON, "evilflowers_text_worker"),
        ("evilflowers_image.figures", {**COMMON, "regions": REGIONS["figure"]}, "evilflowers_image"),
        ("evilflowers_equation.equations", {**COMMON, "regions": REGIONS["equation"]}, "evilflowers_equation"),
        # Sent even when empty, so the job reports and the run can finish.
        ("evilflowers_text_worker.tables", {**COMMON, "regions": []}, "evilflowers_text_worker"),
    ]


def test_per_book_collects_every_batch_before_sending(settings, catalog, pdf):
    _outcome, sent = run(settings, catalog, by_page)
    assert [(task, kwargs["regions"]) for task, kwargs, _queue in sent[1:]] == [
        ("evilflowers_image.figures", REGIONS["figure"]),
        ("evilflowers_equation.equations", REGIONS["equation"]),
        ("evilflowers_text_worker.tables", []),
    ]


def test_every_task_carries_the_attempt_of_the_start(settings, pdf):
    catalog = FakeCatalog(attempt=7)
    _outcome, sent = run(settings, catalog)
    assert [kwargs["attempt"] for _task, kwargs, _queue in sent] == [7, 7, 7, 7]


def test_layout_receives_the_absolute_path_and_page_count(settings, catalog, pdf):
    calls = []

    def layout(path, pages):
        calls.append((path, pages))
        return one_batch(path, pages)

    run(settings, catalog, layout)
    assert calls == [(str(pdf), 2)]


def test_an_unreadable_pdf_fails_the_analyzer_job(settings, catalog):
    outcome, sent = run(settings, catalog, source="missing.pdf")

    assert outcome == "failed"
    assert sent == [] and catalog.started == []
    assert [(job["name"], job["failed"]) for job in catalog.jobs] == [("analyzer", True)]
    assert catalog.jobs[0]["error"].startswith("Cannot open the PDF")


def test_a_run_the_catalog_no_longer_accepts_stops_quietly(settings, pdf):
    catalog = FakeCatalog(start_error=RunNotRunning("finished"))
    outcome, sent = run(settings, catalog)

    assert outcome == "not_running"
    assert sent == [] and catalog.jobs == []


def test_a_start_the_catalog_rejects_fails_the_analyzer_job(settings, pdf):
    catalog = FakeCatalog(start_error=requests.HTTPError("400 Bad Request"))
    outcome, sent = run(settings, catalog)

    assert outcome == "failed"
    assert sent == []
    assert [job["name"] for job in catalog.jobs] == ["analyzer"]
    assert "400 Bad Request" in catalog.jobs[0]["error"]


def test_a_layout_failure_fails_the_region_jobs_but_not_paragraphs(settings, pdf):
    def broken(path, pages):
        raise RuntimeError("model download failed")
        yield

    catalog = FakeCatalog(attempt=3)
    outcome, sent = run(settings, catalog, broken)

    assert outcome == "failed"
    assert [task for task, _kwargs, _queue in sent] == ["evilflowers_text_worker.paragraphs"]
    assert [(job["name"], job["failed"], job["attempt"]) for job in catalog.jobs] == [
        ("figures", True, 3),
        ("equations", True, 3),
        ("tables", True, 3),
    ]
    assert all(job["error"] == "Layout failed: model download failed" for job in catalog.jobs)


def test_a_failure_report_the_catalog_refuses_does_not_raise(settings):
    class Refusing(FakeCatalog):
        def job(self, name, **kwargs):
            raise requests.ConnectionError("catalog down")

    outcome, _sent = run(settings, Refusing(), source="missing.pdf")
    assert outcome == "failed"


# -- redeliveries (#3) ------------------------------------------------------------------------------


def test_a_redelivery_within_the_cap_runs_again(settings, catalog, pdf):
    outcome, _sent = run(settings, catalog, passes=settings.extract_max_passes)
    assert outcome == "dispatched"


def test_past_the_cap_the_run_is_failed_instead_of_started_again(settings, catalog, pdf):
    outcome, sent = run(settings, catalog, passes=settings.extract_max_passes + 1)

    assert outcome == "failed"
    assert sent == [] and catalog.started == []
    assert [(job["name"], job["failed"]) for job in catalog.jobs] == [("analyzer", True)]
    assert catalog.jobs[0]["error"].startswith(f"Gave up after {settings.extract_max_passes} passes")


# -- per batch (#2) -----------------------------------------------------------------------------------


@pytest.fixture
def per_batch(settings):
    return settings.model_copy(update={"dispatch_per_batch": True, "layout_batch_pages": 1})


def test_per_batch_declares_every_batch_of_every_kind(per_batch):
    assert declared_jobs(per_batch, 2) == [
        "paragraphs",
        "figures:0001-0001",
        "equations:0001-0001",
        "tables:0001-0001",
        "figures:0002-0002",
        "equations:0002-0002",
        "tables:0002-0002",
    ]
    assert declared_jobs(per_batch.model_copy(update={"layout_batch_pages": 8}), 10)[1:4] == [
        "figures:0001-0008",
        "equations:0001-0008",
        "tables:0001-0008",
    ]


def test_per_batch_sends_each_batch_as_its_own_job(per_batch, pdf):
    catalog = FakeCatalog(attempt=2)
    outcome, sent = run(per_batch, catalog, by_page)

    assert outcome == "dispatched"
    assert catalog.started[0]["jobs"] == declared_jobs(per_batch, 2)
    common = {**COMMON, "attempt": 2}
    assert sent == [
        ("evilflowers_text_worker.paragraphs", common, "evilflowers_text_worker"),
        (
            "evilflowers_image.figures",
            {**common, "regions": REGIONS["figure"], "job": "figures:0001-0001"},
            "evilflowers_image",
        ),
        (
            "evilflowers_equation.equations",
            {**common, "regions": [], "job": "equations:0001-0001"},
            "evilflowers_equation",
        ),
        (
            "evilflowers_text_worker.tables",
            {**common, "regions": [], "job": "tables:0001-0001"},
            "evilflowers_text_worker",
        ),
        ("evilflowers_image.figures", {**common, "regions": [], "job": "figures:0002-0002"}, "evilflowers_image"),
        (
            "evilflowers_equation.equations",
            {**common, "regions": REGIONS["equation"], "job": "equations:0002-0002"},
            "evilflowers_equation",
        ),
        (
            "evilflowers_text_worker.tables",
            {**common, "regions": [], "job": "tables:0002-0002"},
            "evilflowers_text_worker",
        ),
    ]


def test_per_batch_a_layout_failure_fails_only_the_batches_not_sent(per_batch, catalog, pdf):
    def dies_on_page_two(path, pages):
        yield 1, 1, {"figure": [], "table": [], "equation": []}
        raise MemoryError("page 2")

    outcome, sent = run(per_batch, catalog, dies_on_page_two)

    assert outcome == "failed"
    assert [kwargs.get("job") for _task, kwargs, _queue in sent] == [
        None,
        "figures:0001-0001",
        "equations:0001-0001",
        "tables:0001-0001",
    ]
    assert [(job["name"], job["failed"]) for job in catalog.jobs] == [
        ("figures:0002-0002", True),
        ("equations:0002-0002", True),
        ("tables:0002-0002", True),
    ]


def test_per_batch_a_batch_the_layout_skipped_is_failed(per_batch, catalog, pdf):
    def only_page_one(path, pages):
        yield 1, 1, {"figure": [], "table": [], "equation": []}

    outcome, _sent = run(per_batch, catalog, only_page_one)

    assert outcome == "failed"
    assert [job["name"] for job in catalog.jobs] == ["figures:0002-0002", "equations:0002-0002", "tables:0002-0002"]
