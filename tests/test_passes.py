"""Counting deliveries of one run's `extract` (#3)."""

from evilflowers_analyzer import passes


class FakeRedis:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.values: dict[str, int] = {}
        self.ttl: dict[str, int] = {}

    def set(self, key, value, ex):
        if self.fail:
            raise ConnectionError("redis down")
        self.values[key], self.ttl[key] = int(value), ex

    def incr(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key, seconds):
        self.ttl[key] = seconds


def test_a_first_delivery_starts_over_and_redeliveries_add_one():
    client = FakeRedis()
    assert passes.count(client, "run-1", redelivered=False) == 1
    assert passes.count(client, "run-1", redelivered=True) == 2
    assert passes.count(client, "run-1", redelivered=True) == 3
    # A new message for the run (backfill, retry) is a first delivery again.
    assert passes.count(client, "run-1", redelivered=False) == 1
    assert client.ttl["evilflowers_analyzer:passes:run-1"] == passes.TTL_SECONDS


def test_runs_are_counted_apart():
    client = FakeRedis()
    passes.count(client, "run-1", redelivered=False)
    assert passes.count(client, "run-2", redelivered=True) == 1


def test_an_unreachable_redis_never_stops_a_book():
    assert passes.count(FakeRedis(fail=True), "run-1", redelivered=True) == 1
    # No client at all (a REDIS_URL the client cannot parse).
    assert passes.count(None, "run-1", redelivered=True) == 1
