"""The document's outline: the headings its paragraphs fall under (#5).

Read from the PDF's bookmarks when it has them. Many repackaged books (Open Research
Library copies among them) have none, and their paragraphs then came out with no
section at all. Those get their headings detected from the text layer instead:
lines set larger than the body text, or numbered (``1.2.3 Title``) in bold, that do
not repeat in the page margins like a running head.

Detection is layout-based, so a bold first line of a list, a figure label or a table
header can pass for a heading (#9). Each detected candidate is therefore asked of
``decider`` (``SECTION_HEADING``, 64 per request through the kit's ``Panel.ask_batch``);
books with bookmarks are never asked. A candidate is dropped only when its verdict is
banded ``hide``, which needs thresholds for ``section_heading`` in ``JUDGE_THRESHOLDS``
(knowledge#65); until then every candidate stays, and a candidate without an answer
always stays (the outline as before). Each verdict (``p``, ``decision``,
``decider_model``) is kept in ``Outline.decisions`` for the run's provenance.

The outline goes to text-service with the paragraph job, in PyMuPDF's
``get_toc(simple=True)`` shape (``[level, title, page]``, 1-based page), and its
source (``bookmarks`` | ``headings`` | ``none``, or ``failed``) into the run's
provenance, so the catalog can tell a book without an outline from a failure.
"""

import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import pymupdf

from evilflowers_worker.judges import Binary, Verdict

logger = logging.getLogger(__name__)

#: Asked of ``decider`` about every heading detected in a book without bookmarks (#9).
SECTION_HEADING = Binary(
    id="section_heading",
    instructions=(
        "Is this line a section heading of the book (chapter or section title), not a caption, list item, table "
        "header or running head? The state gives the line, the two lines before and after it, its page and its font "
        "size relative to the body text."
    ),
)
#: Asks one question about many states, a verdict or ``None`` per state in order: ``Panel.ask_batch`` bound to a
#: judge and ``SECTION_HEADING``.
Ask = Callable[[Sequence[Mapping[str, Any]]], Sequence[Verdict | None]]
#: Lines of context on each side of a candidate.
CONTEXT_LINES = 2

Source = Literal["bookmarks", "headings", "none", "failed"]

#: A heading is set at least this much larger than the body text.
SIZE_RATIO = 1.15
#: Share of the page height at the top and bottom where running heads and page numbers live.
MARGIN = 0.07
#: Longest heading, in words and characters.
MAX_WORDS = 16
MAX_CHARS = 160
#: Font sizes (heading levels) kept; smaller distinct sizes fold into the last level.
MAX_LEVELS = 3
#: A text found on more pages than this is a running head, not a heading.
MAX_REPEATS = 2
#: More detected headings per page than this means the detection is not finding headings.
MAX_PER_PAGE = 3.0

#: ``1.2.3 Title``: the depth of the number is the heading's level.
_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")
#: ``Chapter 1 Title``, ``Kapitola 2``, ``Part III``: a top-level heading.
_CHAPTER = re.compile(r"^(chapter|part|appendix|kapitola|časť|príloha)\s+([0-9]+|[ivxlc]+|[a-z])\b", re.IGNORECASE)
#: A line that is only a heading's number (``1.2.1``) or its ``Chapter 1`` label, set apart from its title.
_LABEL = re.compile(r"^(\d+(?:\.\d+)*\.?|" + _CHAPTER.pattern[1:] + r")$", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")
#: C0 control characters (but tab, newline) and DEL: the catalog refuses ``\x00`` in a ``section`` (#6).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_BOLD = 16  # PyMuPDF span flag
#: Text blocks only: by default "dict" also decodes every image on the page, every page of a scan.
_TEXT_ONLY = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES


@dataclass(frozen=True)
class Outline:
    source: Source
    #: ``[level, title, page]``, in page order.
    entries: list[list[Any]] = field(default_factory=list)
    #: The decider's verdict on each detected candidate it answered for (#9), dropped ones included:
    #: ``{"page", "title", "decision", "p", "decider_model"}`` and ``band`` once the question has thresholds.
    decisions: list[dict[str, Any]] = field(default_factory=list)


def clean_title(title: str) -> str:
    """The title without control characters (a UTF-16 title read without its BOM is ``\\x00I\\x00n…``), whitespace
    collapsed."""
    return " ".join(_CONTROL.sub("", title).split())


def find(document: Any, ask: Ask | None = None) -> Outline:
    """The bookmarks, else the detected headings, each asked of ``ask`` when given; never raises."""
    try:
        bookmarks = [
            [int(level), cleaned, int(page)]
            for level, title, page, *_ in document.get_toc(simple=True)
            if title and (cleaned := clean_title(title)) and page >= 1
        ]
        if bookmarks:
            return Outline("bookmarks", bookmarks)
        headings, decisions = detect_headings(document, ask)
        return Outline("headings" if headings else "none", headings, decisions)
    except Exception:
        logger.exception("Cannot read the outline")
        return Outline("failed")


@dataclass
class _Line:
    page: int
    text: str
    size: float
    bold: bool
    block: tuple[int, int]
    #: A joined heading's first and last line, as indices into ``_lines``; the context the decider sees.
    first: int = -1
    last: int = -1


def _half_points(size: float) -> float:
    return round(size * 2) / 2


def _lines(document: Any) -> tuple[list[_Line], Counter[float]]:
    """Every text line outside the page margins, and how many characters are set in each size (all lines)."""
    lines: list[_Line] = []
    sizes: Counter[float] = Counter()
    for index, page in enumerate(document):
        height = page.rect.height
        for number, block in enumerate(page.get_text("dict", flags=_TEXT_ONLY)["blocks"]):
            for line in block.get("lines", []):
                spans = [span for span in line["spans"] if span["text"].strip()]
                for span in spans:
                    sizes[_half_points(span["size"])] += len(span["text"].strip())
                _x0, y0, _x1, y1 = line["bbox"]
                if spans and height * MARGIN < y1 and y0 < height * (1 - MARGIN):
                    lines.append(_line(spans, page=index + 1, block=(index, number)))
    return lines, sizes


def _line(spans: list[dict[str, Any]], *, page: int, block: tuple[int, int]) -> _Line:
    """One line with its largest font size; bold only when every span is."""
    text = clean_title(" ".join(span["text"] for span in spans))
    return _Line(
        page=page,
        text=unicodedata.normalize("NFKC", text),
        size=_half_points(max(span["size"] for span in spans)),
        bold=all(span["flags"] & _BOLD for span in spans),
        block=block,
    )


def _stands_out(line: _Line, body: float) -> bool:
    """Set like a heading: larger than the body text, or bold and numbered."""
    if line.size >= body * SIZE_RATIO:
        return True
    return line.bold and line.size >= body and bool(_NUMBERED.match(line.text) or _LABEL.match(line.text))


def _is_heading(text: str) -> bool:
    if not (2 <= len(text) <= MAX_CHARS) or len(text.split()) > MAX_WORDS:
        return False
    return any(char.isalpha() for char in _LABEL.sub("", text))


def _depth(text: str) -> int | None:
    """The level the heading's own numbering gives it, if it has any."""
    if _CHAPTER.match(text):
        return 1
    numbered = _NUMBERED.match(text)
    return numbered.group(1).count(".") + 1 if numbered else None


def detect_headings(document: Any, ask: Ask | None = None) -> tuple[list[list[Any]], list[dict[str, Any]]]:
    """Headings found from font size and numbering, as ``[level, title, page]``, empty when none are credible; and
    the decider's verdicts on them when ``ask`` is given (a candidate banded ``hide`` is left out)."""
    lines, sizes = _lines(document)
    if not sizes:
        return [], []  # no text layer (a scan without OCR)
    body = sizes.most_common(1)[0][0]
    candidates = [line for line in _join(lines, body) if _is_heading(line.text)]
    headings = _without_running_heads(candidates)
    if not headings or len(headings) > MAX_PER_PAGE * max(document.page_count, 1):
        return [], []
    if ask is None:
        return _levels(headings), []
    verdicts = _judge(ask, [_state(heading, lines, body) for heading in headings])
    decisions = [_decision(h, v) for h, v in zip(headings, verdicts, strict=True) if v is not None]
    kept = [h for h, v in zip(headings, verdicts, strict=True) if v is None or v.band != "hide"]
    return (_levels(kept) if kept else []), decisions


def _state(heading: _Line, lines: list[_Line], body: float) -> dict[str, Any]:
    """What the decider sees of a candidate: the line, its neighbours, its page and its size relative to the body."""
    before = lines[max(heading.first - CONTEXT_LINES, 0) : heading.first]
    after = lines[heading.last + 1 : heading.last + 1 + CONTEXT_LINES]
    return {
        "line": heading.text,
        # Capped: one oversized state would get its whole request of 64 refused (evilflowers-worker#10).
        "before": [line.text[:MAX_CHARS] for line in before],
        "after": [line.text[:MAX_CHARS] for line in after],
        "page": heading.page,
        "relative_size": round(heading.size / body, 2),
    }


def _judge(ask: Ask, states: list[dict[str, Any]]) -> list[Verdict | None]:
    """A verdict or ``None`` per state. A judge never costs the book its outline: on any error, every one is kept."""
    try:
        verdicts = list(ask(states))
    except Exception:
        logger.exception("Cannot ask whether %d detected headings are section headings; all are kept", len(states))
        return [None] * len(states)
    if len(verdicts) != len(states):
        logger.error("%d verdicts for %d detected headings; all are kept", len(verdicts), len(states))
        return [None] * len(states)
    return verdicts


def _decision(heading: _Line, verdict: Verdict) -> dict[str, Any]:
    decision = {"page": heading.page, "title": heading.text, **verdict.paragraph_fields()}
    return decision | {"band": verdict.band} if verdict.band else decision


def _join(lines: list[_Line], body: float) -> list[_Line]:
    """One heading is often several lines of one block: its number or ``Chapter 1`` label, then a title that may wrap.

    Join consecutive lines that stand out; the largest size is the heading's. A bare label joins the next line that
    stands out on its page, even in another block.
    """
    joined: list[_Line] = []
    previous: _Line | None = None
    for index, line in enumerate(lines):
        last = joined[-1] if joined else None
        # The line right after a bare label (`3.1`) is its title, even when only bold, not larger (LaTeX).
        after_label = (
            last is not None
            and previous is not None
            and _LABEL.match(last.text) is not None
            and last.page == line.page
            and _stands_out(previous, body)
        )
        titled = after_label and line.bold and line.size >= body
        if not (_stands_out(line, body) or titled):
            previous = line
            continue
        if last and (last.block == line.block or after_label):
            last.text = f"{last.text} {line.text}"
            last.size = max(last.size, line.size)
            last.last = index
        else:
            joined.append(_Line(line.page, line.text, line.size, line.bold, line.block, first=index, last=index))
        previous = line
    return joined


def _without_running_heads(candidates: list[_Line]) -> list[_Line]:
    """Running heads repeat on many pages (often with the page number); headings do not."""

    def key(line: _Line) -> str:
        return _DIGITS.sub("#", line.text.lower())

    pages_of: dict[str, set[int]] = {}
    for line in candidates:
        pages_of.setdefault(key(line), set()).add(line.page)
    return [line for line in candidates if len(pages_of[key(line)]) <= MAX_REPEATS]


def _levels(headings: list[_Line]) -> list[list[Any]]:
    """Numbering decides the level where there is any. An unnumbered heading takes the level that numbered ones of
    its size have, else its size's rank (larger is higher)."""
    by_size: dict[float, Counter[int]] = {}
    for line in headings:
        if (depth := _depth(line.text)) is not None:
            by_size.setdefault(line.size, Counter())[depth] += 1
    ranks = {size: rank for rank, size in enumerate(sorted({line.size for line in headings}, reverse=True), start=1)}
    result = []
    for line in headings:
        level = _depth(line.text)
        if level is None:
            level = by_size[line.size].most_common(1)[0][0] if line.size in by_size else ranks[line.size]
        result.append([min(level, MAX_LEVELS), line.text, line.page])
    return result
