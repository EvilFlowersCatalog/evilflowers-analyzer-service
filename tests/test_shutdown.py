"""Scale-in: a SIGTERMed worker puts its unacknowledged task back on the Redis list at once (issue #2).

Runs a real worker against a real Redis (``TEST_REDIS_URL``, default
``redis://localhost:6379/15``) and is skipped when none answers. The worker gets
the environment the image sets (``REMAP_SIGTERM=SIGQUIT``, see the Dockerfile).
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from celery import Celery

from evilflowers_analyzer.worker import create_app

redis = pytest.importorskip("redis")

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
ROOT = Path(__file__).parents[1]


def _redis():
    client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip(f"no Redis at {REDIS_URL}")
    return client


def _wait(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return condition()


def test_soft_shutdown_settings_come_from_the_environment(settings):
    app = create_app(settings.model_copy(update={"worker_soft_shutdown_timeout": 7.5}))
    assert app.conf.worker_soft_shutdown_timeout == 7.5
    assert app.conf.worker_enable_soft_shutdown_on_idle is True


def test_the_image_turns_sigterm_into_a_cold_shutdown():
    # Billiard reads it when Celery is imported, so it must be in the process environment, not in Settings.
    assert "REMAP_SIGTERM=SIGQUIT" in (ROOT / "Dockerfile").read_text()


def test_sigterm_requeues_the_running_task_at_once(tmp_path):
    client = _redis()
    queue = f"shutdown-probe-{uuid.uuid4().hex[:8]}"
    started = tmp_path / "started"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "tests"), os.environ.get("PYTHONPATH")])),
        "REDIS_URL": REDIS_URL,
        "ANALYZER_QUEUE": queue,
        "WORKER_SOFT_SHUTDOWN_TIMEOUT": "2",
        # Without it SIGTERM is a warm shutdown: the worker waits for the task and nothing is requeued.
        "REMAP_SIGTERM": "SIGQUIT",
        "PROBE_STARTED": str(started),
        "LOG_FORMAT": "text",
    }
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "HOST_IP", "PYROSCOPE_SERVER_ADDRESS"):
        env.pop(name, None)

    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "shutdown_probe",
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            f"--queues={queue}",
            "--loglevel=INFO",
            f"--hostname={queue}@%h",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Its own group: on failure the prefork child is killed too, or it keeps the pipe open.
        start_new_session=True,
    )
    try:
        Celery(broker=REDIS_URL).send_task("shutdown_probe.long", queue=queue)
        assert _wait(started.exists, 60), "the task never started"
        assert client.llen(queue) == 0

        sent = time.monotonic()
        worker.send_signal(signal.SIGTERM)

        assert _wait(lambda: client.llen(queue) == 1, 30), "the task was not requeued"
        assert time.monotonic() - sent < 30
        assert worker.wait(timeout=30) is not None
        # What `extract` counts its passes by (#3).
        requeued = json.loads(client.lindex(queue, 0))
        assert requeued["properties"]["delivery_info"]["redelivered"] is True
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(worker.pid, signal.SIGKILL)
        output = worker.communicate(timeout=30)[0]
        client.delete(queue)
        print(output)
