# evilflowers-analyzer-service

Entry point of an extraction run (IP-018 Intelligent Viewer, catalog
`docs/proposals/posts/ip-018-intelligent-viewer.md`).

The catalog queues `evilflowers_analyzer.extract` on the `evilflowers_analyzer` queue when a PDF is
stored (`apps/extraction/services.py`). The analyzer:

1. opens the PDF with PyMuPDF and starts the run on the catalog with the page count, page sizes and
   the jobs it dispatches (`paragraphs`, `figures`, `equations`, `tables`);
2. sends the paragraph job to text-service at once (it needs only the text layer);
3. runs one Docling layout pass (CPU, no OCR, pages in batches) and sends each kind of region to the
   service that enriches it.

| Region kind | Task | Queue |
|---|---|---|
| paragraphs | `evilflowers_text_worker.paragraphs` | `evilflowers_text_worker` |
| figure | `evilflowers_image.figures` | `evilflowers_image` |
| equation | `evilflowers_equation.equations` | `evilflowers_equation` |
| table | `evilflowers_text_worker.tables` | `evilflowers_text_worker` |

Every task gets `run_id`, `source` (the PDF relative to `STORAGE_PATH`) and `output_dir` (where its
crops go, relative to `STORAGE_PATH`); region tasks also get `regions`: `{page, seq, bbox, caption}`
with `bbox` as page fractions, origin top-left, on the original file's 1-based page. Each service
reports its job to `{CATALOG_API_URL}/extraction/v1/runs/{run_id}/...` (`src/catalog.py`); the
analyzer reports job `analyzer` as failed when it cannot open the document.

## Configuration

See `.env.sample`. `CATALOG_API_KEY` is an API key of a catalog user holding the
`extraction.ingest_extraction` permission. Docling's layout model is downloaded to `HF_HOME`
(`/models`, a volume) on first use.

## Development

```bash
make install
make test
```

CI publishes `ghcr.io/evilflowerscatalog/evilflowers-analyzer-service`: `:main` on every push to
`main`, semver tags on `v*.*.*`.
