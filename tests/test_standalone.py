"""``evilflowers_analyzer.standalone.layout`` (#8): Docling layout on leaf images for the books digitalizer."""

import json
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from evilflowers_analyzer import layout
from evilflowers_analyzer.tasks import standalone

JOB_DIR = "digitized/book/_studio/jobs/18-a1"


def box(left, top, right, bottom):
    return SimpleNamespace(to_top_left_origin=lambda page_height: SimpleNamespace(l=left, t=top, r=right, b=bottom))


def item(kind, bbox, caption=None):
    return SimpleNamespace(
        kind=kind, prov=[SimpleNamespace(page_no=1, bbox=bbox)], caption_text=lambda document: caption or ""
    )


class FakeImageConverter:
    """Every image: a formula low on the page, then a captioned picture above it. ``broken`` images raise."""

    def __init__(self):
        self.read = []

    def convert(self, path, raises_on_error):
        with Image.open(path) as image:
            self.read.append((path, image.getpixel((0, 0))))
        if "broken" in path:
            raise RuntimeError("cannot read image")
        items = [item("formula", box(0, 900, 100, 950)), item("picture", box(10, 100, 60, 500), "Obr. 3")]
        document = SimpleNamespace(
            pages={1: SimpleNamespace(size=SimpleNamespace(width=100, height=1000))},
            iterate_items=lambda: [(entry, 0) for entry in items],
        )
        return SimpleNamespace(document=document)


@pytest.fixture
def converter(monkeypatch, settings):
    fake = FakeImageConverter()
    monkeypatch.setattr(layout, "_converter", fake)
    monkeypatch.setattr(layout, "label", lambda entry: entry.kind)
    monkeypatch.setattr(standalone, "runtime", lambda: SimpleNamespace(settings=settings))
    return fake


def leaf_image(settings, name, color=(0, 0, 0)):
    path = settings.storage_path / "digitized/book" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (100, 1000), color).save(path)
    return f"digitized/book/{name}"


def message(**leaves_and_overrides):
    fields = {
        "run_id": "42",
        "job": "analyze-layout-0003-a1",
        "token": "secret",
        "output_dir": JOB_DIR,
        "outcome": f"{JOB_DIR}/outcome.json",
    }
    return {**fields, **leaves_and_overrides}


def read_outcome(settings):
    return json.loads((settings.storage_path / JOB_DIR / "outcome.json").read_text())


def test_the_message_is_the_kits_standalone_job_and_inputs_default_to_the_leaves():
    """The digitalizer sends `leaves` only; the kit's `StandaloneJob` requires `inputs`."""
    from evilflowers_worker.contract import StandaloneJob

    leaves = [{"leaf": 3, "image": "digitized/book/0003.tif"}, {"leaf": 4, "image": "digitized/book/0004.tif"}]
    job = standalone.LayoutJob.model_validate(message(leaves=leaves))
    assert isinstance(job, StandaloneJob)
    assert job.inputs == ["digitized/book/0003.tif", "digitized/book/0004.tif"]

    given = standalone.LayoutJob.model_validate(message(leaves=leaves, inputs=["digitized/book/0003.tif"]))
    assert given.inputs == ["digitized/book/0003.tif"]
    assert [leaf.leaf for leaf in given.leaves] == [3, 4]


def test_writes_regions_then_the_outcome_and_returns_a_compact_summary(settings, converter):
    leaves = [
        {"leaf": 3, "image": leaf_image(settings, "0003.png")},
        {"leaf": 4, "image": leaf_image(settings, "0004.png"), "mask": [[0, 0, 10, 10]]},
    ]

    summary = standalone.run(standalone.LayoutJob.model_validate(message(leaves=leaves)), 1)

    assert summary == {"job": "analyze-layout-0003-a1", "regions": 4}
    rows = json.loads((settings.storage_path / JOB_DIR / "regions.json").read_text())
    assert rows[:2] == [
        {"leaf": 3, "kind": "picture", "seq": 0, "bbox": [0.1, 0.1, 0.6, 0.5], "caption": "Obr. 3"},
        {"leaf": 3, "kind": "formula", "seq": 1, "bbox": [0.0, 0.9, 1.0, 0.95], "caption": None},
    ]
    assert [(row["leaf"], row["seq"]) for row in rows[2:]] == [(4, 0), (4, 1)]
    assert read_outcome(settings) == {
        "run_id": "42",
        "token": "secret",
        "status": "done",
        "error": None,
        "regions": f"{JOB_DIR}/regions.json",
        "leaves": [3, 4],
    }
    assert not list((settings.storage_path / JOB_DIR).glob("*.partial"))


def test_the_mask_is_painted_on_a_private_copy_never_on_the_leaf(settings, converter):
    image = leaf_image(settings, "0004.png")
    leaves = [{"leaf": 4, "image": image, "mask": [[0, 0, 10, 10]]}]

    standalone.run(standalone.LayoutJob.model_validate(message(leaves=leaves)), 1)

    ((path, corner),) = converter.read
    assert corner == (255, 255, 255)
    assert not path.startswith(str(settings.storage_path))
    with Image.open(settings.storage_path / image) as original:
        assert original.getpixel((0, 0)) == (0, 0, 0)


def test_a_leaf_without_a_mask_is_read_in_place(settings, converter):
    image = leaf_image(settings, "0003.png")
    standalone.run(standalone.LayoutJob.model_validate(message(leaves=[{"leaf": 3, "image": image}])), 1)
    assert converter.read[0][0] == str((settings.storage_path / image).resolve())


def test_paint_white_treats_the_far_edges_as_exclusive(tmp_path):
    source = tmp_path / "leaf.png"
    Image.new("L", (20, 20), 0).save(source)

    standalone.paint_white(source, [(2, 3, 5, 6)], tmp_path / "copy.png")

    with Image.open(tmp_path / "copy.png") as copy:
        assert copy.getpixel((2, 3)) == 255 and copy.getpixel((4, 5)) == 255
        assert copy.getpixel((5, 5)) == 0 and copy.getpixel((4, 6)) == 0
    with Image.open(source) as original:
        assert original.getpixel((2, 3)) == 0


def test_a_leaf_docling_cannot_read_fails_the_job_with_a_failed_outcome(settings, converter):
    """The digitalizer applies `leaves` as given; a leaf silently left out would never get its layout."""
    leaves = [
        {"leaf": 3, "image": leaf_image(settings, "0003.png")},
        {"leaf": 4, "image": leaf_image(settings, "broken-0004.png")},
    ]

    with pytest.raises(RuntimeError, match=r"leaves \[4\] of 2"):
        standalone.run(standalone.LayoutJob.model_validate(message(leaves=leaves)), 1)

    outcome = read_outcome(settings)
    assert (outcome["status"], outcome["regions"], outcome["leaves"]) == ("failed", None, [])
    assert outcome["error"] == "RuntimeError: Layout failed on leaves [4] of 2"
    assert not (settings.storage_path / JOB_DIR / "regions.json").exists()


def test_an_output_dir_outside_the_storage_fails_the_job(settings, converter):
    leaves = [{"leaf": 3, "image": leaf_image(settings, "0003.png")}]
    with pytest.raises(ValueError, match="escapes"):
        standalone.run(standalone.LayoutJob.model_validate(message(leaves=leaves, output_dir="../elsewhere")), 1)
    assert read_outcome(settings)["status"] == "failed"


def test_past_the_delivery_cap_the_failed_outcome_is_written_then_raised(settings, converter):
    job = standalone.LayoutJob.model_validate(message(leaves=[{"leaf": 3, "image": "x.png"}]))
    with pytest.raises(RuntimeError, match="Gave up after 5 deliveries"):
        standalone.give_up(job, 6)
    assert read_outcome(settings)["status"] == "failed"


def test_an_invalid_message_writes_a_failed_outcome_when_it_names_one(settings, converter):
    with pytest.raises(ValidationError):
        standalone.standalone_layout.run(**message(leaves=[]))
    outcome = read_outcome(settings)
    assert outcome["status"] == "failed" and outcome["error"].startswith("Invalid message")


# -- the app ---------------------------------------------------------------------------------------


def test_the_standalone_task_is_registered_routed_and_acknowledged_late():
    from evilflowers_analyzer.worker import app

    app.loader.import_default_modules()
    task = app.tasks[standalone.TASK]
    assert task.acks_late and task.reject_on_worker_lost
    assert app.amqp.router.route({}, standalone.TASK)["queue"].name == "evilflowers_analyzer.standalone"
    assert app.amqp.router.route({}, "evilflowers_analyzer.extract")["queue"].name == "evilflowers_analyzer"


def test_the_standalone_deployment_consumes_both_standalone_queues(settings, monkeypatch):
    from evilflowers_analyzer.settings import Settings
    from evilflowers_analyzer.worker import create_app

    monkeypatch.setenv("ANALYZER_STANDALONE", "1")
    standalone_settings = Settings(_env_file=None)
    assert standalone_settings.catalog_api_key is None

    custom = create_app(standalone_settings)
    assert [queue.name for queue in custom.conf.task_queues] == [
        "evilflowers_analyzer.standalone.interactive",
        "evilflowers_analyzer.standalone",
    ]
    # Restore the module's app as the process's runtime for the other tests.
    create_app()
