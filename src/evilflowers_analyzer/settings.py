"""Configuration of the worker: one typed object read from the environment (and `.env`).

Every field keeps the environment variable name the deployments already set
(``REDIS_URL``, ``ANALYZER_QUEUE``, ``CATALOG_API_KEY``, ``LAYOUT_BATCH_PAGES`` …),
so the move from the old ``settings.py`` module needs no change in the k8s manifests.

Docling's model cache is not a setting here: the Hugging Face libraries read
``HF_HOME`` (``/models`` in the image, a volume) themselves.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # -- Celery -------------------------------------------------------------------------
    #: The broker: the catalog's Redis.
    redis_url: str = "redis://redis:6379/7"
    #: The queue this worker consumes (`evilflowers_analyzer.extract`).
    queue: str = Field("evilflowers_analyzer", validation_alias=AliasChoices("ANALYZER_QUEUE", "QUEUE"))
    #: Seconds before Redis redelivers an unacknowledged message. `extract` acknowledges
    #: late, so this must outlast the layout pass of the longest book.
    broker_visibility_timeout: int = 7200

    # -- Queues of the services the analyzer dispatches to ------------------------------------
    text_queue: str = "evilflowers_text_worker"
    image_queue: str = "evilflowers_image"
    equation_queue: str = "evilflowers_equation"

    # -- Storage and provenance -------------------------------------------------------------
    #: The catalog's storage root: the originals are read here.
    storage_path: Path = Path("/mnt/data")
    #: Built into the image by CI; recorded in every run's provenance and as `service.version`.
    image_tag: str = "dev"

    # -- Catalog ingestion API (IP-018) -------------------------------------------------------
    catalog_api_url: str = "http://catalog:8000"
    #: API key (bearer JWT) of the catalog user holding `extraction.ingest_extraction`.
    catalog_api_key: SecretStr | None = None
    catalog_attempts: int = Field(4, ge=1)
    catalog_timeout: float = 120.0

    # -- Layout (Docling) ---------------------------------------------------------------------
    #: Pages per Docling conversion; bounds memory on long books.
    layout_batch_pages: int = Field(16, ge=1)
    #: Docling's CPU threads.
    layout_threads: int = Field(4, ge=1)

    # -- Logs ---------------------------------------------------------------------------------
    #: `json` (one object per line, what Loki parses) or `text` (Celery's own format, for a terminal).
    log_format: Literal["json", "text"] = "json"

    def source_path(self, source: str) -> Path:
        """Absolute path of a file the catalog names relative to its storage root."""
        return self.storage_path / source


@lru_cache
def get_settings() -> Settings:
    return Settings()
