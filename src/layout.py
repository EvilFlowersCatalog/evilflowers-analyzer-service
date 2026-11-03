"""One Docling layout pass per document (IP-018 Q11).

Finds figures, tables and display equations and returns them by kind as
`{page, seq, bbox, caption}` with `bbox` as page fractions, origin top-left.
The other services only enrich these regions; none of them detects layout.

Layout runs on the page images, so it needs no OCR: a scanned book gets its
regions too. Table structure and formula recognition are left to text-service
and equation-service.
"""

import logging
from collections import defaultdict
from typing import Dict, List, Optional

import settings

logger = logging.getLogger(__name__)

KINDS = ("figure", "table", "equation")

_converter = None


def _get_converter():
    """Built once per worker process: the layout model loads with it."""
    global _converter
    if _converter is None:
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
        options.accelerator_options = AcceleratorOptions(num_threads=settings.LAYOUT_THREADS or 4, device="cpu")
        _converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    return _converter


def fractions(left: float, top: float, right: float, bottom: float, width: float, height: float) -> Dict[str, float]:
    """A top-left-origin box in points as page fractions, clamped to the page."""

    def clamp(value: float) -> float:
        return round(min(max(value, 0.0), 1.0), 5)

    return {
        "x0": clamp(min(left, right) / width),
        "y0": clamp(min(top, bottom) / height),
        "x1": clamp(max(left, right) / width),
        "y1": clamp(max(top, bottom) / height),
    }


def _kind(item) -> Optional[str]:
    from docling_core.types.doc import DocItemLabel, PictureItem, TableItem

    if isinstance(item, PictureItem):
        return "figure"
    if isinstance(item, TableItem):
        return "table"
    if getattr(item, "label", None) == DocItemLabel.FORMULA:
        return "equation"
    return None


def find_regions(path: str, page_count: int) -> Dict[str, List[dict]]:
    """Every figure, table and equation of the document, by kind, in reading order."""
    regions: Dict[str, List[dict]] = {kind: [] for kind in KINDS}
    seq = defaultdict(int)
    batch = max(settings.LAYOUT_BATCH_PAGES, 1)

    for first in range(1, page_count + 1, batch):
        last = min(first + batch - 1, page_count)
        try:
            result = _get_converter().convert(path, page_range=(first, last), raises_on_error=False)
        except Exception:
            # One bad batch of pages never fails the whole document.
            logger.exception("Layout failed on pages %s-%s of %s", first, last, path)
            continue

        document = result.document
        for item, _level in document.iterate_items():
            kind = _kind(item)
            if kind is None:
                continue
            caption = None
            if kind in ("figure", "table"):
                try:
                    caption = item.caption_text(document) or None
                except Exception:
                    caption = None
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

    return regions


def versions() -> dict:
    try:
        from importlib.metadata import version

        return {"docling": version("docling")}
    except Exception:
        return {}
