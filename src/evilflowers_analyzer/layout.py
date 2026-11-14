"""One Docling layout pass per document (IP-018 Q11).

Finds figures, tables and display equations and returns them by kind as
``{page, seq, bbox, caption}`` with ``bbox`` as page fractions, origin top-left,
on the original file's 1-based page. The other services only enrich these
regions; none of them detects layout.

Layout runs on the page images, so it needs no OCR: a scanned book gets its
regions too. Table structure and formula recognition are left to text-service
and equation-service.

Docling (and torch with it) is imported on first use, in the prefork child that
runs the task, never in the parent before the fork.
"""

import logging
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from typing import Any

from evilflowers_analyzer.observability import annotate, fail, metrics, span
from evilflowers_analyzer.settings import Settings, get_settings

logger = logging.getLogger(__name__)

KINDS = ("figure", "table", "equation")

_lock = threading.Lock()
_converter: Any = None


def _get_converter(settings: Settings) -> Any:
    """Built once per worker process: the layout model loads with it."""
    global _converter
    with _lock:
        if _converter is None:
            with span("analyzer.layout.load", **{"evilflowers.layout.threads": settings.layout_threads}):
                _converter = _build_converter(settings.layout_threads)
    return _converter


def _build_converter(threads: int) -> Any:
    try:
        from docling.datamodel.accelerator_options import AcceleratorOptions
    except ImportError:  # docling < 2.40
        from docling.datamodel.pipeline_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = False
    options.do_formula_enrichment = False
    options.generate_page_images = False
    options.generate_picture_images = False
    options.accelerator_options = AcceleratorOptions(num_threads=threads, device="cpu")
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def fractions(left: float, top: float, right: float, bottom: float, width: float, height: float) -> dict[str, float]:
    """A top-left-origin box in points as page fractions, clamped to the page."""

    def clamp(value: float) -> float:
        return round(min(max(value, 0.0), 1.0), 5)

    return {
        "x0": clamp(min(left, right) / width),
        "y0": clamp(min(top, bottom) / height),
        "x1": clamp(max(left, right) / width),
        "y1": clamp(max(top, bottom) / height),
    }


def batches(page_count: int, size: int) -> list[tuple[int, int]]:
    """1-based inclusive page ranges of at most ``size`` pages."""
    size = max(size, 1)
    return [(first, min(first + size - 1, page_count)) for first in range(1, page_count + 1, size)]


def _kind(item: Any) -> str | None:
    from docling_core.types.doc import DocItemLabel, PictureItem, TableItem

    if isinstance(item, PictureItem):
        return "figure"
    if isinstance(item, TableItem):
        return "table"
    if getattr(item, "label", None) == DocItemLabel.FORMULA:
        return "equation"
    return None


def _caption(item: Any, kind: str, document: Any) -> str | None:
    if kind not in ("figure", "table"):
        return None
    try:
        return item.caption_text(document) or None
    except Exception:
        return None


#: One layout batch: its first and last page (1-based, inclusive) and the regions found on them, by kind.
Batch = tuple[int, int, dict[str, list[dict]]]


def layout_batches(path: str, page_count: int, settings: Settings | None = None) -> Iterator[Batch]:
    """The document's figures, tables and equations, one batch of ``LAYOUT_BATCH_PAGES`` pages at a time.

    Every batch is yielded, in page order, as soon as it is converted. A bad batch is
    logged and yielded with no regions; it never fails the whole document. Only a
    failure to load the model raises, before the first batch.
    """
    settings = settings or get_settings()
    seq: defaultdict[tuple[str, int], int] = defaultdict(int)
    converter = _get_converter(settings)
    failed = 0
    found: Counter[str] = Counter()

    for first, last in batches(page_count, settings.layout_batch_pages):
        regions: dict[str, list[dict]] = {kind: [] for kind in KINDS}
        if _convert_batch(converter, path, first, last, regions, seq) == "failed":
            failed += 1
        found.update({kind: len(items) for kind, items in regions.items()})
        yield first, last, regions

    annotate(
        **{
            "evilflowers.layout.failed_batches": failed,
            **{f"evilflowers.layout.{kind}s": found[kind] for kind in KINDS},
        }
    )


def find_regions(path: str, page_count: int, settings: Settings | None = None) -> dict[str, list[dict]]:
    """Every figure, table and equation of the document, by kind, in reading order (see ``layout_batches``)."""
    regions: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    for _first, _last, batch in layout_batches(path, page_count, settings):
        for kind in KINDS:
            regions[kind].extend(batch[kind])
    return regions


def _convert_batch(
    converter: Any,
    path: str,
    first: int,
    last: int,
    regions: dict[str, list[dict]],
    seq: defaultdict[tuple[str, int], int],
) -> str:
    """Convert pages ``first``-``last`` and collect their regions; returns ``ok`` or ``failed``."""
    started = time.perf_counter()
    outcome = "ok"
    with span(
        "analyzer.layout.batch", **{"evilflowers.layout.first_page": first, "evilflowers.layout.last_page": last}
    ):
        try:
            result = converter.convert(path, page_range=(first, last), raises_on_error=False)
        except Exception as error:
            logger.exception("Layout failed on pages %s-%s of %s", first, last, path)
            fail(error)
            outcome = "failed"
        else:
            # With raises_on_error=False Docling reports a failed conversion in `status`, not by raising.
            status = getattr(result, "status", None)
            status = str(getattr(status, "value", status or "success"))
            if status == "failure":
                logger.warning("Layout failed on pages %s-%s of %s: %s", first, last, path, result.errors)
                outcome = "failed"
            found = _collect(result.document, regions, seq)
            annotate(**{"evilflowers.layout.status": status, "evilflowers.layout.regions": found})
    metrics.record_layout_batch(last - first + 1, time.perf_counter() - started, outcome)
    return outcome


def _collect(document: Any, regions: dict[str, list[dict]], seq: defaultdict[tuple[str, int], int]) -> int:
    """Append the regions of one converted batch; returns how many were found."""
    found = 0
    for item, _level in document.iterate_items():
        kind = _kind(item)
        if kind is None:
            continue
        caption = _caption(item, kind, document)
        for prov in item.prov:
            page = document.pages.get(prov.page_no)
            if page is None or not page.size.width or not page.size.height:
                continue
            box = prov.bbox.to_top_left_origin(page_height=page.size.height)
            key = (kind, prov.page_no)
            regions[kind].append(
                {
                    "page": prov.page_no,
                    "seq": seq[key],
                    "bbox": fractions(box.l, box.t, box.r, box.b, page.size.width, page.size.height),
                    "caption": caption,
                }
            )
            seq[key] += 1
            found += 1
    return found


def versions() -> dict[str, str]:
    """Versions recorded in the run's provenance."""
    try:
        from importlib.metadata import version

        return {"docling": version("docling")}
    except Exception:
        return {}
