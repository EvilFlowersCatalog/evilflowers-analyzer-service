"""One run end to end, with the catalog, the broker and the layout model replaced by fakes."""

from types import SimpleNamespace

import pymupdf
import pytest
import requests

from conftest import write_pdf
from evilflowers_analyzer.pipeline import Extraction, declared_jobs
from evilflowers_worker.catalog import CatalogClient, RunNotRunning
from evilflowers_worker.contract import ExtractRequest
from evilflowers_worker.testing import FakeCatalog, RecordingPublisher

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
    publisher = RecordingPublisher()
    request = ExtractRequest(
        run_id="run-1",
        source=source,
        output_dir="catalogs/c/e/extraction/a/v2",
        acquisition_id="acq-1",
        pipeline_version=2,
    )
    outcome = Extraction(
        request,
        publisher=publisher,
        delivery=passes,
        settings=settings,
        catalog=catalog,
        layout_batches=layout_batches or one_batch,
    ).run()
    return outcome, publisher.sent


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
    # One line of body text per page: no outline, and the catalog is told so (#5).
    assert catalog.started[0]["provenance"]["analyzer"]["outline_source"] == "none"
    assert catalog.started[0]["provenance"]["analyzer"]["outline_entries"] == 0
    assert catalog.jobs == []

    assert sent == [
        ("evilflowers_text_worker.paragraphs", {**COMMON, "outline": []}, "evilflowers.text.paragraphs"),
        ("evilflowers_image.figures", {**COMMON, "regions": REGIONS["figure"]}, "evilflowers_image"),
        ("evilflowers_equation.equations", {**COMMON, "regions": REGIONS["equation"]}, "evilflowers_equation"),
        # Sent even when empty, so the job reports and the run can finish.
        ("evilflowers_text_worker.tables", {**COMMON, "regions": []}, "evilflowers.text.tables"),
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


class RecordingSession:
    """The HTTP session under the real ``CatalogClient``: answers ``/start`` with attempt 6, records every call."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    def request(self, method, url, json=None, params=None, timeout=None):
        self.calls.append((method, url, json, params))
        body = {"response": {"status": "running", "attempt": 6}} if url.endswith("/start") else {}
        return SimpleNamespace(status_code=200, content=b"{}", json=lambda: body, raise_for_status=lambda: None)


def test_the_real_client_puts_the_attempt_of_the_start_on_every_report(settings, pdf):
    """The seam between the coordinator and the catalog client, with only the HTTP session faked."""
    http = RecordingSession()

    def broken(path, pages):
        raise RuntimeError("model download failed")
        yield

    outcome, sent = run(settings, CatalogClient("run-1", settings=settings, http=http), broken)

    assert outcome == "failed"
    method, url, payload, params = http.calls[0]
    assert (method, url, params) == ("POST", "http://catalog:8000/extraction/v1/runs/run-1/start", None)
    assert payload["jobs"] == ["paragraphs", "figures", "equations", "tables"]
    assert payload["page_sizes"] == [[595.0, 842.0], [420.5, 595.25]]
    assert [(m, u.rsplit("/", 1)[-1], p) for m, u, _b, p in http.calls[1:]] == [
        ("POST", "figures", {"attempt": 6}),
        ("POST", "equations", {"attempt": 6}),
        ("POST", "tables", {"attempt": 6}),
    ]
    assert http.calls[1][2] == {"status": "failed", "error": "Layout failed: model download failed", "provenance": {}}
    assert sent[0][1]["attempt"] == 6


def test_the_outline_goes_to_paragraphs_and_its_source_to_the_provenance(settings, catalog):
    path = settings.storage_path / "catalogs/c/e/book.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        for _ in range(3):
            pdf.new_page().insert_text((40, 60), "Entropy measures the uncertainty of a random variable.")
        pdf.set_toc([[1, "1 Entropy", 1], [2, "1.1 Coding", 3]])
        pdf.save(path)

    _outcome, sent = run(settings, catalog)

    assert sent[0][1]["outline"] == [[1, "1 Entropy", 1], [2, "1.1 Coding", 3]]
    assert catalog.started[0]["provenance"]["analyzer"]["outline_source"] == "bookmarks"
    assert catalog.started[0]["provenance"]["analyzer"]["outline_entries"] == 2


class DeciderHttp:
    """The HTTP session under the kit's ``DeciderClient``: "no" for a line starting ``Figure``, else "yes"."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None):
        self.requests.append((url, json))
        results = [
            {"answers": {"section_heading": {"noul": 0.02 if state["line"].startswith("Figure") else 0.95}}}
            for state in json["states"]
        ]
        body = {"model": "laya-multilingual", "results": results}
        return SimpleNamespace(status_code=200, ok=True, json=lambda: body, text="")


def letters(index: int) -> str:
    """``1`` → ``a``, ``27`` → ``aa``: distinct titles that digits alone would not make (running-head check)."""
    name = ""
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(ord("a") + rest) + name
    return name


def headings_pdf(settings, count: int):
    """No bookmarks; one heading per page set larger than the body, every third a figure label set like one."""
    path = settings.storage_path / "catalogs/c/e/book.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        for index in range(1, count + 1):
            page = pdf.new_page(width=420, height=600)
            kind = "Figure" if index % 3 == 0 else "Section"
            title = f"{kind} {letters(index)}"
            page.insert_text((40, 80), title, fontsize=14, fontname="hebo")
            for row in range(20):
                page.insert_text((40, 110 + row * 14), "Entropy measures the uncertainty of a variable.", fontsize=10)
        pdf.save(path)


def judged_run(settings, catalog, **judge_settings):
    from evilflowers_analyzer.settings import Settings
    from evilflowers_worker.judges import Judges

    settings = Settings(
        _env_file=None, storage_path=settings.storage_path, catalog_attempts=2, image_tag="test",
        judges_enabled=True, decider_url="http://decider", decider_revision="r1", **judge_settings,
    )  # fmt: skip
    judges = Judges.from_settings(settings)
    http = DeciderHttp()
    judges.decider.http = http
    publisher = RecordingPublisher()
    request = ExtractRequest(run_id="run-1", source="catalogs/c/e/book.pdf", output_dir="catalogs/c/e/extraction/a/v2")
    Extraction(
        request, publisher=publisher, settings=settings, catalog=catalog, layout_batches=one_batch, judges=judges
    ).run()
    return http, publisher.sent, catalog.started[0]["provenance"]["analyzer"]


def test_detected_headings_are_asked_of_decider_64_per_request(settings, catalog):
    headings_pdf(settings, 130)

    http, sent, provenance = judged_run(settings, catalog)

    assert [(url, len(body["states"])) for url, body in http.requests] == [
        ("http://decider/v1/systemone/batch", 64),
        ("http://decider/v1/systemone/batch", 64),
        ("http://decider/v1/systemone/batch", 2),
    ]
    assert http.requests[0][1]["model"] == "multilingual"
    assert set(http.requests[0][1]["questions"]) == {"section_heading"}
    # No thresholds yet (knowledge#65): every candidate goes to text-service, its verdict to the provenance.
    assert len(sent[0][1]["outline"]) == 130
    assert provenance["outline_entries"] == 130
    assert len(provenance["outline_decisions"]) == 130
    assert provenance["outline_decisions"][2] == {
        "page": 3,
        "title": "Figure c",
        "decision": "no",
        "p": 0.98,
        "decider_model": "laya-multilingual@r1",
    }


def test_with_thresholds_rejected_headings_leave_the_outline(settings, catalog):
    headings_pdf(settings, 6)

    _http, sent, provenance = judged_run(
        settings, catalog, judge_thresholds={"section_heading": {"keep": 0.8, "hide": 0.9}}
    )

    assert [title for _level, title, _page in sent[0][1]["outline"]] == [
        "Section a",
        "Section b",
        "Section d",
        "Section e",
    ]
    assert [d["band"] for d in provenance["outline_decisions"]] == ["keep", "keep", "hide", "keep", "keep", "hide"]


def test_a_book_with_bookmarks_asks_decider_nothing(settings, catalog, pdf):
    with pymupdf.open(pdf) as document:
        document.set_toc([[1, "Entropy", 1]])
        document.saveIncr()

    http, sent, provenance = judged_run(settings, catalog)

    assert http.requests == []
    assert sent[0][1]["outline"] == [[1, "Entropy", 1]]
    assert "outline_decisions" not in provenance


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
    unreachable = FakeCatalog(fail_on={"job": requests.ConnectionError("catalog down")})
    outcome, _sent = run(settings, unreachable, source="missing.pdf")
    assert outcome == "failed"


# -- redeliveries (#3) ------------------------------------------------------------------------------


def test_a_redelivery_within_the_cap_runs_again(settings, catalog, pdf):
    outcome, _sent = run(settings, catalog, passes=settings.max_deliveries)
    assert outcome == "dispatched"


def test_past_the_cap_the_run_is_failed_instead_of_started_again(settings, catalog, pdf):
    outcome, sent = run(settings, catalog, passes=settings.max_deliveries + 1)

    assert outcome == "failed"
    assert sent == [] and catalog.started == []
    assert [(job["name"], job["failed"]) for job in catalog.jobs] == [("analyzer", True)]
    assert catalog.jobs[0]["error"].startswith(f"Gave up after {settings.max_deliveries} passes")


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
        ("evilflowers_text_worker.paragraphs", {**common, "outline": []}, "evilflowers.text.paragraphs"),
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
            "evilflowers.text.tables",
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
            "evilflowers.text.tables",
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
