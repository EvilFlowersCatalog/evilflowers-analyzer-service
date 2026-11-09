import json
import logging
import sys

import pytest

from evilflowers_analyzer import observability
from evilflowers_analyzer.observability import metrics
from evilflowers_analyzer.observability.setup import ENDPOINT_VARIABLES


@pytest.fixture
def clean_env(monkeypatch):
    for name in (*ENDPOINT_VARIABLES, "HOST_IP"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def record(message="hello", exc_info=None):
    return logging.LogRecord("analyzer", logging.WARNING, __file__, 1, message, None, exc_info)


def line(log_record=None) -> dict:
    return json.loads(observability.JsonFormatter().format(log_record or record()))


def test_no_endpoint_without_configuration(clean_env):
    assert observability.endpoint() is None


def test_explicit_endpoint_wins(clean_env):
    clean_env.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    clean_env.setenv("HOST_IP", "10.0.0.1")
    assert observability.endpoint() == "http://collector:4318"


def test_endpoint_from_host_ip(clean_env):
    clean_env.setenv("HOST_IP", "192.168.122.10")
    assert observability.endpoint() == "http://192.168.122.10:4318"


def test_endpoint_from_ipv6_host_ip(clean_env):
    clean_env.setenv("HOST_IP", "fd00::1")
    assert observability.endpoint() == "http://[fd00::1]:4318"


def test_span_and_metrics_are_safe_before_setup():
    with observability.span("test.span", **{"absent": None, "present": 1}):
        observability.annotate(**{"key": "value"})
        observability.fail(RuntimeError("boom"))
    metrics.record_run("dispatched", "dispatch")
    metrics.record_layout_batch(16, 1.5, "ok")
    metrics.REGIONS.add(3, {"kind": "figure"})


def test_json_log_line_outside_a_span():
    log_record = logging.LogRecord("analyzer", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    payload = line(log_record)
    assert payload["level"] == "warning"
    assert payload["logger"] == "analyzer"
    assert payload["message"] == "hello world"
    assert "trace_id" not in payload and "run_id" not in payload


def test_json_log_line_carries_the_traceback():
    try:
        raise ValueError("bad pdf")
    except ValueError:
        payload = line(record("failed", sys.exc_info()))
    assert "ValueError: bad pdf" in payload["exception"]


def test_json_log_line_inside_a_span_has_the_trace_id():
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    tracer = sdk.TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work") as span:
        payload = line()
        expected = format(span.get_span_context().trace_id, "032x")
    assert payload["trace_id"] == expected
    assert len(payload["span_id"]) == 16


def test_bound_fields_reach_the_log_line_and_the_span():
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    tracer = sdk.TracerProvider().get_tracer("test")
    with (
        tracer.start_as_current_span("task") as span,
        observability.bind(run_id="r-1", source="a.pdf", acquisition_id=None),
    ):
        with observability.bind(job="analyzer"):
            payload = line()
        outer = line()
    assert payload["run_id"] == "r-1" and payload["job"] == "analyzer" and payload["source"] == "a.pdf"
    assert "job" not in outer and "acquisition_id" not in outer
    assert span.attributes["evilflowers.extraction.run_id"] == "r-1"
    assert span.attributes["evilflowers.source"] == "a.pdf"
    assert "run_id" not in line()


def test_bound_fields_cannot_overwrite_the_line():
    with observability.bind(message="spoofed", level="error"):
        payload = line()
    assert payload["message"] == "hello" and payload["level"] == "warning"


def test_sent_tasks_carry_the_trace_context():
    """The downstream jobs join the run's trace: `send_task` inside a span injects `traceparent`.

    Needs the `otel` extra (`make install-all`); skipped with the dev tools only.
    """
    celery_instrumentation = pytest.importorskip("opentelemetry.instrumentation.celery")
    from celery import Celery, signals
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    instrumentor = celery_instrumentation.CeleryInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    captured: list[dict] = []

    def capture(headers=None, **_kwargs):
        captured.append(dict(headers or {}))

    signals.before_task_publish.connect(capture, weak=False)
    try:
        app = Celery("propagation", broker="memory://")
        with provider.get_tracer("test").start_as_current_span("evilflowers_analyzer.extract") as span:
            app.send_task("evilflowers_image.figures", kwargs={"run_id": "r-1"}, queue="evilflowers_image")
            trace_id = format(span.get_span_context().trace_id, "032x")
    finally:
        signals.before_task_publish.disconnect(capture)
        instrumentor.uninstrument()

    assert captured and trace_id in captured[0]["traceparent"]
