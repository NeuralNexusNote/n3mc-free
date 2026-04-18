"""
Layer 1: DB unit tests (CRUD, schema, transactions)
"""
import os
import sys
import math
import pytest
from uuid_extensions import uuid7 as _gen_uuid7

_CORE_DIR = os.path.join(os.path.dirname(__file__), "..", "core")
sys.path.insert(0, _CORE_DIR)

from database import (
    get_connection, init_db, migrate_schema, insert_memory,
    delete_memory, count_memories, check_exact_duplicate,
    find_unindexed_memories, search_fts, search_vector,
    serialize_vector, deserialize_vector, strip_fts_punctuation,
    _quote_fts_query, get_all_memories, get_memory_by_rowid,
    get_memories_by_turn_id,
)


def make_vec(dim=768):
    v = [1.0 / math.sqrt(dim)] * dim
    return v


class TestSchema:
    def test_init_db_creates_tables(self, tmp_db):
        cur = tmp_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in cur.fetchall()}
        assert "memories" in names

    def test_migrate_schema_idempotent(self, tmp_db):
        migrate_schema(tmp_db)
        migrate_schema(tmp_db)

    def test_migrate_schema_adds_missing_columns(self, tmp_db):
        info = {r[1] for r in tmp_db.execute("PRAGMA table_info(memories)")}
        assert "local_id" in info
        assert "agent_name" in info


class TestInsertAndRetrieve:
    def test_insert_and_count(self, tmp_db):
        assert count_memories(tmp_db) == 0
        insert_memory(tmp_db, str(_gen_uuid7()), "hello world", "2025-01-01T00:00:00+00:00", "owner1", None)
        assert count_memories(tmp_db) == 1

    def test_insert_populates_all_three_tables(self, tmp_db):
        vec = make_vec()
        rowid = insert_memory(tmp_db, str(_gen_uuid7()), "test content", "2025-01-01T00:00:00+00:00", "owner1", vec)
        row = tmp_db.execute("SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone()
        assert row is not None
        row2 = tmp_db.execute("SELECT rowid FROM memories_fts WHERE rowid = ?", (rowid,)).fetchone()
        assert row2 is not None

    def test_insert_without_embedding(self, tmp_db):
        rowid = insert_memory(tmp_db, str(_gen_uuid7()), "no embedding", "2025-01-01T00:00:00+00:00", "owner1", None)
        assert rowid is not None
        row = tmp_db.execute("SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone()
        assert row is None

    def test_get_memory_by_rowid(self, tmp_db):
        rid = str(_gen_uuid7())
        rowid = insert_memory(tmp_db, rid, "find me", "2025-01-01T00:00:00+00:00", "owner1", None)
        row = get_memory_by_rowid(tmp_db, rowid)
        assert row is not None
        assert row["content"] == "find me"

    def test_get_all_memories(self, tmp_db):
        insert_memory(tmp_db, str(_gen_uuid7()), "a", "2025-01-01T00:00:00+00:00", "owner1", None)
        insert_memory(tmp_db, str(_gen_uuid7()), "b", "2025-01-02T00:00:00+00:00", "owner1", None)
        rows = get_all_memories(tmp_db)
        assert len(rows) == 2


class TestDelete:
    def test_delete_removes_from_all_three_tables(self, tmp_db):
        vec = make_vec()
        rid = str(_gen_uuid7())
        rowid = insert_memory(tmp_db, rid, "to delete", "2025-01-01T00:00:00+00:00", "owner1", vec)
        assert count_memories(tmp_db) == 1
        delete_memory(tmp_db, rid)
        assert count_memories(tmp_db) == 0
        assert tmp_db.execute("SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone() is None
        assert tmp_db.execute("SELECT rowid FROM memories_fts WHERE rowid = ?", (rowid,)).fetchone() is None

    def test_delete_nonexistent_no_error(self, tmp_db):
        result = delete_memory(tmp_db, "nonexistent-id")
        assert result is False


class TestDedup:
    def test_check_exact_duplicate_true(self, tmp_db):
        insert_memory(tmp_db, str(_gen_uuid7()), "duplicate text", "2025-01-01T00:00:00+00:00", "owner1", None)
        assert check_exact_duplicate(tmp_db, "duplicate text") is True

    def test_check_exact_duplicate_false(self, tmp_db):
        assert check_exact_duplicate(tmp_db, "unique text") is False


class TestUnindexed:
    def test_find_unindexed_vec_missing(self, tmp_db):
        insert_memory(tmp_db, str(_gen_uuid7()), "no vec", "2025-01-01T00:00:00+00:00", "owner1", None)
        rows = find_unindexed_memories(tmp_db)
        assert len(rows) >= 1
        assert any(r[4] == 0 for r in rows)

    def test_find_unindexed_all_indexed(self, tmp_db):
        vec = make_vec()
        insert_memory(tmp_db, str(_gen_uuid7()), "fully indexed", "2025-01-01T00:00:00+00:00", "owner1", vec)
        rows = find_unindexed_memories(tmp_db)
        assert len(rows) == 0


class TestFTS:
    def test_strip_fts_punctuation(self):
        assert strip_fts_punctuation("hello, world!") == "hello world"
        assert strip_fts_punctuation("(test) [bracket]") == "test bracket"

    def test_quote_fts_query(self):
        result = _quote_fts_query("hello world")
        assert '"hello"' in result
        assert '"world"' in result

    def test_quote_fts_query_max_terms(self):
        big_query = " ".join(f"word{i}" for i in range(35))
        result = _quote_fts_query(big_query)
        terms = result.split()
        assert len(terms) == 30

    def test_search_fts_basic(self, tmp_db):
        insert_memory(tmp_db, str(_gen_uuid7()), "Abraham Lincoln president", "2025-01-01T00:00:00+00:00", "owner1", None)
        results = search_fts(tmp_db, "Lincoln")
        assert len(results) >= 1

    def test_search_fts_short_query_skipped(self, tmp_db):
        results = search_fts(tmp_db, "I")
        assert results == []

    def test_search_fts_punctuation_resilience(self, tmp_db):
        insert_memory(tmp_db, str(_gen_uuid7()), "Planet [Alpha-9] temperature settings", "2025-01-01T00:00:00+00:00", "owner1", None)
        results = search_fts(tmp_db, "Alpha-9 temperature")
        assert len(results) >= 1


class TestVectorSearch:
    def test_search_vector_returns_results(self, tmp_db):
        vec = make_vec()
        insert_memory(tmp_db, str(_gen_uuid7()), "vector test", "2025-01-01T00:00:00+00:00", "owner1", vec)
        results = search_vector(tmp_db, vec, k=5)
        assert len(results) >= 1

    def test_search_vector_empty_db(self, tmp_db):
        vec = make_vec()
        results = search_vector(tmp_db, vec, k=5)
        assert results == []


class TestSerialization:
    def test_serialize_vector_roundtrip(self):
        v = [float(i) / 768 for i in range(768)]
        serialized = serialize_vector(v)
        recovered = deserialize_vector(serialized)
        assert len(recovered) == 768
        assert abs(recovered[0] - v[0]) < 1e-5


class TestTurnIdPairing:
    """Q-A pair reconstruction: one [user] + N [claude i/N] share one turn_id."""

    def test_schema_has_turn_id_column(self, tmp_db):
        info = {r[1] for r in tmp_db.execute("PRAGMA table_info(memories)")}
        assert "turn_id" in info

    def test_turn_id_index_exists(self, tmp_db):
        idx_rows = tmp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memories'"
        ).fetchall()
        names = {r[0] for r in idx_rows}
        assert "idx_memories_turn_id" in names

    def test_insert_memory_persists_turn_id(self, tmp_db):
        tid = "11111111-1111-1111-1111-111111111111"
        insert_memory(
            tmp_db, str(_gen_uuid7()), "[user] hi", "2025-01-01T00:00:00+00:00",
            "owner1", None, None, "claude-code", None, tid,
        )
        row = tmp_db.execute("SELECT turn_id FROM memories").fetchone()
        assert row["turn_id"] == tid

    def test_get_memories_by_turn_id_orders_by_rowid(self, tmp_db):
        tid = "22222222-2222-2222-2222-222222222222"
        insert_memory(tmp_db, str(_gen_uuid7()), "[user] Q", "2025-01-01T00:00:00+00:00",
                      "owner1", None, None, "claude-code", None, tid)
        insert_memory(tmp_db, str(_gen_uuid7()), "[claude 1/2] A1", "2025-01-01T00:00:01+00:00",
                      "owner1", None, None, "claude-code", None, tid)
        insert_memory(tmp_db, str(_gen_uuid7()), "[claude 2/2] A2", "2025-01-01T00:00:02+00:00",
                      "owner1", None, None, "claude-code", None, tid)
        # Unrelated row
        insert_memory(tmp_db, str(_gen_uuid7()), "[user] other", "2025-01-01T00:00:03+00:00",
                      "owner1", None, None, "claude-code", None, "deadbeef")

        rows = get_memories_by_turn_id(tmp_db, tid)
        assert len(rows) == 3
        contents = [r["content"] for r in rows]
        assert contents == ["[user] Q", "[claude 1/2] A1", "[claude 2/2] A2"]

    def test_get_memories_by_turn_id_empty_on_unknown(self, tmp_db):
        assert get_memories_by_turn_id(tmp_db, "no-such-turn") == []

    def test_get_memories_by_turn_id_empty_string(self, tmp_db):
        assert get_memories_by_turn_id(tmp_db, "") == []

    def test_get_all_memories_exposes_turn_id(self, tmp_db):
        tid = "33333333-3333-3333-3333-333333333333"
        insert_memory(tmp_db, str(_gen_uuid7()), "[user] x", "2025-01-01T00:00:00+00:00",
                      "owner1", None, None, "claude-code", None, tid)
        rows = get_all_memories(tmp_db)
        assert rows
        assert rows[0]["turn_id"] == tid

    def test_get_memory_by_rowid_exposes_turn_id(self, tmp_db):
        tid = "44444444-4444-4444-4444-444444444444"
        rowid = insert_memory(
            tmp_db, str(_gen_uuid7()), "[user] y", "2025-01-01T00:00:00+00:00",
            "owner1", None, None, "claude-code", None, tid,
        )
        row = get_memory_by_rowid(tmp_db, rowid)
        assert row["turn_id"] == tid

    def test_insert_without_turn_id_stores_null(self, tmp_db):
        insert_memory(tmp_db, str(_gen_uuid7()), "[user] no-turn", "2025-01-01T00:00:00+00:00",
                      "owner1", None)
        row = tmp_db.execute("SELECT turn_id FROM memories").fetchone()
        assert row["turn_id"] is None
