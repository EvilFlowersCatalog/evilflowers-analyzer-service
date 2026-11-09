"""One run end to end, with the catalog, the broker and the layout model replaced by fakes."""

import pytest
import requests

from conftest import FakeCatalog, write_pdf
from evilflowers_analyzer.clients.catalog import RunNotRunning
from evilflowers_analyzer.pipeline import Extraction

REGIONS = {
    "figure": [{"page": 1, "seq": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}, "caption": "Fig. 1"}],
    "table": [],
    "equation": [{"page": 2, "seq": 0, "bbox": {"x0": 0.2, "y0": 0.6, "x1": 0.8, "y1": 0.7}, "caption": None}],
}


@pytest.fixture
def pdf(settings):
    return write_pdf(settings.storage_path / "catalogs/c/e/book.pdf", [(595, 842), (420.5, 595.25)])


def run(settings, catalog, find_regions=None, source="catalogs/c/e/book.pdf"):
    sent: list[tuple[str, dict, str]] = []
    outcome = Extraction(
        "run-1",
        source,
        "catalogs/c/e/extraction/a/v2",
        dispatch=lambda task, kwargs, queue: sent.append((task, kwargs, queue)),
        acquisition_id="acq-1",
        pipeline_version=2,
        settings=settings,
        catalog=catalog,
        find_regions=find_regions or (lambda path, pages: REGIONS),
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

    common = {"run_id": "run-1", "source": "catalogs/c/e/book.pdf", "output_dir": "catalogs/c/e/extraction/a/v2"}
    assert sent == [
        ("evilflowers_text_worker.paragraphs", common, "evilflowers_text_worker"),
        ("evilflowers_image.figures", {**common, "regions": REGIONS["figure"]}, "evilflowers_image"),
        ("evilflowers_equation.equations", {**common, "regions": REGIONS["equation"]}, "evilflowers_equation"),
        # Sent even when empty, so the job reports and the run can finish.
        ("evilflowers_text_worker.tables", {**common, "regions": []}, "evilflowers_text_worker"),
    ]


def test_layout_receives_the_absolute_path_and_page_count(settings, catalog, pdf):
    calls = []
    run(settings, catalog, lambda path, pages: calls.append((path, pages)) or REGIONS)
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


def test_a_layout_failure_fails_the_region_jobs_but_not_paragraphs(settings, catalog, pdf):
    def broken(path, pages):
        raise RuntimeError("model download failed")

    outcome, sent = run(settings, catalog, broken)

    assert outcome == "failed"
    assert [task for task, _kwargs, _queue in sent] == ["evilflowers_text_worker.paragraphs"]
    assert [(job["name"], job["failed"]) for job in catalog.jobs] == [
        ("figures", True),
        ("equations", True),
        ("tables", True),
    ]
    assert all(job["error"] == "Layout failed: model download failed" for job in catalog.jobs)


def test_a_failure_report_the_catalog_refuses_does_not_raise(settings):
    class Refusing(FakeCatalog):
        def job(self, name, **kwargs):
            raise requests.ConnectionError("catalog down")

    outcome, _sent = run(settings, Refusing(), source="missing.pdf")
    assert outcome == "failed"
