"""Replaces the Docling pass in every process of a test worker (``test_end_to_end``): no model, no download.

Python imports ``sitecustomize`` at start-up from ``PYTHONPATH``, in the worker's parent and in every pool child,
whether forked or spawned, so the production app (``evilflowers_analyzer.worker``) runs unchanged otherwise. Every
layout batch finds one figure on its first page.
"""

from collections.abc import Iterator
from typing import Any

from evilflowers_analyzer import layout


def _layout_batches(path: str, page_count: int, settings: Any = None) -> Iterator[layout.Batch]:
    size = settings.layout_batch_pages if settings else 16
    for first, last in layout.batches(page_count, size):
        figure = {"page": first, "seq": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}, "caption": "Fig."}
        yield first, last, {"figure": [figure], "table": [], "equation": []}


layout.layout_batches = _layout_batches
