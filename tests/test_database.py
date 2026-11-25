"""Layer 1: the DB layer (schema, CRUD, transactions, FTS, KNN). No model needed."""
import sqlite3

import pytest

from n3memorycore.core import database
from n3memorycore.core.database import (
    get_connection, init_db, migrate_schema, insert_memory, delete_memory, count_memories,
    check_exact_duplicate, find_unindexed_memories, search_fts, search_vector, serialize_vector,
    deserialize_vector, strip_fts_punctuation, _build_fts_query, get_all_memories,
)
from tests.conftest import add, basis, fake_embed, ts_days_ago


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall()}


def _cols(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]


class TestSchema:
    def test_tables_created(self, conn):
        assert {"memories", "memories_fts", "memories_fts_bi", "memories_vec", "meta"} <= _tables(conn)

    def test_migrate_idempotent(self, conn):
        before = _cols(conn)
        init_db(conn)
        migrate_schema(conn)
        migrate_schema(conn)
        assert _cols(conn) == before

    def test_add_missing_columns(self, isolate):
        path = str(isolate["mem"] / "old.db")
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, timestamp TEXT NOT NULL, "
                    "owner_id TEXT NOT NULL)")
        raw.execute("INSERT INTO memories VALUES ('a', 'old record', '2025-01-01T00:00:00+00:00', 'o')")
        raw.commit()
        raw.close()
        c = get_connection(path)
        init_db(c)
        cols = _cols(c)
        for col in ("local_id", "agent_name", "session_id", "turn_id"):
            assert col in cols
        idx = c.execute("SELECT name FROM sqlite_master WHERE name = 'idx_memories_turn_id'").fetchone()
        assert idx is not None
        assert count_memories(c) == 1
        c.close()

    def test_fts_tokenizer_is_trigram(self, conn):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'memories_fts'").fetchone()[0]
        assert "trigram" in sql
        assert "content=" not in sql.replace(" ", "")

    def test_migrate_tokenizer_to_trigram(self, isolate):
        path = str(isolate["mem"] / "porter.db")
        c = get_connection(path)
        c.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, timestamp TEXT NOT NULL, "
                  "owner_id TEXT NOT NULL)")
        c.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, tokenize='porter unicode61')")
        c.execute("INSERT INTO memories VALUES ('a', '架空の惑星アルファ9の気温設定', '2025-01-01T00:00:00+00:00', 'o')")
        c.execute("INSERT INTO memories_fts(rowid, content) VALUES (1, '架空の惑星アルファ9の気温設定')")
        c.commit()
        init_db(c)
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'memories_fts'").fetchone()[0]
        assert "trigram" in sql
        assert search_fts(c, "アルファ9")
        c.close()


class TestInsertAndRetrieve:
    def test_insert_count(self, conn):
        add(conn, "one", fake_embed("one"))
        add(conn, "two", fake_embed("two"))
        assert count_memories(conn) == 2

    def test_three_table_consistency(self, conn):
        rowid = add(conn, "consistent record", fake_embed("x"))
        for table in ("memories_fts", "memories_fts_bi", "memories_vec"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE rowid = ?", (rowid,)).fetchone()[0] == 1

    def test_insert_without_embedding(self, conn):
        rowid = add(conn, "no vector")
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memories_fts WHERE rowid = ?", (rowid,)).fetchone()[0] == 1

    def test_rowid_returned(self, conn):
        r1 = add(conn, "first")
        r2 = add(conn, "second")
        assert isinstance(r1, int) and r2 == r1 + 1


class TestDelete:
    def test_delete_three_tables(self, conn):
        add(conn, "to delete", fake_embed("d"))
        mid = conn.execute("SELECT id FROM memories").fetchone()[0]
        assert delete_memory(conn, mid) is True
        for table in ("memories", "memories_fts", "memories_fts_bi", "memories_vec"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    def test_delete_nonexistent(self, conn):
        assert delete_memory(conn, "nope") is False


class TestDedup:
    def test_exact_duplicate_detected(self, conn):
        add(conn, "same text", turn_id="t1")
        assert check_exact_duplicate(conn, "same text", turn_id="t1")
        assert check_exact_duplicate(conn, "same text")
        assert not check_exact_duplicate(conn, "same text", turn_id="t2")

    def test_no_false_positive(self, conn):
        add(conn, "same text")
        assert not check_exact_duplicate(conn, "same text!")
        assert not check_exact_duplicate(conn, "Same text")


class TestUnindexed:
    def test_vec_missing_detected(self, conn):
        add(conn, "indexed", fake_embed("i"))
        rowid = add(conn, "missing vector")
        rows = find_unindexed_memories(conn)
        assert [r["rowid"] for r in rows] == [rowid]
        assert rows[0]["has_fts"] and not rows[0]["has_vec"]

    def test_all_indexed(self, conn):
        add(conn, "a", fake_embed("a"))
        assert find_unindexed_memories(conn) == []


class TestFTS:
    def test_punctuation_removal(self):
        assert strip_fts_punctuation("架空の惑星「アルファ9」の気温設定。") == "架空の惑星アルファ9の気温設定"

    def test_short_query_skipped(self, conn):
        add(conn, "AI と東京の話")
        assert _build_fts_query("AI 東京") == ""
        assert search_fts(conn, "AI 東京") == []

    def test_basic_search(self, conn):
        add(conn, "The quick brown fox jumps")
        add(conn, "Nothing related here")
        hits = search_fts(conn, "brown")
        assert len(hits) == 1 and "brown" in hits[0]["content"]

    def test_punctuation_tolerance(self, conn):
        add(conn, "架空の惑星「アルファ9」の気温設定")
        assert search_fts(conn, "アルファ9の気温")

    def test_max_terms_limit(self):
        q = " ".join(f"word{i:03d}" for i in range(100))
        assert _build_fts_query(q).count(" OR ") == 39


class TestVectorSearch:
    def test_vector_search_results(self, conn):
        add(conn, "near", basis(0))
        add(conn, "far", basis(1))
        hits = search_vector(conn, basis(0), 2)
        assert hits[0]["content"] == "near"
        assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-5)

    def test_empty_db_vector_search(self, conn):
        assert search_vector(conn, basis(0), 5) == []


class TestRetentionAndRange:
    def test_old_records_retained(self, conn):
        add(conn, "ten years old", ts=ts_days_ago(3650))
        init_db(conn)
        assert count_memories(conn) == 1

    def test_no_gc_decrements_count(self, conn):
        for i in range(3):
            add(conn, f"record {i}", ts=ts_days_ago(1000 * i))
        assert not hasattr(database, "gc") and not hasattr(database, "garbage_collect")
        migrate_schema(conn)
        assert count_memories(conn) == 3

    def test_since_filter_excludes_old(self, conn):
        add(conn, "old", ts=ts_days_ago(100))
        add(conn, "new", ts=ts_days_ago(1))
        rows = get_all_memories(conn, since=ts_days_ago(10))
        assert [r["content"] for r in rows] == ["new"]

    def test_until_filter_excludes_new(self, conn):
        add(conn, "old", ts=ts_days_ago(100))
        add(conn, "new", ts=ts_days_ago(1))
        rows = get_all_memories(conn, until=ts_days_ago(10))
        assert [r["content"] for r in rows] == ["old"]


class TestSerialization:
    def test_roundtrip(self):
        v = [0.5, -0.25, 1.0, 0.0]
        assert deserialize_vector(serialize_vector(v)) == v
        assert len(serialize_vector(v)) == 16
