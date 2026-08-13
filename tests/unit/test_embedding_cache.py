"""Query-embedding cache.

Replaced a Supabase-backed cache that was measured slower than the OpenAI call
it existed to avoid (687ms read, 891ms blocking write, vs ~500ms to just ask
OpenAI). The tests pin the properties that made it worth replacing.
"""

import asyncio

import pytest

from app.ai.rag import embedding_cache as EC


@pytest.fixture(autouse=True)
def clean():
    EC._lru.clear()
    yield
    EC._lru.clear()


class FakeRedis:
    def __init__(self, fail=False):
        self.store = {}
        self.fail = fail
        self.gets = 0
        self.sets = 0

    async def get(self, key):
        self.gets += 1
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.sets += 1
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value


@pytest.fixture
def redis(monkeypatch):
    def _apply(fail=False):
        fake = FakeRedis(fail)
        monkeypatch.setattr(EC, "_get_redis", lambda: fake)
        return fake

    return _apply


EMB = [0.1, 0.2, 0.3]


class TestTiers:
    @pytest.mark.asyncio
    async def test_a_miss_returns_none(self, redis):
        redis()
        assert await EC.get("never seen") is None

    @pytest.mark.asyncio
    async def test_round_trip(self, redis):
        redis()
        await EC.put("q", EMB)
        assert await EC.get("q") == EMB

    @pytest.mark.asyncio
    async def test_the_lru_answers_without_touching_redis(self, redis):
        """The point of the in-process tier: a repeat in the same worker costs
        nothing at all."""
        fake = redis()
        await EC.put("q", EMB)
        fake.gets = 0
        assert await EC.get("q") == EMB
        assert fake.gets == 0

    @pytest.mark.asyncio
    async def test_redis_serves_a_different_worker(self, redis):
        """Simulates a second worker: same Redis, cold LRU."""
        fake = redis()
        await EC.put("q", EMB)
        EC._lru.clear()
        assert await EC.get("q") == EMB
        assert fake.gets == 1


class TestFailuresAreMisses:
    @pytest.mark.asyncio
    async def test_a_read_failure_is_a_miss_not_an_error(self, redis):
        """A cache problem must degrade to an OpenAI call, never fail the turn."""
        redis(fail=True)
        assert await EC.get("q") is None

    @pytest.mark.asyncio
    async def test_a_write_failure_is_swallowed(self, redis):
        redis(fail=True)
        await EC.put("q", EMB)  # must not raise

    @pytest.mark.asyncio
    async def test_no_redis_at_all_still_works(self, monkeypatch):
        monkeypatch.setattr(EC, "_get_redis", lambda: None)
        assert await EC.get("q") is None
        await EC.put("q", EMB)
        assert await EC.get("q") == EMB  # the LRU still covers it

    @pytest.mark.asyncio
    async def test_corrupt_redis_value_is_a_miss(self, redis):
        fake = redis()
        fake.store[EC._key("q")] = "not json"
        assert await EC.get("q") is None


class TestBackgroundWrite:
    @pytest.mark.asyncio
    async def test_the_lru_is_populated_synchronously(self, redis):
        """An immediate repeat must hit even though the Redis write is
        deferred — otherwise deferring it would cost an OpenAI call."""
        redis()
        EC.put_background("q", EMB)
        assert await EC.get("q") == EMB

    @pytest.mark.asyncio
    async def test_redis_write_lands_eventually(self, redis):
        fake = redis()
        EC.put_background("q", EMB)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        EC._lru.clear()
        assert await EC.get("q") == EMB
        assert fake.sets == 1

    def test_outside_an_event_loop_it_still_caches_in_process(self, monkeypatch):
        monkeypatch.setattr(EC, "_get_redis", lambda: None)
        EC.put_background("q", EMB)  # no running loop — must not raise
        assert EC._lru_get(EC._key("q")) == EMB


class TestBounded:
    def test_the_lru_evicts_oldest_first(self, monkeypatch):
        monkeypatch.setattr(EC, "_LRU_MAX", 3)
        for i in range(5):
            EC._lru_put(f"k{i}", [float(i)])
        assert len(EC._lru) == 3
        assert EC._lru_get("k0") is None
        assert EC._lru_get("k4") == [4.0]

    def test_reading_an_entry_keeps_it_alive(self, monkeypatch):
        monkeypatch.setattr(EC, "_LRU_MAX", 3)
        for i in range(3):
            EC._lru_put(f"k{i}", [float(i)])
        EC._lru_get("k0")          # touch the oldest
        EC._lru_put("k3", [3.0])   # forces one eviction
        assert EC._lru_get("k0") == [0.0]
        assert EC._lru_get("k1") is None

    def test_keys_are_hashed_not_raw_questions(self):
        """Student questions are personal data; they should not be Redis keys."""
        key = EC._key("what is a limit?")
        assert "what is a limit" not in key
        assert key.startswith(EC._KEY_PREFIX)
        assert len(key) == len(EC._KEY_PREFIX) + 64
