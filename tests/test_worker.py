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
    assert paragraphs_job(settings).task == "evilflowers_text_worker.paragraphs"
    assert {kind: (job.task, job.queue) for kind, job in region_jobs(settings).items()} == {
        "figure": ("evilflowers_image.figures", "evilflowers_image"),
        "equation": ("evilflowers_equation.equations", "evilflowers_equation"),
        "table": ("evilflowers_text_worker.tables", "evilflowers_text_worker"),
    }
