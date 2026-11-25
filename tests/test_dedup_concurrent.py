"""Layer 1: the same text of the same turn saved twice at the same time. No model needed."""
import threading

from n3memorycore import n3memory
from n3memorycore.core.database import count_memories, insert_memory_once, get_connection, init_db
from tests.conftest import fake_embed


class TestDedupConcurrent:
    def test_concurrent_same_turn_saved_once(self, monkeypatch, cfg, conn):
        barrier = threading.Barrier(2, timeout=10)

        def slow_embed(content, name=None):
            barrier.wait()  # both requests have passed the first duplicate check
            return fake_embed(content)
        monkeypatch.setattr(n3memory, "embed_passage", slow_embed)
        results = []

        def call():
            req = n3memory.BufferRequest(content="[user] the same question", turn_id="turn-1")
            results.append(n3memory.api_buffer(req))
        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = sorted(r["status"] for r in results)
        assert statuses == ["ok", "skipped"]
        assert [r.get("reason") for r in results if r["status"] == "skipped"] == ["exact_duplicate"]
        c = get_connection(n3memory.DB_PATH)
        assert count_memories(c) == 1
        c.close()

    def test_same_text_other_turn_still_saved(self, monkeypatch, cfg):
        monkeypatch.setattr(n3memory, "embed_passage", lambda content, name=None: fake_embed(content))
        a = n3memory.api_buffer(n3memory.BufferRequest(content="[user] yes", turn_id="t1"))
        b = n3memory.api_buffer(n3memory.BufferRequest(content="[user] yes", turn_id="t2"))
        assert a["status"] == b["status"] == "ok"
        c = get_connection(n3memory.DB_PATH)
        assert count_memories(c) == 2
        c.close()

    def test_insert_memory_once_returns_none_for_a_duplicate(self, conn):
        first = insert_memory_once(conn, "id1", "text", "2026-01-01T00:00:00+00:00", "o", turn_id="t")
        second = insert_memory_once(conn, "id2", "text", "2026-01-01T00:00:00+00:00", "o", turn_id="t")
        assert isinstance(first, int)
        assert second is None
        assert not conn.in_transaction
        assert count_memories(conn) == 1
