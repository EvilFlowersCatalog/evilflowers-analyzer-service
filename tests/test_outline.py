"""The outline the paragraphs fall under (#5): bookmarks, else headings detected on the text layer."""

import pymupdf
import pytest

from evilflowers_analyzer import outline
from evilflowers_worker.judges import Verdict

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


class Bookmarked:
    """A document whose bookmarks are exactly these, as ``get_toc`` returns them."""

    def __init__(self, toc):
        self.toc = toc

    def get_toc(self, simple=True):
        return self.toc


def test_control_characters_are_stripped_from_bookmark_titles():
    found = outline.find(Bookmarked([[1, "\x00I\x00n\x00t\x00r\x00o", 1], [2, "Back\x7fground\x1f \t", 2]]))

    assert found.entries == [[1, "Intro", 1], [2, "Background", 2]]


def test_a_bookmark_of_only_control_characters_is_dropped():
    found = outline.find(Bookmarked([[1, "\x00\x00\x01", 1], [1, "Methods", 2]]))

    assert found == outline.Outline("bookmarks", [[1, "Methods", 2]])


def test_control_characters_are_stripped_from_detected_headings():
    spans = [
        {"text": "\x001\x00.\x001", "size": 12, "flags": 16},
        {"text": " Entropy\x7f\n", "size": 12, "flags": 16},
    ]

    assert outline._line(spans, page=1, block=(0, 0)).text == "1.1 Entropy"


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


def verdict(answer: str, p: float, band=None):
    return Verdict(
        question="section_heading", verdict=answer, p=p, model="laya-multilingual", revision="r1", band=band
    )


class Asked:
    """The decider through ``Panel.ask_batch``: records the states, answers from ``answers`` (by line)."""

    def __init__(self, answers=None, error=None):
        self.answers = answers or {}
        self.error = error
        self.states = []

    def __call__(self, states):
        self.states.extend(states)
        if self.error:
            raise self.error
        return [self.answers.get(state["line"]) for state in states]


@pytest.fixture
def chapters(pdf):
    page(pdf, [("Chapter 1", 14, True), ("Information", 16, True)])
    page(pdf, [("1.1 Entropy", 12, True)])
    page(pdf, [("Figure 3 Channel capacity", 12, True)])
    return pdf


def test_a_book_with_bookmarks_is_never_asked(pdf):
    page(pdf, [("1.1 Entropy", 12, True)])
    pdf.set_toc([[1, "Entropy", 1]])
    asked = Asked()

    assert outline.find(pdf, asked).source == "bookmarks"
    assert asked.states == []


def test_each_detected_heading_is_asked_with_its_neighbours_page_and_relative_size(chapters):
    asked = Asked()

    outline.find(chapters, asked)

    assert [state["line"] for state in asked.states] == [
        "Chapter 1 Information",
        "1.1 Entropy",
        "Figure 3 Channel capacity",
    ]
    first, second = asked.states[0], asked.states[1]
    assert first["page"] == 1
    assert first["before"] == []
    assert first["after"] == [BODY[0][:80], BODY[1][:80]]
    assert first["relative_size"] == 1.6
    assert second["page"] == 2
    assert len(second["before"]) == 2  # the end of page 1's body text
    assert second["relative_size"] == 1.2


def test_without_thresholds_every_candidate_stays_with_its_verdict(chapters):
    asked = Asked({"Figure 3 Channel capacity": verdict("no", 0.97), "1.1 Entropy": verdict("yes", 0.9)})

    found = outline.find(chapters, asked)

    assert [title for _level, title, _page in found.entries] == [
        "Chapter 1 Information",
        "1.1 Entropy",
        "Figure 3 Channel capacity",
    ]
    # No answer about the chapter: it stays, and nothing is recorded for it.
    assert found.decisions == [
        {"page": 2, "title": "1.1 Entropy", "decision": "yes", "p": 0.9, "decider_model": "laya-multilingual@r1"},
        {
            "page": 3,
            "title": "Figure 3 Channel capacity",
            "decision": "no",
            "p": 0.97,
            "decider_model": "laya-multilingual@r1",
        },
    ]


def test_a_candidate_banded_hide_is_dropped_but_its_verdict_kept(chapters):
    asked = Asked({"Figure 3 Channel capacity": verdict("no", 0.97, band="hide")})

    found = outline.find(chapters, asked)

    assert found.entries == [[1, "Chapter 1 Information", 1], [2, "1.1 Entropy", 2]]
    assert found.decisions[0]["band"] == "hide"
    assert found.decisions[0]["title"] == "Figure 3 Channel capacity"


def test_when_every_candidate_is_dropped_the_book_has_no_outline(pdf):
    page(pdf, [("Figure 3 Channel capacity", 12, True)])

    found = outline.find(pdf, Asked({"Figure 3 Channel capacity": verdict("no", 0.99, band="hide")}))

    assert found.source == "none"
    assert found.entries == []
    assert len(found.decisions) == 1


def test_a_failing_judge_keeps_the_outline_as_detected(chapters):
    found = outline.find(chapters, Asked(error=ValueError("decider-vision asks one state per request")))

    assert found.source == "headings"
    assert len(found.entries) == 3
    assert found.decisions == []


def test_a_wrong_number_of_verdicts_keeps_every_candidate(chapters):
    found = outline.find(chapters, lambda states: [verdict("no", 0.99, band="hide")])

    assert len(found.entries) == 3


def test_an_unreadable_document_is_reported_failed():
    class Broken:
        def get_toc(self, simple=True):
            raise RuntimeError("damaged xref")

    assert outline.find(Broken()) == outline.Outline("failed")
