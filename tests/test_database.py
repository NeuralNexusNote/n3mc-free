"""Layer 1: DB unit tests — schema, CRUD, FTS, vector, dedup, GC, repair."""
from __future__ import annotations

from core import database as db


# ---------------------------------------------------------------------------
class TestSchema:
    def test_init_db_creates_tables(self, isolated_db):
        conn, _ = isolated_db
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual')"
        )}
        # FTS virtual tables show up as 'table' too
        all_names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('memories','memories_fts','memories_vec')"
        )}
        assert {"memories", "memories_fts", "memories_vec"}.issubset(all_names)

    def test_migrate_schema_idempotent(self, isolated_db):
        conn, _ = isolated_db
        before = list(conn.execute("PRAGMA table_info(memories)"))
        db.migrate_schema(conn)
        db.migrate_schema(conn)
        after = list(conn.execute("PRAGMA table_info(memories)"))
        assert len(before) == len(after)

    def test_migrate_adds_missing_columns(self, tmp_path):
        # Open a raw sqlite, create memories without the new columns, then migrate.
        import sqlite3
        path = tmp_path / "legacy.db"
        c = sqlite3.connect(str(path))
        c.execute(
            "CREATE TABLE memories(id TEXT PRIMARY KEY, content TEXT NOT NULL, "
            "timestamp TEXT NOT NULL, owner_id TEXT NOT NULL)"
        )
        c.commit()
        c.close()
        conn = db.init_db(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        assert {"local_id", "agent_name", "turn_id"}.issubset(cols)
        conn.close()


class TestInsertAndRetrieve:
    def test_insert_and_count(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        assert db.count_memories(conn) == 0
        db.insert_memory(conn, "hello", dummy_vec(), "owner-1", "local-1")
        assert db.count_memories(conn) == 1

    def test_insert_populates_all_three_tables(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        mem_id, rowid = db.insert_memory(
            conn, "foo bar baz", dummy_vec(), "o", "l", agent_name="claude-code"
        )
        m = conn.execute("SELECT COUNT(*) FROM memories WHERE id=?", (mem_id,)).fetchone()[0]
        f = conn.execute("SELECT COUNT(*) FROM memories_fts WHERE rowid=?", (rowid,)).fetchone()[0]
        v = conn.execute("SELECT COUNT(*) FROM memories_vec WHERE rowid=?", (rowid,)).fetchone()[0]
        assert m == 1 and f == 1 and v == 1

    def test_insert_without_embedding(self, isolated_db):
        conn, _ = isolated_db
        mem_id, rowid = db.insert_memory(conn, "no-vec", None, "o", "l")
        v = conn.execute("SELECT COUNT(*) FROM memories_vec WHERE rowid=?", (rowid,)).fetchone()[0]
        assert v == 0  # vec missing on purpose

    def test_get_memory_by_rowid(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        _, rowid = db.insert_memory(conn, "x", dummy_vec(), "o", "l")
        row = db.get_memory_by_rowid(conn, rowid)
        assert row is not None and row["content"] == "x"

    def test_get_all_memories(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        for i in range(3):
            db.insert_memory(conn, f"row{i}", dummy_vec(), "o", "l")
        rows = db.get_all_memories(conn)
        assert len(rows) == 3


class TestDelete:
    def test_delete_removes_from_all_three_tables(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        mid, rowid = db.insert_memory(conn, "doomed", dummy_vec(), "o", "l")
        assert db.delete_memory(conn, mid) is True
        for tbl in ("memories", "memories_fts", "memories_vec"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE rowid=?", (rowid,)).fetchone()[0]
            assert n == 0

    def test_delete_nonexistent(self, isolated_db):
        conn, _ = isolated_db
        assert db.delete_memory(conn, "no-such-id") is False


class TestDedup:
    def test_check_exact_duplicate_true(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        db.insert_memory(conn, "same", dummy_vec(), "o", "l")
        assert db.check_exact_duplicate(conn, "same") is True

    def test_check_exact_duplicate_false(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        db.insert_memory(conn, "alpha", dummy_vec(), "o", "l")
        assert db.check_exact_duplicate(conn, "beta") is False


class TestUnindexed:
    def test_find_unindexed_vec_missing(self, isolated_db):
        conn, _ = isolated_db
        db.insert_memory(conn, "no-vec", None, "o", "l")  # vec missing on purpose
        rows = db.find_unindexed_memories(conn)
        assert len(rows) == 1
        assert rows[0]["vec_missing"] == 1
        assert rows[0]["fts_missing"] == 0

    def test_find_unindexed_all_indexed(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        db.insert_memory(conn, "ok", dummy_vec(), "o", "l")
        assert db.find_unindexed_memories(conn) == []


class TestFTS:
    def test_strip_fts_punctuation(self):
        assert db.strip_fts_punctuation("Hello, world!") == "Hello world"
        assert db.strip_fts_punctuation("[Alpha-9]") == "Alpha9"

    def test_quote_fts_query(self):
        q = db._quote_fts_query("Hello, world!")
        assert q == '"Hello" "world"'

    def test_quote_fts_query_max_terms(self):
        text = " ".join(f"w{i}" for i in range(50))
        q = db._quote_fts_query(text)
        # 30-term cap (spec §3 _FTS_MAX_TERMS)
        assert q.count('"') == 60

    def test_search_fts_basic(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        db.insert_memory(conn, "Abraham Lincoln was the 16th president", dummy_vec(), "o", "l")
        hits = db.search_fts(conn, "Lincoln")
        assert len(hits) >= 1

    def test_search_fts_short_query_skipped(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        db.insert_memory(conn, "I am here", dummy_vec(), "o", "l")
        # 1-char query is skipped per spec §3 FTS5 Constraint
        assert db.search_fts(conn, "I") == []

    def test_search_fts_punctuation_resilience(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        db.insert_memory(conn, "Planet [Alpha-9] temperature settings", dummy_vec(), "o", "l")
        hits = db.search_fts(conn, "Alpha9 temperature")
        assert len(hits) >= 1


class TestVectorSearch:
    def test_search_vector_returns_results(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        db.insert_memory(conn, "x", dummy_vec(0.1), "o", "l")
        db.insert_memory(conn, "y", dummy_vec(0.2), "o", "l")
        hits = db.search_vector(conn, dummy_vec(0.1), k=2)
        assert len(hits) == 2
        assert hits[0]["distance"] <= hits[1]["distance"]

    def test_search_vector_empty_db(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        assert db.search_vector(conn, dummy_vec(), k=5) == []


class TestSerialization:
    def test_serialize_vector_roundtrip(self):
        vec = [i * 0.001 for i in range(768)]
        blob = db.serialize_vector(vec)
        restored = db.deserialize_vector(blob)
        for a, b in zip(vec, restored):
            assert abs(a - b) < 1e-5


class TestGC:
    def test_gc_deletes_expired(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        # Insert with old timestamp
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")
        db.insert_memory(conn, "ancient", dummy_vec(), "o", "l", timestamp=old_ts)
        db.insert_memory(conn, "fresh", dummy_vec(), "o", "l")
        deleted = db.gc_expired(conn, retain_days=365)
        assert deleted == 1
        assert db.count_memories(conn) == 1

    def test_gc_keeps_recent(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        db.insert_memory(conn, "fresh", dummy_vec(), "o", "l")
        deleted = db.gc_expired(conn, retain_days=365)
        assert deleted == 0
