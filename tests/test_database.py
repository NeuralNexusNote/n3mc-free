"""Layer 1: DB layer (no model)."""
import sqlite3
from datetime import datetime, timedelta, timezone

from n3memorycore.core import database as db

from .conftest import add, fake_embed


class TestSchema:
    def test_tables_created(self, conn):
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"memories", "memories_fts", "memories_fts_bi", "memories_vec", "meta"} <= names

    def test_migrate_idempotent(self, conn):
        db.migrate_schema(conn)
        db.migrate_schema(conn)

    def test_add_missing_columns(self, tmp_path):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, timestamp TEXT, owner_id TEXT)")
        db.migrate_schema(c)
        cols = {r[1] for r in c.execute("PRAGMA table_info(memories)")}
        assert {"local_id", "agent_name", "session_id", "turn_id"} <= cols

    def test_fts_tokenizer_is_trigram(self, conn):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='memories_fts'").fetchone()[0]
        assert "trigram" in sql

    def test_migrate_tokenizer_to_trigram(self, conn, cfg):
        add(conn, cfg, "alphabet soup recipe", embed=False)
        conn.execute("DROP TABLE memories_fts")
        conn.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, tokenize='porter unicode61')")
        db.migrate_schema(conn)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='memories_fts'").fetchone()[0]
        assert "trigram" in sql
        assert db.search_fts(conn, "alphabet")


class TestInsertAndRetrieve:
    def test_three_table_consistency(self, conn, cfg):
        add(conn, cfg, "one record")
        for t in ("memories", "memories_fts", "memories_fts_bi", "memories_vec"):
            assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 1

    def test_insert_without_embedding(self, conn, cfg):
        add(conn, cfg, "no vector", embed=False)
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 0
        assert len(db.find_unindexed_memories(conn)) == 1

    def test_rowid_returned(self, conn, cfg):
        rid = db.insert_memory(conn, "id1", "x", "2026-01-01T00:00:00+00:00", cfg["owner_id"])
        assert isinstance(rid, int)


class TestDelete:
    def test_delete_all_tables(self, conn, cfg):
        rid = add(conn, cfg, "to delete")
        assert db.delete_memory(conn, rid)
        for t in ("memories", "memories_fts", "memories_fts_bi", "memories_vec"):
            assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0

    def test_delete_nonexistent(self, conn):
        assert db.delete_memory(conn, "nope") is False


class TestDedup:
    def test_exact_duplicate_detected(self, conn, cfg):
        add(conn, cfg, "same text", turn_id="t1")
        assert db.check_exact_duplicate(conn, "same text", turn_id="t1")
        assert db.check_exact_duplicate(conn, "same text")

    def test_no_false_positive(self, conn, cfg):
        add(conn, cfg, "same text", turn_id="t1")
        assert not db.check_exact_duplicate(conn, "same text.", turn_id="t1")
        assert not db.check_exact_duplicate(conn, "same text", turn_id="t2")


class TestUnindexed:
    def test_vec_missing_detected(self, conn, cfg):
        add(conn, cfg, "a", embed=False)
        assert len(db.find_unindexed_memories(conn)) == 1

    def test_all_indexed(self, conn, cfg):
        add(conn, cfg, "a")
        assert db.find_unindexed_memories(conn) == []

    def test_same_rows_as_double_left_join(self, conn, cfg):
        """Rows missing a vector, an FTS row, or both are found, in rowid order and up to `limit`,
        exactly as the double LEFT JOIN finds them (2026-11-22: the query no longer joins vec0 per row)."""
        rids = [add(conn, cfg, f"record {i}", embed=(i % 3 != 0)) for i in range(12)]
        rowid = {r: conn.execute("SELECT rowid FROM memories WHERE id=?", (r,)).fetchone()[0] for r in rids}
        for r in rids[1::4]:
            conn.execute("DELETE FROM memories_fts WHERE rowid=?", (rowid[r],))
        conn.commit()
        want = [dict(r) for r in conn.execute(
            "SELECT m.rowid AS rowid, m.id AS id, m.content AS content, "
            "(mv.rowid IS NULL) AS no_vec, (mf.rowid IS NULL) AS no_fts FROM memories m "
            "LEFT JOIN memories_vec mv ON mv.rowid = m.rowid LEFT JOIN memories_fts mf ON mf.rowid = m.rowid "
            "WHERE mv.rowid IS NULL OR mf.rowid IS NULL ORDER BY m.rowid")]
        assert {w["no_vec"] for w in want} == {0, 1} and {w["no_fts"] for w in want} == {0, 1}
        assert db.find_unindexed_memories(conn) == want
        assert db.find_unindexed_memories(conn, 3) == want[:3]


class TestFTS:
    def test_punctuation_removal(self):
        assert db.strip_fts_punctuation("架空の惑星「アルファ9」の気温設定") == "架空の惑星アルファ9の気温設定"

    def test_short_query_skipped(self):
        assert db._build_fts_query("AI 東京") == ""

    def test_basic_search(self, conn, cfg):
        add(conn, cfg, "the quick brown fox jumps")
        assert db.search_fts(conn, "quick")

    def test_punctuation_tolerance(self, conn, cfg):
        add(conn, cfg, "架空の惑星「アルファ9」の気温設定")
        assert db.search_fts(conn, "アルファ9の気温")

    def test_max_terms_limit(self):
        q = db._build_fts_query(" ".join(f"word{i:03d}" for i in range(100)))
        assert q.count('"') == 2 * db._FTS_MAX_TERMS


class TestVectorSearch:
    def test_vector_search_results(self, conn, cfg):
        add(conn, cfg, "apples and oranges")
        add(conn, cfg, "completely different subject matter here")
        hits = db.search_vector(conn, fake_embed("apples and oranges"), k=5)
        assert hits and hits[0][0]["content"] == "apples and oranges" and hits[0][1] < 1e-3

    def test_empty_db_vector_search(self, conn):
        assert db.search_vector(conn, fake_embed("x"), k=5) == []


class TestRetentionAndRange:
    def test_old_records_retained(self, conn, cfg):
        add(conn, cfg, "ancient", ts="2000-01-01T00:00:00+00:00")
        assert db.count_memories(conn) == 1

    def test_no_gc_decrements_count(self, conn, cfg):
        add(conn, cfg, "a")
        add(conn, cfg, "b")
        assert db.count_memories(conn) == 2

    def test_since_filter_excludes_old(self, conn, cfg):
        add(conn, cfg, "old fox story", ts="2020-01-01T00:00:00+00:00")
        add(conn, cfg, "new fox story", ts="2026-01-01T00:00:00+00:00")
        hits = db.search_vector(conn, fake_embed("fox story"), k=5, since="2025-01-01T00:00:00+00:00")
        assert [h[0]["content"] for h in hits] == ["new fox story"]

    def test_until_filter_excludes_new(self, conn, cfg):
        add(conn, cfg, "old fox story", ts="2020-01-01T00:00:00+00:00")
        add(conn, cfg, "new fox story", ts="2026-01-01T00:00:00+00:00")
        hits = db.search_vector(conn, fake_embed("fox story"), k=5, until="2025-01-01T00:00:00+00:00")
        assert [h[0]["content"] for h in hits] == ["old fox story"]


class TestSerialization:
    def test_roundtrip(self):
        v = [0.5, -0.25, 1.0]
        assert db.deserialize_vector(db.serialize_vector(v)) == v
