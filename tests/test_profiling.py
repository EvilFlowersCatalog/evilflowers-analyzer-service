import pytest

from evilflowers_analyzer.observability import profiling


@pytest.fixture
def fresh(monkeypatch):
    """A profiler module that has not run `setup()` yet."""
    monkeypatch.setitem(profiling._state, "done", False)
    monkeypatch.setitem(profiling._state, "enabled", False)
    for name in ("PYROSCOPE_SERVER_ADDRESS", "PYROSCOPE_APPLICATION_NAME", "PYROSCOPE_TAGS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_parse_tags():
    assert profiling.parse_tags("namespace=evilflowers-dev, pod=analyzer-1 ,container=analyzer") == {
        "namespace": "evilflowers-dev",
        "pod": "analyzer-1",
        "container": "analyzer",
    }
    assert profiling.parse_tags(None) == {}
    assert profiling.parse_tags("broken, =x, y=, ok=1") == {"ok": "1"}


def test_off_without_a_server_address(fresh):
    assert profiling.setup() is False
    assert profiling.is_enabled() is False
    assert profiling.span_processor() is None
    profiling.shutdown()


def test_configures_pyroscope_from_the_environment(fresh):
    calls = []

    class FakePyroscope:
        @staticmethod
        def configure(**kwargs):
            calls.append(kwargs)

        @staticmethod
        def shutdown():
            calls.append("shutdown")

    fresh.setitem(__import__("sys").modules, "pyroscope", FakePyroscope)
    fresh.setenv("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
    fresh.setenv("PYROSCOPE_TAGS", "namespace=evilflowers-dev")

    assert profiling.setup() is True
    assert profiling.setup() is True  # idempotent
    assert len(calls) == 1
    assert calls[0]["application_name"] == "analyzer"
    assert calls[0]["gil_only"] is False
    assert calls[0]["tags"] == {"namespace": "evilflowers-dev"}

    profiling.shutdown()
    assert calls[-1] == "shutdown" and profiling.is_enabled() is False
