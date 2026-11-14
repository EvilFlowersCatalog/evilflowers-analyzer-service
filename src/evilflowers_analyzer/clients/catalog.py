"""Reporting to the catalog's extraction ingestion API (IP-018).

Every evilflowers-*-service posts its results to
``{CATALOG_API_URL}/extraction/v1/runs/{run_id}/...`` with the API key of the
catalog's service user. The services never write to the catalog database.

The analyzer only starts a run (``/start``) and, when it cannot go on, reports
jobs as failed (``/jobs/<name>``); regions and paragraphs are posted by the
services it dispatches to.
"""

import logging
import time
from collections.abc import Iterable
from typing import Any

import requests

from evilflowers_analyzer.clients.http import session
from evilflowers_analyzer.observability import metrics
from evilflowers_analyzer.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: The catalog keeps at most this much of a job's error message.
ERROR_LIMIT = 2000


class RunNotRunning(Exception):
    """The catalog no longer accepts results for this run (409): it was reset, started or finished."""


class CatalogClient:
    def __init__(self, run_id: str, *, settings: Settings | None = None, http: requests.Session | None = None):
        self.settings = settings or get_settings()
        self.base = f"{self.settings.catalog_api_url.rstrip('/')}/extraction/v1/runs/{run_id}"
        self.http = http or session(self.settings.catalog_api_key)

    def _post(self, path: str, payload: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST with retries on connection errors and 5xx (exponential backoff); 409 → `RunNotRunning`."""
        attempts = self.settings.catalog_attempts
        for attempt in range(1, attempts + 1):
            try:
                response = self.http.post(
                    f"{self.base}{path}", json=payload, params=params, timeout=self.settings.catalog_timeout
                )
            except requests.RequestException:
                if attempt == attempts:
                    raise
                logger.warning("Catalog unreachable at %s%s, retrying", self.base, path)
                reason = "unreachable"
            else:
                if response.status_code == 409:
                    raise RunNotRunning(response.text)
                if response.status_code < 500 or attempt == attempts:
                    response.raise_for_status()
                    return response.json() if response.content else {}
                logger.warning("Catalog answered %s on %s, retrying", response.status_code, path)
                reason = str(response.status_code)
            metrics.CATALOG_RETRIES.add(1, {"path": path, "reason": reason})
            time.sleep(2**attempt)
        return {}  # pragma: no cover - the loop always returns or raises

    def start(
        self, page_count: int, page_sizes: list[list[float]], jobs: Iterable[str], provenance: dict
    ) -> int | None:
        """Start the run; returns its new `attempt` (EvilFlowersCatalog#140), `None` from a catalog without one."""
        body = self._post(
            "/start",
            {"page_count": page_count, "page_sizes": page_sizes, "jobs": list(jobs), "provenance": provenance},
        )
        attempt = (body.get("response") or {}).get("attempt") if isinstance(body, dict) else None
        return attempt if isinstance(attempt, int) else None

    def job(
        self,
        name: str,
        *,
        failed: bool = False,
        error: str | None = None,
        provenance: dict | None = None,
        attempt: int | None = None,
    ) -> None:
        """`attempt`: the one this pass works for; the catalog refuses the report (409) once it is superseded."""
        payload: dict[str, Any] = {"status": "failed" if failed else "done", "provenance": provenance or {}}
        if error:
            payload["error"] = error[:ERROR_LIMIT]
        self._post(f"/jobs/{name}", payload, None if attempt is None else {"attempt": attempt})
