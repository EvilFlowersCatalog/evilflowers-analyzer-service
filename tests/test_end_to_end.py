"""One ``extract`` through the production analyzer worker, a real Redis and the catalog's API on localhost.

Only Docling is replaced (``fake_layout/sitecustomize.py``): no model is loaded or downloaded.

What only this test sees: the task wrapper (validation, delivery count, the publisher inside the task), the wire form
of every message the analyzer sends, and their ``parent_id`` (the catalog's job ledger hangs them under the run by it).
Needs ``TEST_REDIS_URL`` (default ``redis://localhost:6379/15``); skipped without one, a failure in CI.
"""

import os
import uuid
from pathlib import Path

import pytest

from conftest import write_pdf
from evilflowers_worker.testing import StubCatalogServer, WorkerProcess, redis_client, wait_for

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
HERE = Path(__file__).parent


@pytest.fixture
def broker():
    if redis_client(REDIS_URL) is None:
        if os.environ.get("CI"):
            pytest.fail(f"no Redis at {REDIS_URL}: the real-broker tests must run in CI")
        pytest.skip(f"no Redis at {REDIS_URL}")
    return REDIS_URL


def test_an_extract_starts_the_run_and_sends_every_job_downstream(broker, tmp_path):
    write_pdf(tmp_path / "catalogs/c/e/book.pdf", [(595, 842), (420.5, 595.25)])
    prefix = f"e2e-{uuid.uuid4().hex[:6]}"
    downstream = {
        "TEXT_PARAGRAPHS_QUEUE": f"{prefix}.paragraphs",
        "TEXT_TABLES_QUEUE": f"{prefix}.tables",
        "IMAGE_QUEUE": f"{prefix}.figures",
        "EQUATION_QUEUE": f"{prefix}.equations",
    }

    with StubCatalogServer(attempt=4) as catalog:
        env = {"CATALOG_API_URL": catalog.url, "STORAGE_PATH": str(tmp_path), **downstream}
        # Its sitecustomize replaces Docling in every process of the worker; the app is the production one.
        fake_layout = [str(HERE / "fake_layout")]
        with WorkerProcess("evilflowers_analyzer.worker", redis_url=broker, pythonpath=fake_layout, env=env) as worker:
            worker.wait_ready()
            try:
                task_id = worker.send(
                    "evilflowers_analyzer.extract",
                    run_id="run-1",
                    source="catalogs/c/e/book.pdf",
                    output_dir="catalogs/c/e/extraction/a/v2",
                    acquisition_id="acq-1",
                    pipeline_version=2,
                )
                assert wait_for(lambda: all(worker.redis.llen(q) == 1 for q in downstream.values()), 60), catalog.calls
                sent = {name: worker.queued(queue)[0] for name, queue in downstream.items()}
                # Acknowledged once dispatched: nothing left for Redis to restore.
                assert wait_for(lambda: worker.redis.llen(worker.queue) == 0, 10)
            finally:
                for queue in downstream.values():
                    worker.redis.delete(queue)

    ((method, path, start),) = catalog.calls
    assert (method, path) == ("POST", "/extraction/v1/runs/run-1/start")
    assert start["jobs"] == ["paragraphs", "figures", "equations", "tables"]
    assert start["provenance"]["analyzer"]["outline_source"] == "none"

    common = {"run_id": "run-1", "source": "catalogs/c/e/book.pdf", "output_dir": "catalogs/c/e/extraction/a/v2"}
    expected_tasks = {
        "TEXT_PARAGRAPHS_QUEUE": "evilflowers_text_worker.paragraphs",
        "TEXT_TABLES_QUEUE": "evilflowers_text_worker.tables",
        "IMAGE_QUEUE": "evilflowers_image.figures",
        "EQUATION_QUEUE": "evilflowers_equation.equations",
    }
    for name, message in sent.items():
        assert message["headers"]["task"] == expected_tasks[name]
        assert message["headers"]["parent_id"] == task_id
        kwargs = WorkerProcess.kwargs(message)
        assert {key: kwargs[key] for key in common} == common and kwargs["attempt"] == 4

    assert WorkerProcess.kwargs(sent["TEXT_PARAGRAPHS_QUEUE"])["outline"] == []
    figures = WorkerProcess.kwargs(sent["IMAGE_QUEUE"])["regions"]
    assert [(region["page"], region["caption"]) for region in figures] == [(1, "Fig.")]
    assert WorkerProcess.kwargs(sent["EQUATION_QUEUE"])["regions"] == []
