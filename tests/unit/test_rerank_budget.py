"""Re-ranking must never cost more than it is worth.

Found in live testing: the reranker inherited a 20s HTTP timeout, so a
throttled free-tier key turned a 4s turn into a 24s one — and it fell back
anyway, so the student waited twenty seconds for a result that was already
available. Precision is worth about a second; it is never worth twenty.
"""

import asyncio

import pytest

from app.ai.rag import retriever as R


@pytest.fixture(autouse=True)
def reset_circuit(monkeypatch):
    monkeypatch.setattr(R, "_RERANK_FAILS", 0)
    monkeypatch.setattr(R, "_RERANK_OPEN_UNTIL", 0.0)
    yield
    R._RERANK_FAILS = 0
    R._RERANK_OPEN_UNTIL = 0.0


def chunks(n=5):
    return [R.RetrievedChunk(id=f"c{i}", text=f"passage {i}", score=1 - i / 10,
                             dense_score=1 - i / 10) for i in range(n)]


class TestBudget:
    @pytest.mark.asyncio
    async def test_a_slow_reranker_is_abandoned(self, monkeypatch):
        monkeypatch.setenv("RAG_RERANK", "cohere")
        monkeypatch.setattr(R, "_RERANK_BUDGET", 0.05)

        async def crawl(*_a):
            await asyncio.sleep(5)
            return []

        monkeypatch.setattr(R, "_rerank_cohere", crawl)
        pool = chunks()
        out = await R._maybe_rerank("q", pool, 3)
        assert [c.id for c in out] == ["c0", "c1", "c2"]

    @pytest.mark.asyncio
    async def test_the_wait_is_bounded_by_the_budget(self, monkeypatch):
        monkeypatch.setenv("RAG_RERANK", "cohere")
        monkeypatch.setattr(R, "_RERANK_BUDGET", 0.05)

        async def crawl(*_a):
            await asyncio.sleep(5)

        monkeypatch.setattr(R, "_rerank_cohere", crawl)
        import time as _t
        start = _t.monotonic()
        await R._maybe_rerank("q", chunks(), 3)
        assert _t.monotonic() - start < 1.0

    @pytest.mark.asyncio
    async def test_a_fast_reranker_is_used(self, monkeypatch):
        monkeypatch.setenv("RAG_RERANK", "cohere")
        pool = chunks()
        reordered = list(reversed(pool))

        async def quick(*_a):
            return reordered

        monkeypatch.setattr(R, "_rerank_cohere", quick)
        out = await R._maybe_rerank("q", pool, 3)
        assert [c.id for c in out] == ["c4", "c3", "c2"]

    @pytest.mark.asyncio
    async def test_an_error_falls_back_to_fused_order(self, monkeypatch):
        monkeypatch.setenv("RAG_RERANK", "cohere")

        async def boom(*_a):
            raise RuntimeError("429 rate limited")

        monkeypatch.setattr(R, "_rerank_cohere", boom)
        out = await R._maybe_rerank("q", chunks(), 3)
        assert len(out) == 3


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_it_trips_after_repeated_failures(self, monkeypatch):
        """During an outage, paying the budget every turn is pure loss — the
        fallback is already known to be acceptable."""
        monkeypatch.setenv("RAG_RERANK", "cohere")
        monkeypatch.setattr(R, "_RERANK_TRIP_AFTER", 3)
        calls = {"n": 0}

        async def boom(*_a):
            calls["n"] += 1
            raise RuntimeError("down")

        monkeypatch.setattr(R, "_rerank_cohere", boom)
        for _ in range(6):
            await R._maybe_rerank("q", chunks(), 3)
        assert calls["n"] == 3, "should stop calling once the circuit is open"

    @pytest.mark.asyncio
    async def test_an_open_circuit_returns_immediately(self, monkeypatch):
        import time as _t

        monkeypatch.setenv("RAG_RERANK", "cohere")
        monkeypatch.setattr(R, "_RERANK_OPEN_UNTIL", _t.monotonic() + 60)

        async def never(*_a):
            raise AssertionError("must not be called while open")

        monkeypatch.setattr(R, "_rerank_cohere", never)
        out = await R._maybe_rerank("q", chunks(), 3)
        assert len(out) == 3

    @pytest.mark.asyncio
    async def test_success_resets_the_failure_count(self, monkeypatch):
        monkeypatch.setenv("RAG_RERANK", "cohere")
        monkeypatch.setattr(R, "_RERANK_TRIP_AFTER", 3)

        async def boom(*_a):
            raise RuntimeError("blip")

        async def fine(*_a):
            return chunks()

        monkeypatch.setattr(R, "_rerank_cohere", boom)
        await R._maybe_rerank("q", chunks(), 3)
        await R._maybe_rerank("q", chunks(), 3)
        monkeypatch.setattr(R, "_rerank_cohere", fine)
        await R._maybe_rerank("q", chunks(), 3)
        assert R._RERANK_FAILS == 0, "an intermittent blip must not trip the circuit"


class TestConnectionReuse:
    def test_the_cohere_client_is_a_module_singleton(self):
        """A per-call client meant a TLS handshake on every turn."""
        assert R._get_cohere_client() is R._get_cohere_client()

    def test_it_is_the_sync_client(self):
        """Celery runs asyncio.run() per task, so every turn gets a new event
        loop. An AsyncClient binds its pool to the creating loop and could not
        be reused across turns."""
        import httpx

        assert isinstance(R._get_cohere_client(), httpx.Client)

    def test_the_http_timeout_does_not_outlive_the_budget(self):
        client = R._get_cohere_client()
        assert client.timeout.read <= R._RERANK_BUDGET + 0.01
