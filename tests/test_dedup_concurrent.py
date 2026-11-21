"""R04: saving the same text in the same turn twice at once stores it once (no model)."""
import threading

from n3memorycore import n3memory as n3m
from n3memorycore.core import database as db


def _save(cfg, content, turn_id, results, barrier):
    conn = db.get_connection()
    try:
        barrier.wait()
        results.append(n3m._store_record(conn, cfg, content, "claude-code", None, "s", turn_id, embed=False))
    finally:
        conn.close()


class TestDedupConcurrent:
    def test_concurrent_same_turn_saved_once(self, conn, cfg):
        results, barrier = [], threading.Barrier(2)
        ts = [threading.Thread(target=_save, args=(cfg, "[user] same", "T", results, barrier)) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert db.count_memories(conn) == 1
        assert sorted(r["status"] for r in results) == ["ok", "skipped"]
        assert [r.get("reason") for r in results if r["status"] == "skipped"] == ["exact_duplicate"]

    def test_same_text_other_turn_still_saved(self, conn, cfg):
        n3m._store_record(conn, cfg, "[user] same", turn_id="A", embed=False)
        n3m._store_record(conn, cfg, "[user] same", turn_id="B", embed=False)
        assert db.count_memories(conn) == 2

    def test_insert_memory_once_returns_none_for_a_duplicate(self, conn, cfg):
        a = db.insert_memory_once(conn, "i1", "txt", "2026-01-01T00:00:00+00:00", cfg["owner_id"], turn_id="T")
        b = db.insert_memory_once(conn, "i2", "txt", "2026-01-01T00:00:00+00:00", cfg["owner_id"], turn_id="T")
        assert a is not None and b is None
        assert not conn.in_transaction
        assert db.count_memories(conn) == 1
