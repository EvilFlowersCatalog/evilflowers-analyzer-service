"""Step re-run (#7, EvilFlowersCatalog#183): ``extract(steps=[...])`` restarts and sends only the named steps."""

from types import SimpleNamespace

import pytest
import requests

from conftest import write_pdf
from evilflowers_analyzer.pipeline import Extraction, declared_jobs
from evilflowers_worker.catalog import CatalogClient, RunNotRunning
from evilflowers_worker.contract import ExtractRequest
from evilflowers_worker.testing import FakeCatalog, RecordingPublisher

REGIONS = {
    "figure": [{"page": 1, "seq": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}, "caption": "Fig. 1"}],
    "table": [{"page": 2, "seq": 0, "bbox": {"x0": 0.2, "y0": 0.2, "x1": 0.9, "y1": 0.6}, "caption": None}],
    "equation": [{"page": 2, "seq": 0, "bbox": {"x0": 0.2, "y0": 0.6, "x1": 0.8, "y1": 0.7}, "caption": None}],
}


@pytest.fixture
def pdf(settings):
    return write_pdf(settings.storage_path / "catalogs/c/e/book.pdf", [(595, 842), (420.5, 595.25)])


def by_page(path, pages):
    for page in range(1, pages + 1):
        yield page, page, {kind: [r for r in items if r["page"] == page] for kind, items in REGIONS.items()}


def run(settings, catalog, steps, layout_batches=by_page, source="catalogs/c/e/book.pdf", passes=1):
    publisher = RecordingPublisher()
    request = ExtractRequest(
        run_id="run-1", source=source, output_dir="catalogs/c/e/extraction/a/v2", pipeline_version=2, steps=steps
    )
    outcome = Extraction(
        request,
        publisher=publisher,
        delivery=passes,
        settings=settings,
        catalog=catalog,
        layout_batches=layout_batches,
    ).run()
    return outcome, publisher.sent


# -- what is declared --------------------------------------------------------------------------------


def test_a_full_pass_declares_every_job(settings):
    every = ["paragraphs", "figures", "equations", "tables"]
    assert declared_jobs(settings, 2, []) == declared_jobs(settings, 2) == every


def test_step_mode_declares_only_the_named_steps(settings):
    assert declared_jobs(settings, 2, ["paragraphs", "tables"]) == ["paragraphs", "tables"]
    assert declared_jobs(settings, 2, ["equations", "figures"]) == ["figures", "equations"]


def test_batch_names_follow_the_current_batching(settings):
    per_batch = settings.model_copy(update={"dispatch_per_batch": True, "layout_batch_pages": 8})
    named = ["paragraphs", "tables"]
    assert declared_jobs(per_batch, 10, named) == ["paragraphs", "tables:0001-0008", "tables:0009-0010"]
    smaller = per_batch.model_copy(update={"layout_batch_pages": 4})
    assert declared_jobs(smaller, 10, ["tables"]) == ["tables:0001-0004", "tables:0005-0008", "tables:0009-0010"]


# -- what is started and sent ------------------------------------------------------------------------


def test_step_start_names_the_steps_and_only_their_jobs(settings, pdf):
    catalog = FakeCatalog(attempt=4)
    outcome, sent = run(settings, catalog, ["tables", "paragraphs"])

    assert outcome == "dispatched"
    assert catalog.started[0]["steps"] == ["paragraphs", "tables"]
    assert catalog.started[0]["jobs"] == ["paragraphs", "tables"]
    assert catalog.started[0]["page_sizes"] == [[595.0, 842.0], [420.5, 595.25]]
    assert [task for task, _kwargs, _queue in sent] == [
        "evilflowers_text_worker.paragraphs",
        "evilflowers_text_worker.tables",
    ]
    assert sent[1][1]["regions"] == REGIONS["table"]
    assert all(kwargs["attempt"] == 4 for _task, kwargs, _queue in sent)
    assert catalog.jobs == []


def test_a_full_pass_sends_no_steps_field(settings, pdf):
    catalog = FakeCatalog()
    run(settings, catalog, [])
    assert "steps" not in catalog.started[0]


def test_step_mode_per_batch_sends_only_the_named_kinds(settings, pdf):
    per_batch = settings.model_copy(update={"dispatch_per_batch": True, "layout_batch_pages": 1})
    catalog = FakeCatalog(attempt=2)
    outcome, sent = run(per_batch, catalog, ["figures"])

    assert outcome == "dispatched"
    assert catalog.started[0]["jobs"] == ["figures:0001-0001", "figures:0002-0002"]
    assert [(task, kwargs["job"]) for task, kwargs, _queue in sent] == [
        ("evilflowers_image.figures", "figures:0001-0001"),
        ("evilflowers_image.figures", "figures:0002-0002"),
    ]


def test_paragraphs_alone_runs_no_layout(settings, pdf):
    def never(path, pages):
        raise AssertionError("the layout pass must not run")

    catalog = FakeCatalog(attempt=3)
    outcome, sent = run(settings, catalog, ["paragraphs"], never)

    assert outcome == "dispatched"
    assert catalog.started[0]["jobs"] == ["paragraphs"]
    assert [task for task, _kwargs, _queue in sent] == ["evilflowers_text_worker.paragraphs"]


def test_a_layout_failure_fails_only_the_named_kinds_with_the_attempt(settings, pdf):
    def broken(path, pages):
        raise RuntimeError("model download failed")
        yield

    catalog = FakeCatalog(attempt=5)
    outcome, sent = run(settings, catalog, ["paragraphs", "tables"], broken)

    assert outcome == "failed"
    assert [task for task, _kwargs, _queue in sent] == ["evilflowers_text_worker.paragraphs"]
    assert [(job["name"], job["failed"], job["attempt"]) for job in catalog.jobs] == [("tables", True, 5)]


# -- nothing is reported before a successful step /start --------------------------------------------


class ConflictSession:
    """The HTTP session under the real ``CatalogClient``: ``/start`` answers 409 with ``reason``."""

    def __init__(self, reason):
        self.reason = reason
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, json=None, params=None, timeout=None):
        self.calls.append((method, url))
        body = {"reason": self.reason}
        return SimpleNamespace(status_code=409, content=b"{}", text=self.reason, json=lambda: body)


@pytest.mark.parametrize("reason", ["not_running", "steps_refused"])
def test_a_409_on_the_step_start_reports_nothing(settings, pdf, reason):
    http = ConflictSession(reason)
    outcome, sent = run(settings, CatalogClient("run-1", settings=settings, http=http), ["tables"])

    assert outcome == "not_running"
    assert sent == []
    assert [url.rsplit("/", 1)[-1] for _method, url in http.calls] == ["start"]


def test_a_start_error_in_step_mode_reports_nothing(settings, pdf):
    catalog = FakeCatalog(start_error=requests.HTTPError("400 Bad Request"))
    outcome, sent = run(settings, catalog, ["tables"])
    assert outcome == "failed"
    assert sent == [] and catalog.jobs == []


def test_an_unreadable_pdf_in_step_mode_reports_nothing(settings):
    catalog = FakeCatalog()
    outcome, _sent = run(settings, catalog, ["tables"], source="missing.pdf")
    assert outcome == "failed"
    assert catalog.started == [] and catalog.jobs == []


def test_past_the_cap_step_mode_reports_nothing(settings, pdf):
    catalog = FakeCatalog()
    outcome, _sent = run(settings, catalog, ["tables"], passes=settings.max_deliveries + 1)
    assert outcome == "failed"
    assert catalog.started == [] and catalog.jobs == []


def test_a_run_not_running_is_still_mapped_from_steps_refused(settings):
    """The kit maps an unknown 409 reason to ``RunNotRunning``; the analyzer relies on it for ``steps_refused``."""
    http = ConflictSession("steps_refused")
    with pytest.raises(RunNotRunning):
        CatalogClient("run-1", settings=settings, http=http).run()


# -- the task -----------------------------------------------------------------------------------------


def test_the_task_takes_steps(settings, monkeypatch):
    import inspect

    from evilflowers_analyzer.worker import app

    app.loader.import_default_modules()
    assert "steps" in inspect.signature(app.tasks["evilflowers_analyzer.extract"].run).parameters


def test_an_invalid_step_message_reports_nothing(monkeypatch):
    from evilflowers_analyzer.worker import app
    from evilflowers_worker import jobs

    reported = FakeCatalog()
    monkeypatch.setattr(jobs, "CatalogClient", lambda run_id, settings, attempt: reported)
    app.loader.import_default_modules()

    outcome = app.tasks["evilflowers_analyzer.extract"](
        run_id="run-1", source="a.pdf", output_dir="out", steps=["paragraphs", "pictures"]
    )

    assert outcome == "failed"
    assert reported.jobs == []
