"""The analyzer's configuration: the kit's ``WorkerSettings`` plus the queues it sends to and the layout pass.

Every field keeps the environment variable name the deployments already set (``REDIS_URL``, ``ANALYZER_QUEUE``,
``CATALOG_API_KEY``, ``LAYOUT_BATCH_PAGES``, ``EXTRACT_MAX_PASSES`` …).

Docling's model cache is not a setting here: the Hugging Face libraries read ``HF_HOME`` (``/models`` in the image,
a volume) themselves.
"""

from functools import lru_cache

from pydantic import AliasChoices, Field

from evilflowers_worker.settings import WorkerSettings


class Settings(WorkerSettings):
    # -- What it consumes -------------------------------------------------------------------------------------------
    #: The queue of `evilflowers_analyzer.extract`.
    queue: str = Field("evilflowers_analyzer", validation_alias=AliasChoices("ANALYZER_QUEUE", "QUEUE"))
    #: 15 min lets a typical book (3-10 min) finish on scale-in; only long ones are cut and requeued. Must stay under
    #: the pod's termination grace.
    worker_soft_shutdown_timeout: float = Field(900.0, ge=0)
    #: Passes of one run's `extract` before the run is failed instead of started again (#3). A pass is a delivery of
    #: the same message: the worker died (OOM) or was stopped during the one before.
    max_deliveries: int = Field(5, ge=0, validation_alias=AliasChoices("EXTRACT_MAX_PASSES", "MAX_DELIVERIES"))

    # -- Queues of the services the analyzer sends jobs to ------------------------------------------------------------
    #: Each text job has its own queue (#4, evilflowers-text-service#13). The old shared ``TEXT_QUEUE`` is
    #: deliberately not read: a manifest still setting it to ``evilflowers_text_worker`` would silently keep both on
    #: the legacy queue.
    text_tables_queue: str = "evilflowers.text.tables"
    text_paragraphs_queue: str = "evilflowers.text.paragraphs"
    image_queue: str = "evilflowers_image"
    equation_queue: str = "evilflowers_equation"

    # -- Layout (Docling) -----------------------------------------------------------------------------------------
    #: Pages per Docling conversion; bounds memory on long books.
    layout_batch_pages: int = Field(16, ge=1)
    #: Docling's CPU threads; must equal the pod's CPU limit (knowledge Known-Issues 45).
    layout_threads: int = Field(4, ge=1)
    #: Send figures, equations and tables per layout batch, each its own job (``equations:0001-0008``), as soon as the
    #: batch is laid out, instead of once per book after the whole pass (#2). Needs services that report under the
    #: ``job`` kwarg (text#16, image#5); until they all do, keep it off.
    dispatch_per_batch: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
