"""
N3MemoryCore - database.py
DB layer: schema definitions, CRUD, PRAGMA settings, migrations
"""
import os
import re
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup (required for inter-module imports when called from n3memory.py)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))

# ---------------------------------------------------------------------------
# sqlite-vec extension loader
# ---------------------------------------------------------------------------
def _load_vec_extension(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec extension. Idempotent — ignores 'already loaded' errors."""
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        msg = str(e).lower()
        if "already" in msg or "duplicate" in msg:
            return
        raise

# ---------------------------------------------------------------------------
# FTS punctuation helpers (must be identical on INSERT and MATCH)
# ---------------------------------------------------------------------------
_FTS_PUNCT_RE = re.compile(r'[()[\]{}<>,.!?;:\-—–…\'\"\`~@#$%^&*+=|/\\]')
_FTS_MAX_TERMS = 30


def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


def _quote_fts_query(text: str) -> str:
    stripped = strip_fts_punctuation(text)
    terms = stripped.split()[:_FTS_MAX_TERMS]
    return ' '.join(f'"{t}"' for t in terms)


# ---------------------------------------------------------------------------
# Vector serialization
# ---------------------------------------------------------------------------
def serialize_vector(vec: list) -> bytes:
    """Serialize a list of floats to little-endian float32 bytes."""
    return struct.pack(f'{len(vec)}f', *vec)


def deserialize_vector(data: bytes) -> list:
    n = len(data) // 4
    return list(struct.unpack(f'{n}f', data))


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------
def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with durability settings."""
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _load_vec_extension(conn)
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn
    except sqlite3.DatabaseError as e:
        raise sqlite3.DatabaseError(
            f"DB connection failed: {e}. "
            f"If DB is corrupt, rename {db_path} to {db_path}.corrupt.bak and restart."
        ) from e


# ---------------------------------------------------------------------------
# Schema initialization & migration
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id        TEXT PRIMARY KEY,
            content   TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            owner_id  TEXT NOT NULL,
            local_id  TEXT,
            agent_name  TEXT,
            session_id TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content,
            content='memories',
            content_rowid='rowid',
            tokenize='porter unicode61'
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
            embedding float[768]
        )
    """)
    conn.commit()
    migrate_schema(conn)


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Idempotently add missing columns and migrate FTS tokenizer if needed."""
    # Add missing columns
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    for col, typedef in [("local_id", "TEXT"), ("agent_name", "TEXT"), ("session_id", "TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {typedef}")

    # v1.0.0 → v1.1.0 migration: field renamed agent_id → agent_name.
    # Copy any legacy data so rows saved under v1.0.0 remain usable.
    # The old agent_id column is left in place (orphaned) to avoid a SQLite table rebuild.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    if "agent_id" in existing and "agent_name" in existing:
        conn.execute(
            "UPDATE memories SET agent_name = agent_id "
            "WHERE agent_name IS NULL AND agent_id IS NOT NULL"
        )

    conn.commit()

    # Check FTS tokenizer — migrate from trigram to porter unicode61 if needed
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        if row and row[0] and 'trigram' in row[0].lower():
            # Drop and recreate FTS with new tokenizer
            conn.execute("DROP TABLE IF EXISTS memories_fts")
            conn.execute("""
                CREATE VIRTUAL TABLE memories_fts USING fts5(
                    content,
                    content='memories',
                    content_rowid='rowid',
                    tokenize='porter unicode61'
                )
            """)
            # Re-index all records (SELECT rowid, content → index 0=rowid, 1=content)
            rows = conn.execute("SELECT rowid, content FROM memories").fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                    (r[0], strip_fts_punctuation(r[1]))
                )
            conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def insert_memory(
    conn: sqlite3.Connection,
    record_id: str,
    content: str,
    timestamp: str,
    owner_id: str,
    embedding: Optional[list],
    local_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int:
    """
    Insert into memories + memories_fts + memories_vec (if embedding provided).
    Returns the implicit SQLite rowid of the inserted record.
    """
    cursor = conn.execute(
        """INSERT INTO memories(id, content, timestamp, owner_id, local_id, agent_name, session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (record_id, content, timestamp, owner_id, local_id, agent_name, session_id),
    )
    rowid = cursor.lastrowid

    # FTS — strip punctuation before indexing
    conn.execute(
        "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
        (rowid, strip_fts_punctuation(content)),
    )

    # Vector index
    if embedding is not None:
        conn.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
            (rowid, serialize_vector(embedding)),
        )

    conn.commit()
    return rowid


def delete_memory(conn: sqlite3.Connection, record_id: str) -> bool:
    """Delete a record from all three tables transactionally."""
    _load_vec_extension(conn)
    row = conn.execute("SELECT rowid FROM memories WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return False
    rowid = row[0]
    try:
        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories WHERE id = ?", (record_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def get_all_memories(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT id, content, timestamp, owner_id, local_id, agent_name, session_id, rowid FROM memories ORDER BY timestamp DESC"
    ).fetchall()


def count_memories(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def check_exact_duplicate(conn: sqlite3.Connection, content: str) -> bool:
    row = conn.execute("SELECT 1 FROM memories WHERE content = ? LIMIT 1", (content,)).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Unindexed detection (for --repair)
# ---------------------------------------------------------------------------
def find_unindexed_memories(conn: sqlite3.Connection) -> list:
    """
    Return records that are missing from memories_vec OR memories_fts.
    Result rows: (id, content, timestamp, rowid, has_vec, has_fts)
    """
    return conn.execute("""
        SELECT m.id, m.content, m.timestamp, m.rowid,
               CASE WHEN mv.rowid IS NOT NULL THEN 1 ELSE 0 END AS has_vec,
               CASE WHEN mf.rowid IS NOT NULL THEN 1 ELSE 0 END AS has_fts
        FROM memories m
        LEFT JOIN memories_vec mv ON mv.rowid = m.rowid
        LEFT JOIN memories_fts mf ON mf.rowid = m.rowid
        WHERE mv.rowid IS NULL OR mf.rowid IS NULL
    """).fetchall()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def search_vector(conn: sqlite3.Connection, query_vec: list, k: int = 50) -> list:
    """
    KNN vector search. Returns list of (rowid, distance).
    """
    vec_bytes = serialize_vector(query_vec)
    rows = conn.execute(
        """SELECT rowid, distance FROM memories_vec
           WHERE embedding MATCH ? AND k = ?
           ORDER BY distance""",
        (vec_bytes, k),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def search_fts(conn: sqlite3.Connection, query: str, limit: int = 50) -> list:
    """
    FTS5 BM25 search. Returns list of (rowid, bm25_score).
    Skips queries shorter than 2 characters (FTS5 porter unicode61 limitation).
    """
    if len(query.strip()) < 2:
        return []
    fts_query = _quote_fts_query(query)
    if not fts_query:
        return []
    try:
        rows = conn.execute(
            """SELECT memories_fts.rowid, bm25(memories_fts) AS score
               FROM memories_fts
               WHERE memories_fts MATCH ?
               ORDER BY score
               LIMIT ?""",
            (fts_query, limit),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except sqlite3.OperationalError:
        return []


def get_memory_by_rowid(conn: sqlite3.Connection, rowid: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, content, timestamp, owner_id, local_id, agent_name, session_id, rowid FROM memories WHERE rowid = ?",
        (rowid,),
    ).fetchone()
