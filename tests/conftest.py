from pathlib import Path

import pymupdf
import pytest

from evilflowers_analyzer.settings import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Defaults only (no `.env`), storage in a temporary directory, two catalog attempts."""
    return Settings(_env_file=None, storage_path=tmp_path, catalog_attempts=2, image_tag="test")


def write_pdf(path: Path, sizes: list[tuple[float, float]]) -> Path:
    """A PDF with one page per size, each with a line of text."""
    pdf = pymupdf.open()
    for width, height in sizes:
        page = pdf.new_page(width=width, height=height)
        page.insert_text((40, 60), "Entropy measures the uncertainty of a random variable.", fontsize=11)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(path)
    pdf.close()
    return path


class FakeCatalog:
    """Records what the analyzer posts; `start_error` makes `/start` raise."""

    def __init__(self, start_error: Exception | None = None):
        self.start_error = start_error
        self.started: list[dict] = []
        self.jobs: list[dict] = []

    def start(self, page_count, page_sizes, jobs, provenance):
        if self.start_error is not None:
            raise self.start_error
        self.started.append(
            {"page_count": page_count, "page_sizes": page_sizes, "jobs": list(jobs), "provenance": provenance}
        )

    def job(self, name, *, failed=False, error=None, provenance=None):
        self.jobs.append({"name": name, "failed": failed, "error": error})


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog()
