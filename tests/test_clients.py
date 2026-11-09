import pytest
import requests
from pydantic import SecretStr

from evilflowers_analyzer.clients import catalog as catalog_module
from evilflowers_analyzer.clients.catalog import CatalogClient, RunNotRunning
from evilflowers_analyzer.clients.http import USER_AGENT, session


class Response:
    def __init__(self, status_code: int, body: bytes = b"{}"):
        self.status_code = status_code
        self.content = body
        self.text = body.decode()

    def json(self):
        return {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeHttp:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json, timeout):
        self.calls.append((url, json))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(catalog_module.time, "sleep", lambda seconds: None)


def client(settings, *answers) -> tuple[CatalogClient, FakeHttp]:
    http = FakeHttp(*answers)
    return CatalogClient("run-1", settings=settings, http=http), http


def test_start_posts_to_the_run(settings):
    catalog, http = client(settings, Response(200))
    catalog.start(1, [[595.0, 842.0]], iter(["paragraphs"]), {"analyzer": {}})
    url, payload = http.calls[0]
    assert url == "http://catalog:8000/extraction/v1/runs/run-1/start"
    assert payload == {
        "page_count": 1,
        "page_sizes": [[595.0, 842.0]],
        "jobs": ["paragraphs"],
        "provenance": {"analyzer": {}},
    }


def test_a_failed_job_carries_a_truncated_error(settings):
    catalog, http = client(settings, Response(200, b""))
    catalog.job("analyzer", failed=True, error="x" * 5000)
    url, payload = http.calls[0]
    assert url.endswith("/jobs/analyzer")
    assert payload["status"] == "failed" and len(payload["error"]) == catalog_module.ERROR_LIMIT


def test_5xx_and_connection_errors_are_retried(settings):
    catalog, http = client(settings, Response(503), Response(200))
    catalog.job("analyzer", failed=True)
    assert len(http.calls) == 2

    catalog, http = client(settings, requests.ConnectionError("down"), Response(200))
    catalog.job("analyzer", failed=True)
    assert len(http.calls) == 2


def test_the_last_attempt_raises(settings):
    catalog, _http = client(settings, Response(503), Response(503))
    with pytest.raises(requests.HTTPError):
        catalog.job("analyzer", failed=True)

    catalog, _http = client(settings, requests.ConnectionError("down"), requests.ConnectionError("down"))
    with pytest.raises(requests.ConnectionError):
        catalog.job("analyzer", failed=True)


def test_409_means_the_run_is_no_longer_running(settings):
    catalog, http = client(settings, Response(409, b"finished"))
    with pytest.raises(RunNotRunning):
        catalog.start(1, [[1, 1]], [], {})
    assert len(http.calls) == 1


def test_4xx_is_not_retried(settings):
    catalog, http = client(settings, Response(400))
    with pytest.raises(requests.HTTPError):
        catalog.start(1, [[1, 1]], [], {})
    assert len(http.calls) == 1


def test_session_sends_the_key_only_when_set():
    assert session(SecretStr("token")).headers["Authorization"] == "Bearer token"
    assert "Authorization" not in session(SecretStr("")).headers
    assert "Authorization" not in session(None).headers
    assert session().headers["User-Agent"] == USER_AGENT
