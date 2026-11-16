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

The pages come from a ``PageSource``: a PDF (the catalog's ``extract``), or page
images (the books digitalizer's ``standalone.layout``, #8). Both go through the
one converter, so the layout model is loaded once per process. Without OCR and
without a text layer, Docling keeps only pictures, tables and formulas on an
image: text-like regions (and so captions) need text cells.
"""

import logging
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from evilflowers_analyzer import metrics
from evilflowers_analyzer.settings import Settings, get_settings
from evilflowers_worker.observability import annotate, fail, span

logger = logging.getLogger(__name__)

KINDS = ("figure", "table", "equation")
#: Kinds (or Docling labels) that carry a caption.
CAPTIONED = frozenset({"figure", "table", "picture", "chart"})

#: The kind of a Docling item, or ``None`` to skip it.
Classify = Callable[[Any], str | None]


class PageSource(Protocol):
    """Where a layout pass reads its pages: 1-based pages, converted batch by batch."""

    @property
    def page_count(self) -> int: ...

    def batches(self, size: int) -> list[tuple[int, int]]:
        """1-based inclusive page ranges, each converted by one Docling call."""
        ...

    def convert(self, converter: Any, first: int, last: int) -> Any:
        """Docling's conversion result of pages ``first``-``last``."""
        ...

    def page(self, first: int, page_no: int) -> int:
        """The source's page of ``page_no`` in the result of the batch that starts at ``first``."""
        ...


@dataclass(frozen=True)
class PdfSource:
    """A PDF, ``LAYOUT_BATCH_PAGES`` pages per conversion."""

    path: str
    page_count: int

    def batches(self, size: int) -> list[tuple[int, int]]:
        return batches(self.page_count, size)

    def convert(self, converter: Any, first: int, last: int) -> Any:
        return converter.convert(self.path, page_range=(first, last), raises_on_error=False)

    def page(self, first: int, page_no: int) -> int:
        return page_no

    def __str__(self) -> str:
        return self.path


@dataclass(frozen=True)
class ImageSource:
    """Page images in page order, one conversion each (Docling reads one image per document)."""

    images: Sequence[str]

    @property
    def page_count(self) -> int:
        return len(self.images)

    def batches(self, size: int) -> list[tuple[int, int]]:
        return [(page, page) for page in range(1, self.page_count + 1)]

    def convert(self, converter: Any, first: int, last: int) -> Any:
        return converter.convert(self.images[first - 1], raises_on_error=False)

    def page(self, first: int, page_no: int) -> int:
        return first + page_no - 1

    def __str__(self) -> str:
        return f"{self.page_count} page images"


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
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = False
    options.do_formula_enrichment = False
    options.generate_page_images = False
    options.generate_picture_images = False
    options.accelerator_options = AcceleratorOptions(num_threads=threads, device="cpu")
    # The same options for both: Docling caches its pipeline by class and options, so PDFs and page images (#8)
    # share one pipeline and one layout model in memory.
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
        }
    )


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


def label(item: Any) -> str | None:
    """Docling's own label of a content item (``picture``, ``table``, ``formula``, ``text`` …); groups are skipped."""
    from docling_core.types.doc import DocItem

    if not isinstance(item, DocItem):
        return None
    return str(getattr(item.label, "value", item.label))


def _caption(item: Any, kind: str, document: Any) -> str | None:
    if kind not in CAPTIONED:
        return None
    try:
        return item.caption_text(document) or None
    except Exception:
        return None


#: One layout batch: its first and last page (1-based, inclusive) and the regions found on them, by kind.
Batch = tuple[int, int, dict[str, list[dict]]]


def layout_batches(
    source: PageSource,
    settings: Settings | None = None,
    *,
    classify: Classify | None = None,
    kinds: Sequence[str] = KINDS,
    on_failed: Callable[[int, int], None] | None = None,
) -> Iterator[Batch]:
    """The source's regions by kind, one batch of pages at a time (``LAYOUT_BATCH_PAGES`` for a PDF).

    Every batch is yielded, in page order, as soon as it is converted. A bad batch is
    logged and yielded with no regions (and passed to ``on_failed``); it never fails the
    whole document. Only a failure to load the model raises, before the first batch.

    ``classify`` names the kind of each Docling item (default: the catalog's ``figure`` /
    ``table`` / ``equation``); ``kinds`` are the keys every batch carries, even when empty,
    and with ``()`` a batch carries only the kinds it found (Docling labels, #8).
    """
    settings = settings or get_settings()
    classify = classify or _kind
    seq: defaultdict[tuple[str, int], int] = defaultdict(int)
    converter = _get_converter(settings)
    failed = 0
    found: Counter[str] = Counter()

    for first, last in source.batches(settings.layout_batch_pages):
        regions: defaultdict[str, list[dict]] = defaultdict(list)
        for kind in kinds:
            regions[kind] = []
        if _convert_batch(converter, source, first, last, regions, seq, classify) == "failed":
            failed += 1
            if on_failed is not None:
                on_failed(first, last)
        found.update({kind: len(items) for kind, items in regions.items()})
        yield first, last, dict(regions)

    annotate(
        **{
            "evilflowers.layout.failed_batches": failed,
            **{f"evilflowers.layout.{kind}s": found[kind] for kind in (kinds or sorted(found))},
        }
    )


def find_regions(path: str, page_count: int, settings: Settings | None = None) -> dict[str, list[dict]]:
    """Every figure, table and equation of the PDF, by kind, in reading order (see ``layout_batches``)."""
    regions: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    for _first, _last, batch in layout_batches(PdfSource(path, page_count), settings):
        for kind in KINDS:
            regions[kind].extend(batch[kind])
    return regions


def _convert_batch(
    converter: Any,
    source: PageSource,
    first: int,
    last: int,
    regions: defaultdict[str, list[dict]],
    seq: defaultdict[tuple[str, int], int],
    classify: Classify,
) -> str:
    """Convert pages ``first``-``last`` and collect their regions; returns ``ok`` or ``failed``."""
    started = time.perf_counter()
    outcome = "ok"
    with span(
        "analyzer.layout.batch", **{"evilflowers.layout.first_page": first, "evilflowers.layout.last_page": last}
    ):
        try:
            result = source.convert(converter, first, last)
        except Exception as error:
            logger.exception("Layout failed on pages %s-%s of %s", first, last, source)
            fail(error)
            outcome = "failed"
        else:
            # With raises_on_error=False Docling reports a failed conversion in `status`, not by raising.
            status = getattr(result, "status", None)
            status = str(getattr(status, "value", status or "success"))
            if status == "failure":
                logger.warning("Layout failed on pages %s-%s of %s: %s", first, last, source, result.errors)
                outcome = "failed"
            found = _collect(result.document, regions, seq, classify, lambda page_no: source.page(first, page_no))
            annotate(**{"evilflowers.layout.status": status, "evilflowers.layout.regions": found})
    metrics.record_layout_batch(last - first + 1, time.perf_counter() - started, outcome)
    return outcome


def _collect(
    document: Any,
    regions: defaultdict[str, list[dict]],
    seq: defaultdict[tuple[str, int], int],
    classify: Classify,
    source_page: Callable[[int], int],
) -> int:
    """Append the regions of one converted batch; returns how many were found."""
    found = 0
    for item, _level in document.iterate_items():
        kind = classify(item)
        if kind is None:
            continue
        caption = _caption(item, kind, document)
        for prov in item.prov:
            page = document.pages.get(prov.page_no)
            if page is None or not page.size.width or not page.size.height:
                continue
            box = prov.bbox.to_top_left_origin(page_height=page.size.height)
            number = source_page(prov.page_no)
            key = (kind, number)
            regions[kind].append(
                {
                    "page": number,
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
