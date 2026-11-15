from pathlib import Path

import pymupdf
import pytest

from evilflowers_analyzer.settings import Settings
from evilflowers_worker.testing import FakeCatalog


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


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog()
