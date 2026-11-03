import os

from dotenv import load_dotenv

load_dotenv()

#: Celery broker: the catalog's Redis.
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/7")

#: The catalog's storage root, mounted read-write (originals are read, crops written).
STORAGE_PATH = os.getenv("STORAGE_PATH", "/mnt/data")

CATALOG_API_URL = os.getenv("CATALOG_API_URL", "http://catalog:8000")
#: API key (bearer JWT) of the catalog user holding `extraction.ingest_extraction`.
CATALOG_API_KEY = os.getenv("CATALOG_API_KEY")

#: Image tag the container runs, recorded in the run's provenance.
IMAGE_TAG = os.getenv("IMAGE_TAG", "dev")

QUEUE = os.getenv("ANALYZER_QUEUE", "evilflowers_analyzer")
TEXT_QUEUE = os.getenv("TEXT_QUEUE", "evilflowers_text_worker")
IMAGE_QUEUE = os.getenv("IMAGE_QUEUE", "evilflowers_image")
EQUATION_QUEUE = os.getenv("EQUATION_QUEUE", "evilflowers_equation")

#: Pages per Docling conversion; bounds memory on long books.
LAYOUT_BATCH_PAGES = int(os.getenv("LAYOUT_BATCH_PAGES", "16"))
#: Docling's own worker threads; 0 lets it decide.
LAYOUT_THREADS = int(os.getenv("LAYOUT_THREADS", "0"))
