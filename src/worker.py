"""Compatibility shim for the pre-1.0 start command, ``celery --workdir src -A worker worker …``.

The dev Deployment's ``args`` are a copy of the old image CMD. Until they are
changed to ``celery -A evilflowers_analyzer.worker worker …`` (EvilFlowersCatalog/knowledge#22),
a restart onto the new image still finds the app here. Remove it after that lands.
"""

from evilflowers_analyzer.worker import app

__all__ = ["app"]
