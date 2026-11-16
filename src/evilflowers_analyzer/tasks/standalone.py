"""``evilflowers_analyzer.standalone.layout``: Docling layout on page images for the books digitalizer (#8).

A leaf task: it sends nothing onward and posts nothing to the catalog. The digitalizer's orchestrator is the system of
record; it sends the task with ``link`` / ``link_error`` callbacks (evilflowers-worker#5) and reads the outcome file.

Message (keyword arguments)::

    {"run_id": "42", "job": "analyze-layout-0003-a1", "token": "…",
     "output_dir": "…/jobs/18-a1", "outcome": "…/jobs/18-a1/outcome.json",
     "leaves": [{"leaf": 3, "image": "digitized/…/0003.tif", "mask": [[10, 2210, 50, 2250]]}]}

- Paths are relative to ``STORAGE_PATH``.
- ``mask``: page-pixel boxes ``[x0, y0, x1, y1]`` (x1, y1 exclusive) the operator removed (stamps, stickers). They are
  painted white on a private copy before Docling reads it; the image itself is never changed.

Output: ``regions.json`` in ``output_dir``, one row per region::

    {"leaf": 3, "kind": "picture", "seq": 0, "bbox": [0.08, 0.41, 0.62, 0.77], "caption": "Obr. 3 …"}

``kind`` is Docling's label, ``seq`` numbers the leaf's regions top to bottom (then left to right), ``bbox`` is in
fractions of the page, origin top-left. Docling has no per-region confidence in its document, so rows carry none (the
digitalizer defaults it to 1.0). Then the outcome file, last and atomically (``evilflowers_worker.outcome``)::

    {"run_id": "42", "token": "…", "status": "done", "error": null, "regions": "…/regions.json", "leaves": [3, 4]}

``leaves`` lists every leaf the layout pass read, those without regions included. A leaf Docling could not read
fails the whole job: the digitalizer applies ``leaves`` as given and would never notice a missing one.

On failure the outcome is written with ``status: failed`` and the error, then the task raises, so ``link_error``
fires. Never on a deliberate requeue: a scale-in stops the process before anything is written, and the redelivery
writes the outcome.
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from celery import Task, shared_task
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# The app's module, so `create_app` has run in this process (see tasks.extraction).
from evilflowers_analyzer import worker as _worker  # noqa: F401  # isort: skip
from evilflowers_analyzer import layout
from evilflowers_analyzer.settings import Settings
from evilflowers_worker.contract import StandaloneJob
from evilflowers_worker.observability import bind, fail, span
from evilflowers_worker.observability import metrics as standard_metrics
from evilflowers_worker.outcome import OutcomeFile, atomic_write, write_outcome
from evilflowers_worker.runtime import JOB_TASK_OPTIONS, run_task, runtime

logger = logging.getLogger(__name__)

TASK = "evilflowers_analyzer.standalone.layout"
#: The job's metric label.
JOB = "standalone.layout"
REGIONS_FILE = "regions.json"


class Leaf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    leaf: int
    #: The cleaned page, relative to ``STORAGE_PATH``.
    image: str
    #: Page-pixel boxes ``[x0, y0, x1, y1]``, x1 and y1 exclusive, to paint white before layout.
    mask: list[tuple[int, int, int, int]] = Field(default_factory=list)


class LayoutJob(StandaloneJob):
    """The kit's ``StandaloneJob`` (evilflowers-worker#5) with the leaves to lay out.

    ``leaves`` is authoritative. The digitalizer sends no ``inputs`` (``layout_inputs`` builds ``leaves`` only), while
    the kit requires them, so a message without ``inputs`` gets the leaves' images.
    """

    TASK: ClassVar[str] = TASK

    leaves: list[Leaf] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _inputs_from_leaves(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("inputs") and isinstance(data.get("leaves"), list):
            images = [leaf.get("image") for leaf in data["leaves"] if isinstance(leaf, dict)]
            return {**data, "inputs": images}
        return data


class LayoutOutcome(OutcomeFile):
    status: Literal["done", "failed"]
    error: str | None = None
    #: ``regions.json``, relative to ``STORAGE_PATH``; set when done.
    regions: str | None = None
    #: Every leaf the layout pass read.
    leaves: list[int] = Field(default_factory=list)


@shared_task(name=TASK, bind=True, **JOB_TASK_OPTIONS)
def standalone_layout(self: Task, **kwargs: Any) -> dict[str, Any]:
    return run_task(self, run, LayoutJob, kwargs, on_capped=give_up, on_invalid=invalid)


def run(message: LayoutJob, delivery: int) -> dict[str, Any]:
    """The layout pass over the message's leaves; the compact summary is what ``link`` receives."""
    settings = cast(Settings, runtime().settings)
    with bind(run_id=message.run_id, job=message.job):
        try:
            rows, leaves = find_regions(message, settings)
            message.prepare_output(settings)
            regions = message.output_file(settings, REGIONS_FILE)
            atomic_write(regions, json.dumps(rows).encode())
            outcome = LayoutOutcome(
                run_id=message.run_id,
                token=message.token,
                status="done",
                regions=regions.relative_to(settings.storage_path.resolve()).as_posix(),
                leaves=leaves,
            )
            write_outcome(settings, message.outcome, outcome)
        except Exception as error:
            logger.exception("Standalone layout job %s of run %s failed", message.job, message.run_id)
            fail(error)
            _write_failed(settings, message.outcome, message.run_id, message.token, f"{type(error).__name__}: {error}")
            standard_metrics.record_job(JOB, "failed")
            raise
    logger.info("Standalone layout job %s: %d regions on %d leaves", message.job, len(rows), len(leaves))
    standard_metrics.record_job(JOB, "done")
    return {"job": message.job, "regions": len(rows)}


def find_regions(message: LayoutJob, settings: Settings) -> tuple[list[dict[str, Any]], list[int]]:
    """The regions of every leaf (rows of ``regions.json``) and the leaves read; raises when none could be read."""
    failed: set[int] = set()
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="analyzer-layout-") as private, span("analyzer.standalone.layout"):
        images = [_readable(leaf, settings, Path(private)) for leaf in message.leaves]
        batches = layout.layout_batches(
            layout.ImageSource(images),
            settings,
            classify=layout.label,
            kinds=(),
            on_failed=lambda first, _last: failed.add(first),
        )
        for first, _last, found in batches:
            leaf = message.leaves[first - 1].leaf
            regions = sorted(
                ((kind, region) for kind, items in found.items() for region in items),
                key=lambda pair: (pair[1]["bbox"]["y0"], pair[1]["bbox"]["x0"]),
            )
            rows.extend(_row(leaf, seq, kind, region) for seq, (kind, region) in enumerate(regions))
    if failed:
        # The digitalizer applies every leaf in `leaves` and does not look for missing ones: a leaf left out would
        # silently have no layout. The whole job fails, and the orchestrator retries it by its own policy.
        lost = [leaf.leaf for index, leaf in enumerate(message.leaves, start=1) if index in failed]
        raise RuntimeError(f"Layout failed on leaves {lost} of {len(message.leaves)}")
    return rows, [leaf.leaf for leaf in message.leaves]


def _row(leaf: int, seq: int, kind: str, region: dict[str, Any]) -> dict[str, Any]:
    box = region["bbox"]
    return {
        "leaf": leaf,
        "kind": kind,
        "seq": seq,
        "bbox": [box["x0"], box["y0"], box["x1"], box["y1"]],
        "caption": region["caption"],
    }


def _readable(leaf: Leaf, settings: Settings, private: Path) -> str:
    """The image Docling reads: the leaf itself, or a private copy with its mask painted white."""
    source = settings.storage_file(leaf.image)
    if not leaf.mask:
        return str(source)
    return str(paint_white(source, leaf.mask, private / f"leaf-{leaf.leaf}.png"))


def paint_white(source: Path, boxes: list[tuple[int, int, int, int]], target: Path) -> Path:
    """Write ``source`` with ``boxes`` (x1, y1 exclusive) painted white to ``target``; ``source`` is only read."""
    from PIL import Image, ImageDraw

    with Image.open(source) as image:
        copy = image.convert("RGB") if image.mode not in ("1", "L", "RGB") else image.copy()
    white: int | tuple[int, ...] = {"1": 1, "L": 255}.get(copy.mode, (255, 255, 255))
    draw = ImageDraw.Draw(copy)
    for x0, y0, x1, y1 in boxes:
        if x1 > x0 and y1 > y0:
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=white)
    copy.save(target, format="PNG")
    return target


def give_up(message: LayoutJob, delivery: int) -> dict[str, Any]:
    """Past ``EXTRACT_MAX_PASSES``: the job killed its worker every time. Recorded failed, then raised."""
    settings = cast(Settings, runtime().settings)
    error = f"Gave up after {delivery - 1} deliveries: the worker died or was stopped during each of them"
    _write_failed(settings, message.outcome, message.run_id, message.token, error)
    standard_metrics.record_job(JOB, "failed")
    raise RuntimeError(error)


def invalid(kwargs: dict[str, Any], error: ValidationError) -> dict[str, Any]:
    """A message that does not validate: a failed outcome when it names one, then the error (never retried)."""
    standard_metrics.record_job(JOB, "failed")
    names = (kwargs.get("outcome"), kwargs.get("run_id"), kwargs.get("token"))
    if all(isinstance(name, str) and name for name in names):
        outcome, run_id, token = cast(tuple[str, str, str], names)
        _write_failed(cast(Settings, runtime().settings), outcome, run_id, token, f"Invalid message: {error}")
    raise error


def _write_failed(settings: Settings, relative: str, run_id: str, token: str, error: str) -> None:
    """Best effort: the failure is already logged; a second one must not hide it."""
    try:
        write_outcome(settings, relative, LayoutOutcome(run_id=run_id, token=token, status="failed", error=error))
    except Exception:
        logger.exception("Cannot write the failed outcome of run %s", run_id)
