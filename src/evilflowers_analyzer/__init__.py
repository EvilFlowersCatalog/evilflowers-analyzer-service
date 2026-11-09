"""EvilFlowers analyzer service: the Celery worker that starts an IP-018 extraction run and fans out its jobs."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("evilflowers-analyzer-service")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"
