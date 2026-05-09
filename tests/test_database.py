"""Layer 1: DB unit tests — CRUD, schema, transactions (spec §7)."""
import math
import uuid
import pytest

from n3memorycore.core.database import (
    get_connection, init_db, migrate_schema,
    insert_memory, count_memories, check_exact_duplicate,
    search_vector, search_fts, get_all_memories, delete_memory,
    find_unindexed_memories, get_memories_by_turn_id,
    serialize_vector, deserialize_vector, strip_fts_punctuation,
)


def _vec(val: float = 0.5, dim: int = 768) -> list:
    raw = [val] * dim
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def _fresh_db(tmp_path, name='test.db'):
    db_path = str(tmp_path / name)
    conn = get_connection(db_path)
    init_db(conn)
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
class TestSchema:

    def test_tables_created(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','shadow')"
        ).fetchall()}
        conn.close()
        assert 'memories' in tables

    def test_migrate_idempotent(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        migrate_schema(conn)
        migrate_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
        conn.close()
        assert {'local_id', 'agent_name', 'session_id', 'turn_id'} <= cols

    def test_add_missing_columns(self, tmp_path):
        """Migrate adds columns to an old schema."""
        import sqlite3
        db_path = str(tmp_path / 'old.db')
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT NOT NULL,
                timestamp TEXT NOT NULL, owner_id TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        conn = get_connection(db_path)
        migrate_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
        conn.close()
        assert 'local_id' in cols
        assert 'agent_name' in cols

    def test_fts_tokenizer_is_trigram(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert 'trigram' in row[0].lower()

    def test_migrate_tokenizer_to_trigram(self, tmp_path):
        """If FTS was created with porter unicode61, migrate_schema upgrades to trigram."""
        import sqlite3
        import sqlite_vec
        db_path = str(tmp_path / 'porter.db')
        conn_raw = sqlite3.connect(db_path)
        conn_raw.enable_load_extension(True)
        sqlite_vec.load(conn_raw)
        conn_raw.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT NOT NULL,
                timestamp TEXT NOT NULL, owner_id TEXT NOT NULL,
                local_id TEXT, agent_name TEXT, session_id TEXT, turn_id TEXT
            )
        """)
        conn_raw.execute("""
            CREATE VIRTUAL TABLE memories_fts USING fts5(
                content, tokenize='porter unicode61'
            )
        """)
        conn_raw.execute("""
            CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[768])
        """)
        conn_raw.commit()
        conn_raw.close()

        conn = get_connection(db_path)
        migrate_schema(conn)
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        conn.close()
        assert 'trigram' in row[0].lower()


# ---------------------------------------------------------------------------
class TestInsertAndRetrieve:

    def test_insert_count(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()), 'hello', '2026-01-01T00:00:00',
                      owner_id='owner1')
        assert count_memories(conn) == 1
        conn.close()

    def test_three_table_consistency(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        mid = str(uuid.uuid4())
        vec = _vec()
        insert_memory(conn, mid, 'test content', '2026-01-01T00:00:00',
                      owner_id='owner1', embedding=vec)
        row = conn.execute("SELECT rowid FROM memories WHERE id = ?", (mid,)).fetchone()
        rowid = row[0]
        fts_row = conn.execute("SELECT rowid FROM memories_fts WHERE rowid = ?", (rowid,)).fetchone()
        vec_row = conn.execute("SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone()
        conn.close()
        assert fts_row is not None
        assert vec_row is not None

    def test_insert_without_embedding(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        mid = str(uuid.uuid4())
        insert_memory(conn, mid, 'no embedding', '2026-01-01T00:00:00', owner_id='o')
        assert count_memories(conn) == 1
        conn.close()

    def test_rowid_returned(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        rowid = insert_memory(conn, str(uuid.uuid4()), 'content', '2026-01-01T00:00:00',
                              owner_id='o')
        conn.close()
        assert isinstance(rowid, int) and rowid > 0


# ---------------------------------------------------------------------------
class TestDelete:

    def test_delete_three_tables(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        mid = str(uuid.uuid4())
        vec = _vec()
        rowid = insert_memory(conn, mid, 'to delete', '2026-01-01T00:00:00',
                              owner_id='o', embedding=vec)
        delete_memory(conn, mid)
        assert count_memories(conn) == 0
        fts = conn.execute("SELECT rowid FROM memories_fts WHERE rowid = ?", (rowid,)).fetchone()
        vec_r = conn.execute("SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone()
        conn.close()
        assert fts is None
        assert vec_r is None

    def test_delete_nonexistent(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        result = delete_memory(conn, 'does-not-exist')
        conn.close()
        assert result is False


# ---------------------------------------------------------------------------
class TestDedup:

    def test_exact_duplicate_detected(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()), 'dupe text', '2026-01-01T00:00:00', owner_id='o')
        assert check_exact_duplicate(conn, 'dupe text') is True
        conn.close()

    def test_no_false_positive(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()), 'text A', '2026-01-01T00:00:00', owner_id='o')
        assert check_exact_duplicate(conn, 'text B') is False
        conn.close()


# ---------------------------------------------------------------------------
class TestUnindexed:

    def test_vec_missing_detected(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()), 'no vec', '2026-01-01T00:00:00', owner_id='o')
        rows = find_unindexed_memories(conn)
        conn.close()
        assert len(rows) == 1

    def test_all_indexed(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()), 'has vec', '2026-01-01T00:00:00',
                      owner_id='o', embedding=_vec())
        rows = find_unindexed_memories(conn)
        conn.close()
        assert len(rows) == 0


# ---------------------------------------------------------------------------
class TestFTS:

    def test_punctuation_removal(self):
        text = '「こんにちは」（世界）！'
        result = strip_fts_punctuation(text)
        assert '「' not in result
        assert '」' not in result
        assert '（' not in result
        assert '）' not in result
        assert '！' not in result

    def test_short_query_skipped(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        # 2 ASCII bytes < 3 UTF-8 bytes → skipped
        rows = search_fts(conn, 'ab')
        conn.close()
        assert rows == []

    def test_basic_search(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()), '坂本龍馬は幕末の志士', '2026-01-01T00:00:00', owner_id='o')
        rows = search_fts(conn, '坂本龍馬')
        conn.close()
        assert len(rows) > 0

    def test_punctuation_tolerance(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()),
                      '架空の惑星アルファ9の気温設定', '2026-01-01T00:00:00', owner_id='o')
        rows = search_fts(conn, 'アルファ9の気温')
        conn.close()
        assert len(rows) > 0

    def test_max_terms_limit(self):
        long_query = ' '.join([f'term{i}' for i in range(50)])
        stripped = strip_fts_punctuation(long_query)
        assert isinstance(stripped, str)


# ---------------------------------------------------------------------------
class TestVectorSearch:

    def test_vector_search_results(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        vec = _vec(0.5)
        insert_memory(conn, str(uuid.uuid4()), 'vec content', '2026-01-01T00:00:00',
                      owner_id='o', embedding=vec)
        rows = search_vector(conn, vec, k=5)
        conn.close()
        assert len(rows) > 0

    def test_empty_db_vector_search(self, tmp_path):
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        rows = search_vector(conn, _vec(), k=5)
        conn.close()
        assert rows == []


# ---------------------------------------------------------------------------
class TestGC:
    """Free edition has no TTL/auto-expire: records are retained indefinitely.
    Also verifies that --since/--until time-range filters work correctly."""

    def test_old_records_retained(self, tmp_path):
        """Records with old timestamps are never auto-deleted in Free edition."""
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()), 'ancient record',
                      '2020-01-01T00:00:00', owner_id='o')
        assert count_memories(conn) == 1
        conn.close()

    def test_no_gc_decrements_count(self, tmp_path):
        """Inserting N records without explicit delete always yields N in Free."""
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        for i in range(3):
            insert_memory(conn, str(uuid.uuid4()), f'record {i}',
                          '2020-01-01T00:00:00', owner_id='o')
        assert count_memories(conn) == 3
        conn.close()

    def test_since_filter_excludes_old(self, tmp_path):
        """get_all_memories(since=...) excludes records older than the cutoff."""
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()), 'old', '2020-01-01T00:00:00', owner_id='o')
        insert_memory(conn, str(uuid.uuid4()), 'new', '2026-01-01T00:00:00', owner_id='o')
        rows = get_all_memories(conn, since='2025-01-01T00:00:00')
        conn.close()
        assert len(rows) == 1
        assert rows[0]['content'] == 'new'

    def test_until_filter_excludes_new(self, tmp_path):
        """get_all_memories(until=...) excludes records newer than the cutoff."""
        db = _fresh_db(tmp_path)
        conn = get_connection(db)
        insert_memory(conn, str(uuid.uuid4()), 'old', '2020-01-01T00:00:00', owner_id='o')
        insert_memory(conn, str(uuid.uuid4()), 'new', '2026-01-01T00:00:00', owner_id='o')
        rows = get_all_memories(conn, until='2021-01-01T00:00:00')
        conn.close()
        assert len(rows) == 1
        assert rows[0]['content'] == 'old'


# ---------------------------------------------------------------------------
class TestSerialization:

    def test_roundtrip(self):
        vec = _vec()
        assert len(vec) == 768
        data = serialize_vector(vec)
        recovered = deserialize_vector(data)
        assert len(recovered) == 768
        assert abs(recovered[0] - vec[0]) < 1e-6
