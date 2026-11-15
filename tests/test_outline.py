"""The outline the paragraphs fall under (#5): bookmarks, else headings detected on the text layer."""

import pymupdf
import pytest

from evilflowers_analyzer import outline

BODY = (
    "Entropy measures the uncertainty of a random variable and bounds how far a source can be compressed",
    "without loss. A code that meets the bound is optimal; one that does not wastes bits on every symbol.",
)


def page(pdf, lines: list[tuple[str, float, bool]], running_head: str | None = None):
    """A page of body text with the given ``(text, size, bold)`` lines on top of it."""
    current = pdf.new_page(width=420, height=600)
    if running_head:
        current.insert_text((40, 25), running_head, fontsize=8)
    y = 80
    for text, size, bold in lines:
        current.insert_text((40, y), text, fontsize=size, fontname="hebo" if bold else "helv")
        y += size * 1.8
    for _ in range(12):
        for line in BODY:
            current.insert_text((40, y), line[:80], fontsize=10)
            y += 14
    return current


@pytest.fixture
def pdf():
    with pymupdf.open() as document:
        yield document


def test_bookmarks_win(pdf):
    page(pdf, [])
    page(pdf, [])
    pdf.set_toc([[1, "  Introduction ", 1], [2, "Background", 2], [1, "", 2]])

    found = outline.find(pdf)

    assert found.source == "bookmarks"
    assert found.entries == [[1, "Introduction", 1], [2, "Background", 2]]


def test_without_bookmarks_headings_are_found_by_size_and_numbering(pdf):
    page(pdf, [("Chapter 1", 14, True), ("Information", 16, True)], running_head="Information Theory")
    page(pdf, [("1.1 Entropy", 12, True)], running_head="Information Theory 2")
    page(pdf, [("1.1.1 Joint entropy", 10, True)], running_head="Information Theory 3")
    page(pdf, [("Chapter 2", 14, True), ("Coding", 16, True)], running_head="Information Theory 4")

    found = outline.find(pdf)

    assert found.source == "headings"
    assert found.entries == [
        [1, "Chapter 1 Information", 1],
        [2, "1.1 Entropy", 2],
        [3, "1.1.1 Joint entropy", 3],
        [1, "Chapter 2 Coding", 4],
    ]


def test_a_latex_subsection_is_a_bold_number_then_a_bold_title_at_body_size(pdf):
    page(pdf, [("1 Introduction", 12, True)])
    page(pdf, [("1.1", 10, True), ("Encoder and Decoder Stacks", 10, True)])

    assert outline.find(pdf).entries == [[1, "1 Introduction", 1], [2, "1.1 Encoder and Decoder Stacks", 2]]


def test_an_unnumbered_heading_takes_the_level_of_numbered_ones_its_size(pdf):
    page(pdf, [("1 Information", 16, True)])
    page(pdf, [("Preface", 16, True)])

    assert outline.find(pdf).entries == [[1, "1 Information", 1], [1, "Preface", 2]]


def test_a_document_of_body_text_has_none(pdf):
    page(pdf, [])
    page(pdf, [])

    found = outline.find(pdf)

    assert found == outline.Outline("none")


def test_a_scan_without_a_text_layer_has_none(pdf):
    pdf.new_page()

    assert outline.find(pdf).source == "none"


def test_an_unreadable_document_is_reported_failed():
    class Broken:
        def get_toc(self, simple=True):
            raise RuntimeError("damaged xref")

    assert outline.find(Broken()) == outline.Outline("failed")
