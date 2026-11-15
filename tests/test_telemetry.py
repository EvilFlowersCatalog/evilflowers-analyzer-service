"""The analyzer's own telemetry; the kit's (setup, spans, logs, propagation) is tested in its package."""

from evilflowers_analyzer import metrics
from evilflowers_analyzer.worker import app  # noqa: F401 - configures the service identity
from evilflowers_worker.observability import identity, span


def test_the_service_identity_is_the_analyzers():
    assert identity().service == "analyzer"
    assert identity().scope == "evilflowers.analyzer"


def test_analyzer_metrics_are_safe_before_setup():
    with span("analyzer.test"):
        metrics.record_run("dispatched", "dispatch")
        metrics.record_layout_batch(16, 1.5, "ok")
        metrics.REGIONS.add(3, {"kind": "figure"})
