import os
import sys
import uuid
import math
from datetime import datetime, timezone

import pytest

from n3memorycore.core.database import (
    get_connection, init_db, migrate_schema,
    insert_memory, search_vector, search_fts,
    get_all_memories, delete_memory, count_memories,
    check_exact_duplicate, find_unindexed_memories,
    serialize_vector, deserialize_vector,
    strip_fts_punctuation, _quote_fts_query,
)


def _ts():
    return datetime.now(timezone.utc).isoformat()


def _vec(val=0.5, dim=768):
    raw = [val] * dim
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


class TestSchema:
    def test_init_db_creates_tables(self, tmp_db):
        conn = get_connection(tmp_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert 'memories' in tables
        conn.close()

    def test_migrate_schema_idempotent(self, tmp_db):
        conn = get_connection(tmp_db)
        migrate_schema(conn)
        migrate_schema(conn)  # second call must not raise
        conn.close()

    def test_migrate_schema_adds_missing_columns(self, tmp_path):
        import sqlite3
        db = str(tmp_path / 'old.db')
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, timestamp TEXT NOT NULL, owner_id TEXT NOT NULL)")
        conn.commit()
        conn.close()

        conn2 = get_connection(db)
        init_db(conn2)
        migrate_schema(conn2)
        cols = {r[1] for r in conn2.execute("PRAGMA table_info(memories)").fetchall()}
        assert 'local_id' in cols
        assert 'agent_name' in cols
        assert 'turn_id' in cols
        conn2.close()


class TestInsertAndRetrieve:
    def test_insert_and_count(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'hello', _ts(), cfg['owner_id'])
        assert count_memories(conn) == 1
        conn.close()

    def test_insert_populates_all_three_tables(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        vec = _vec()
        insert_memory(conn, str(uuid.uuid4()), 'test content', _ts(),
                      cfg['owner_id'], embedding=vec)
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 1
        conn.close()

    def test_insert_without_embedding(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'no vec', _ts(), cfg['owner_id'])
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 0
        conn.close()

    def test_get_all_memories(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'aaa', _ts(), cfg['owner_id'])
        insert_memory(conn, str(uuid.uuid4()), 'bbb', _ts(), cfg['owner_id'])
        rows = get_all_memories(conn)
        assert len(rows) == 2
        conn.close()


class TestDelete:
    def test_delete_removes_from_all_three_tables(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        mid = str(uuid.uuid4())
        insert_memory(conn, mid, 'del me', _ts(), cfg['owner_id'], embedding=_vec())
        assert count_memories(conn) == 1
        delete_memory(conn, mid)
        assert count_memories(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 0
        conn.close()

    def test_delete_nonexistent_no_error(self, tmp_db):
        conn = get_connection(tmp_db)
        result = delete_memory(conn, 'nonexistent-id')
        assert result is False
        conn.close()


class TestDedup:
    def test_check_exact_duplicate_true(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'exact', _ts(), cfg['owner_id'])
        assert check_exact_duplicate(conn, 'exact') is True
        conn.close()

    def test_check_exact_duplicate_false(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'original', _ts(), cfg['owner_id'])
        assert check_exact_duplicate(conn, 'different') is False
        conn.close()


class TestUnindexed:
    def test_find_unindexed_vec_missing(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'no embed', _ts(), cfg['owner_id'])
        rows = find_unindexed_memories(conn)
        assert len(rows) == 1
        conn.close()

    def test_find_unindexed_all_indexed(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'full', _ts(), cfg['owner_id'], embedding=_vec())
        rows = find_unindexed_memories(conn)
        assert len(rows) == 0
        conn.close()


class TestFTS:
    def test_strip_fts_punctuation(self):
        result = strip_fts_punctuation("hello, world! (test)")
        assert ',' not in result
        assert '!' not in result
        assert '(' not in result

    def test_quote_fts_query(self):
        q = _quote_fts_query("hello world")
        assert '"hello"' in q
        assert '"world"' in q

    def test_quote_fts_query_max_terms(self):
        many = ' '.join([f'word{i}' for i in range(50)])
        q = _quote_fts_query(many)
        terms = q.split()
        assert len(terms) <= 30

    def test_search_fts_basic(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'Abraham Lincoln president', _ts(), cfg['owner_id'])
        rows = search_fts(conn, 'Lincoln', limit=5)
        assert len(rows) >= 1
        conn.close()

    def test_search_fts_short_query_skipped(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'some text', _ts(), cfg['owner_id'])
        rows = search_fts(conn, 'a', limit=5)
        assert rows == []
        conn.close()

    def test_search_fts_punctuation_resilience(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        insert_memory(conn, str(uuid.uuid4()), 'Planet [Alpha-9] temperature settings',
                      _ts(), cfg['owner_id'])
        rows = search_fts(conn, 'Alpha temperature', limit=5)
        assert len(rows) >= 1
        conn.close()


class TestVectorSearch:
    def test_search_vector_returns_results(self, tmp_db, cfg):
        conn = get_connection(tmp_db)
        vec = _vec(0.5)
        insert_memory(conn, str(uuid.uuid4()), 'vec content', _ts(),
                      cfg['owner_id'], embedding=vec)
        rows = search_vector(conn, vec, k=5)
        assert len(rows) >= 1
        conn.close()

    def test_search_vector_empty_db(self, tmp_db):
        conn = get_connection(tmp_db)
        rows = search_vector(conn, _vec(), k=5)
        assert rows == []
        conn.close()


class TestSerialization:
    def test_serialize_vector_roundtrip(self):
        vec = [0.1, 0.2, 0.3, 0.4]
        data = serialize_vector(vec)
        back = deserialize_vector(data)
        assert len(back) == len(vec)
        for a, b in zip(vec, back):
            assert abs(a - b) < 1e-6
