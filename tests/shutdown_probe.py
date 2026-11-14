"""A Celery app with the analyzer's configuration and one long task, run as a real worker by ``test_shutdown``.

The task is acknowledged late and requeued when its worker dies, like ``extract``.
It touches ``PROBE_STARTED`` once it runs, then sleeps far longer than the test waits.
"""

import os
import time
from pathlib import Path

from evilflowers_analyzer.worker import create_app

app = create_app()


@app.task(name="shutdown_probe.long", acks_late=True, reject_on_worker_lost=True)
def long() -> None:
    Path(os.environ["PROBE_STARTED"]).touch()
    time.sleep(600)
