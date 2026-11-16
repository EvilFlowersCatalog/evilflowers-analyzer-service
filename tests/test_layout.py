from types import SimpleNamespace

from evilflowers_analyzer import layout


def test_fractions_clamp_and_order():
    box = layout.fractions(500, 100, 50, 900, 500, 800)
    assert box == {"x0": 0.1, "y0": 0.125, "x1": 1.0, "y1": 1.0}


def test_fractions_of_a_quarter():
    assert layout.fractions(0, 0, 250, 400, 500, 800) == {"x0": 0.0, "y0": 0.0, "x1": 0.5, "y1": 0.5}


def test_batches_cover_every_page_once():
    assert layout.batches(35, 16) == [(1, 16), (17, 32), (33, 35)]
    assert layout.batches(16, 16) == [(1, 16)]
    assert layout.batches(0, 16) == []


def test_docling_items_map_to_region_kinds():
    from docling_core.types.doc import DocItemLabel, PictureItem, TableItem, TextItem

    def item(cls, label):
        return cls.model_construct(label=label, self_ref="#/x", prov=[])

    assert layout._kind(item(PictureItem, DocItemLabel.PICTURE)) == "figure"
    assert layout._kind(item(TableItem, DocItemLabel.TABLE)) == "table"
    assert layout._kind(item(TextItem, DocItemLabel.FORMULA)) == "equation"
    assert layout._kind(item(TextItem, DocItemLabel.TEXT)) is None


# -- find_regions with a fake converter ---------------------------------------------------------


def box(left, top, right, bottom):
    return SimpleNamespace(to_top_left_origin=lambda page_height: SimpleNamespace(l=left, t=top, r=right, b=bottom))


def fake_item(kind, page, bbox, caption=None):
    return SimpleNamespace(
        kind=kind,
        prov=[SimpleNamespace(page_no=page, bbox=bbox)],
        caption_text=lambda document: caption or "",
    )


class FakeConverter:
    """Page 1: a figure and an equation; page 2: two figures; pages 3-4 raise."""

    def __init__(self):
        self.ranges = []

    def convert(self, path, page_range, raises_on_error):
        self.ranges.append(page_range)
        if page_range[0] > 2:
            raise RuntimeError("bad pages")
        size = SimpleNamespace(width=500, height=800)
        items = [
            fake_item("figure", 1, box(0, 0, 250, 400), "Fig. 1"),
            fake_item("equation", 1, box(50, 500, 450, 560)),
            fake_item(None, 1, box(0, 0, 1, 1)),
            fake_item("figure", 2, box(0, 0, 500, 800)),
            fake_item("figure", 2, box(0, 0, 100, 80)),
        ]
        document = SimpleNamespace(
            pages={1: SimpleNamespace(size=size), 2: SimpleNamespace(size=size)},
            iterate_items=lambda: [(item, 0) for item in items],
        )
        return SimpleNamespace(document=document)


def test_find_regions_numbers_regions_per_page_and_skips_bad_batches(settings, monkeypatch):
    converter = FakeConverter()
    monkeypatch.setattr(layout, "_converter", converter)
    monkeypatch.setattr(layout, "_kind", lambda item: item.kind)
    settings = settings.model_copy(update={"layout_batch_pages": 2})

    regions = layout.find_regions("/book.pdf", 4, settings)

    assert converter.ranges == [(1, 2), (3, 4)]
    assert regions["table"] == []
    assert regions["equation"] == [
        {"page": 1, "seq": 0, "bbox": {"x0": 0.1, "y0": 0.625, "x1": 0.9, "y1": 0.7}, "caption": None}
    ]
    assert [(region["page"], region["seq"], region["caption"]) for region in regions["figure"]] == [
        (1, 0, "Fig. 1"),
        (2, 0, None),
        (2, 1, None),
    ]


def test_layout_batches_yields_every_batch_as_it_is_converted_a_bad_one_empty(settings, monkeypatch):
    converter = FakeConverter()
    monkeypatch.setattr(layout, "_converter", converter)
    monkeypatch.setattr(layout, "_kind", lambda item: item.kind)
    settings = settings.model_copy(update={"layout_batch_pages": 2})

    batches = layout.layout_batches(layout.PdfSource("/book.pdf", 4), settings)
    first, last, regions = next(batches)
    # Yielded before the next batch is even converted: the caller can send it at once.
    assert (first, last, converter.ranges) == (1, 2, [(1, 2)])
    assert [len(regions[kind]) for kind in layout.KINDS] == [3, 0, 1]

    assert list(batches) == [(3, 4, {"figure": [], "table": [], "equation": []})]


# -- page images (#8) ---------------------------------------------------------------------------


class FakeImageConverter:
    """Each image is a one-page document with one picture and one formula; ``bad.png`` raises."""

    def __init__(self):
        self.calls = []

    def convert(self, path, raises_on_error):
        self.calls.append(path)
        if path == "bad.png":
            raise RuntimeError("cannot read image")
        size = SimpleNamespace(width=1000, height=2000)
        items = [
            fake_item("picture", 1, box(100, 200, 500, 1000), "Obr. 3"),
            fake_item("formula", 1, box(0, 1800, 1000, 1900)),
        ]
        document = SimpleNamespace(
            pages={1: SimpleNamespace(size=size)}, iterate_items=lambda: [(item, 0) for item in items]
        )
        return SimpleNamespace(document=document)


def test_image_source_converts_one_image_per_batch_and_maps_its_page(settings, monkeypatch):
    converter = FakeImageConverter()
    monkeypatch.setattr(layout, "_converter", converter)
    failed = []

    batches = list(
        layout.layout_batches(
            layout.ImageSource(["a.png", "bad.png", "c.png"]),
            settings,
            classify=lambda item: item.kind,
            kinds=(),
            on_failed=lambda first, last: failed.append((first, last)),
        )
    )

    assert converter.calls == ["a.png", "bad.png", "c.png"]
    assert [(first, last) for first, last, _regions in batches] == [(1, 1), (2, 2), (3, 3)]
    assert failed == [(2, 2)]
    assert batches[1][2] == {}
    picture = batches[2][2]["picture"]
    bbox = {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}
    assert picture == [{"page": 3, "seq": 0, "bbox": bbox, "caption": "Obr. 3"}]
    assert batches[2][2]["formula"][0]["caption"] is None


def test_docling_labels_are_kept_as_they_are_and_groups_skipped():
    from docling_core.types.doc import DocItemLabel, GroupItem, PictureItem, TextItem

    def item(cls, label):
        return cls.model_construct(label=label, self_ref="#/x", prov=[])

    assert layout.label(item(PictureItem, DocItemLabel.PICTURE)) == "picture"
    assert layout.label(item(TextItem, DocItemLabel.SECTION_HEADER)) == "section_header"
    assert layout.label(GroupItem.model_construct(self_ref="#/g")) is None


def test_one_converter_reads_pdfs_and_images(monkeypatch):
    """Both formats share the options, so Docling builds one pipeline (one layout model) for both."""
    from docling.datamodel.base_models import InputFormat

    converter = layout._build_converter(2)
    pdf = converter.format_to_options[InputFormat.PDF]
    image = converter.format_to_options[InputFormat.IMAGE]
    assert pdf.pipeline_options is image.pipeline_options
    assert pdf.pipeline_cls is image.pipeline_cls
