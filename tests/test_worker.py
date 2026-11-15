"""The task contract with the catalog, and the jobs the analyzer declares and sends."""

import inspect

from evilflowers_analyzer.pipeline import declared_jobs, paragraphs_job, region_jobs
from evilflowers_analyzer.worker import app

TASK = "evilflowers_analyzer.extract"
QUEUE = "evilflowers_analyzer"


def test_extract_is_registered_and_routed():
    app.loader.import_default_modules()
    assert TASK in app.tasks
    assert app.amqp.router.route({}, TASK)["queue"].name == QUEUE


def test_extract_is_acknowledged_late_and_requeued_when_the_worker_dies():
    app.loader.import_default_modules()
    task = app.tasks[TASK]
    assert task.acks_late
    assert task.reject_on_worker_lost


def test_extract_accepts_the_catalogs_arguments_and_ones_a_newer_catalog_adds():
    app.loader.import_default_modules()
    parameters = inspect.signature(app.tasks[TASK].run).parameters
    for name in ("run_id", "source", "output_dir", "acquisition_id", "pipeline_version"):
        assert name in parameters, name
    assert any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def test_broker_redelivery_outlasts_a_long_layout_pass():
    assert app.conf.broker_transport_options["visibility_timeout"] >= 3600
    assert app.conf.worker_prefetch_multiplier == 1


def test_the_old_start_command_still_finds_the_app():
    import importlib.util
    from pathlib import Path

    shim = Path(__file__).parents[1] / "src" / "worker.py"
    spec = importlib.util.spec_from_file_location("analyzer_shim", shim)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.app is app


def test_every_dispatched_job_is_declared(settings):
    assert declared_jobs(settings) == ["paragraphs", "figures", "equations", "tables"]


def test_downstream_tasks_and_queues(settings):
    paragraphs = paragraphs_job(settings)
    assert (paragraphs.task, paragraphs.queue) == ("evilflowers_text_worker.paragraphs", "evilflowers.text.paragraphs")
    assert {kind: (job.task, job.queue) for kind, job in region_jobs(settings).items()} == {
        "figure": ("evilflowers_image.figures", "evilflowers_image"),
        "equation": ("evilflowers_equation.equations", "evilflowers_equation"),
        "table": ("evilflowers_text_worker.tables", "evilflowers.text.tables"),
    }


def test_the_legacy_text_queue_setting_is_ignored(monkeypatch):
    """A manifest still setting `TEXT_QUEUE` must not pull the text jobs back onto the legacy queue (#4)."""
    from evilflowers_analyzer.settings import Settings

    monkeypatch.setenv("TEXT_QUEUE", "evilflowers_text_worker")
    settings = Settings(_env_file=None)
    assert paragraphs_job(settings).queue == "evilflowers.text.paragraphs"
    assert region_jobs(settings)["table"].queue == "evilflowers.text.tables"


def test_the_deployed_variable_names_still_apply(monkeypatch):
    from evilflowers_analyzer.settings import Settings

    monkeypatch.setenv("ANALYZER_QUEUE", "evilflowers_analyzer_v2")
    monkeypatch.setenv("EXTRACT_MAX_PASSES", "3")
    settings = Settings(_env_file=None)
    assert settings.queue == "evilflowers_analyzer_v2"
    assert settings.max_deliveries == 3


def test_the_app_runs_on_the_kits_runtime(settings):
    """Scale-in is the kit's (`evilflowers_worker.shutdown`); Celery's blocking soft shutdown stays off."""
    from evilflowers_analyzer.worker import create_app

    custom = create_app(settings.model_copy(update={"worker_soft_shutdown_timeout": 7.5}))
    assert custom.conf.worker_soft_shutdown_timeout == 0
    assert custom.conf.task_default_queue == QUEUE
    assert [queue.name for queue in custom.conf.task_queues] == [QUEUE]
    # Restore the module's app as the process's runtime for the other tests.
    create_app()


def test_the_image_keeps_sigterm_a_warm_shutdown():
    from pathlib import Path

    # With SIGTERM remapped, Celery's soft shutdown comes back, and with it the redone books (#2).
    assert "REMAP_SIGTERM=SIGQUIT" not in (Path(__file__).parents[1] / "Dockerfile").read_text()


def test_an_invalid_extract_message_fails_the_run_instead_of_retrying(monkeypatch):
    """A message the analyzer cannot read is reported as a failed `analyzer` job, never redelivered."""
    from evilflowers_worker import jobs
    from evilflowers_worker.testing import FakeCatalog

    reported = FakeCatalog()
    monkeypatch.setattr(jobs, "CatalogClient", lambda run_id, settings, attempt: reported)
    app.loader.import_default_modules()

    outcome = app.tasks[TASK](run_id="run-1", source="a.pdf", output_dir="out", pipeline_version="second")

    assert outcome == "failed"
    assert [(job["name"], job["failed"]) for job in reported.jobs] == [("analyzer", True)]
    assert reported.jobs[0]["error"].startswith("Invalid message")
