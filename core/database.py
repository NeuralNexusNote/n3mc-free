"""N3MemoryCore — DB layer.

SQLite + sqlite-vec + FTS5. Synchronous COMMITs per record (no buffering).
See N3MemoryCore_v1.2.0_Free_EN.md §3 for design constraints.
"""
from __future__ import annotations

import os
import re
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    import sqlite_vec
except ImportError as e:  # pragma: no cover - import guard
    raise SystemExit("sqlite-vec is required. Run: pip install sqlite-vec") from e

try:
    from uuid_extensions import uuid7 as _uuid7
except ImportError as e:  # pragma: no cover
    raise SystemExit("uuid7 is required. Run: pip install uuid7") from e


N3MC_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEMORY_DIR = N3MC_ROOT / ".memory"
DEFAULT_DB_PATH = DEFAULT_MEMORY_DIR / "n3memory.db"

VECTOR_DIM = 768

_FTS_PUNCT_RE = re.compile(r'[()[\]{}<>,.!?;:\-—–…\'\"\`~@#$%^&*+=|/\\]')
_FTS_MAX_TERMS = 30


# ---------------------------------------------------------------------------
# Vector serialization
# ---------------------------------------------------------------------------
def serialize_vector(vec: Iterable[float]) -> bytes:
    """Pack a float[768] vector for sqlite-vec storage."""
    arr = list(vec)
    if len(arr) != VECTOR_DIM:
        raise ValueError(f"expected {VECTOR_DIM}-dim vector, got {len(arr)}")
    return struct.pack(f"{VECTOR_DIM}f", *arr)


def deserialize_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{VECTOR_DIM}f", blob))


# ---------------------------------------------------------------------------
# FTS punctuation stripping (mandatory on both INSERT and MATCH; spec §3)
# ---------------------------------------------------------------------------
def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


def _quote_fts_query(text: str) -> str:
    stripped = strip_fts_punctuation(text)
    terms = stripped.split()[:_FTS_MAX_TERMS]
    return ' '.join(f'"{t}"' for t in terms)


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------
def _load_vec_extension(conn: sqlite3.Connection) -> None:
    """Idempotent: loading twice is a no-op (per spec §5)."""
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "already" in msg or "duplicate" in msg:
            return
        raise


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection with the durability PRAGMAs forced (spec §3).

    PRAGMA synchronous=FULL, journal_mode=WAL applied on every connection.
    sqlite-vec extension is loaded.
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError as e:
        raise sqlite3.DatabaseError(
            f"PRAGMA failed: {e}. The DB may be corrupted. "
            f"Rename {path} to {path}.corrupt.bak and re-run to regenerate."
        ) from e
    _load_vec_extension(conn)
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_DDL = [
    """
    CREATE TABLE IF NOT EXISTS memories (
        id        TEXT PRIMARY KEY,
        content   TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        owner_id  TEXT NOT NULL,
        local_id  TEXT,
        agent_name TEXT,
        turn_id   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories(owner_id)",
    # idx_memories_turn_id is created in migrate_schema, after the column is added.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content,
        tokenize='porter unicode61'
    )
    """,
    f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
        embedding float[{VECTOR_DIM}]
    )
    """,
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _fts_uses_trigram(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if not row or not row["sql"]:
        return False
    return "tokenize='trigram'" in row["sql"] or "tokenize=trigram" in row["sql"]


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema migration. Adds missing columns, repairs FTS tokenizer."""
    cols = _table_columns(conn, "memories")
    if "local_id" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN local_id TEXT")
    if "agent_name" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN agent_name TEXT")
    if "turn_id" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN turn_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id)"
        )

    if _fts_uses_trigram(conn):
        conn.execute("DROP TABLE memories_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE memories_fts USING fts5("
            "content, tokenize='porter unicode61')"
        )
        for row in conn.execute("SELECT rowid, content FROM memories"):
            conn.execute(
                "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                (row["rowid"], strip_fts_punctuation(row["content"] or "")),
            )
    conn.commit()


def init_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Create schema if absent, then migrate. Returns an open connection."""
    conn = get_connection(db_path)
    for stmt in SCHEMA_DDL:
        conn.execute(stmt)
    conn.commit()
    migrate_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_memory(
    conn: sqlite3.Connection,
    content: str,
    embedding: Optional[Iterable[float]],
    owner_id: str,
    local_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    *,
    turn_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> tuple[str, int]:
    """Insert one record across memories, memories_fts, memories_vec.

    Synchronous: INSERT + COMMIT happen immediately (spec §3 "Immediate
    Physical Writes"). Returns (id, rowid).
    """
    mem_id = str(_uuid7())
    ts = timestamp or _now_iso()
    cur = conn.execute(
        "INSERT INTO memories(id, content, timestamp, owner_id, local_id, agent_name, turn_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mem_id, content, ts, owner_id, local_id, agent_name, turn_id),
    )
    rowid = cur.lastrowid
    conn.execute(
        "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
        (rowid, strip_fts_punctuation(content)),
    )
    if embedding is not None:
        conn.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
            (rowid, serialize_vector(embedding)),
        )
    conn.commit()
    return mem_id, rowid


def delete_memory(conn: sqlite3.Connection, memory_id: str) -> bool:
    """Transactional delete from all three tables (spec §5)."""
    _load_vec_extension(conn)
    row = conn.execute(
        "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return False
    rowid = row["rowid"]
    try:
        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories WHERE rowid = ?", (rowid,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return True


def count_memories(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]


def get_all_memories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id, content, timestamp, owner_id, local_id, agent_name, turn_id "
            "FROM memories ORDER BY timestamp ASC"
        )
    )


def get_memory_by_rowid(
    conn: sqlite3.Connection, rowid: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT rowid, id, content, timestamp, owner_id, local_id, agent_name, turn_id "
        "FROM memories WHERE rowid = ?",
        (rowid,),
    ).fetchone()


def get_memories_by_turn_id(
    conn: sqlite3.Connection, turn_id: str
) -> list[sqlite3.Row]:
    """Sibling rows for Q-A pair reconstruction (spec §5 Q-A Pairing)."""
    return list(
        conn.execute(
            "SELECT rowid, id, content, timestamp, owner_id, local_id, agent_name, turn_id "
            "FROM memories WHERE turn_id = ? ORDER BY rowid ASC",
            (turn_id,),
        )
    )


def check_exact_duplicate(conn: sqlite3.Connection, content: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM memories WHERE content = ? LIMIT 1", (content,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def search_vector(
    conn: sqlite3.Connection,
    query_vec: Iterable[float],
    k: int = 50,
) -> list[dict]:
    """KNN over memories_vec joined with memories. Returns list of dicts."""
    _load_vec_extension(conn)
    blob = serialize_vector(query_vec)
    rows = conn.execute(
        "SELECT v.rowid AS rowid, v.distance AS distance, "
        "       m.id AS id, m.content AS content, m.timestamp AS timestamp, "
        "       m.owner_id AS owner_id, m.local_id AS local_id, "
        "       m.agent_name AS agent_name, m.turn_id AS turn_id "
        "FROM memories_vec v "
        "JOIN memories m ON m.rowid = v.rowid "
        "WHERE v.embedding MATCH ? AND k = ? "
        "ORDER BY v.distance ASC",
        (blob, k),
    ).fetchall()
    return [dict(r) for r in rows]


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    k: int = 50,
) -> list[dict]:
    """FTS5 BM25 search. Skips queries shorter than 2 chars (spec §3)."""
    if not query or len(query.strip()) < 2:
        return []
    fts_query = _quote_fts_query(query)
    if not fts_query:
        return []
    try:
        rows = conn.execute(
            "SELECT m.rowid AS rowid, bm25(memories_fts) AS bm25_raw, "
            "       m.id AS id, m.content AS content, m.timestamp AS timestamp, "
            "       m.owner_id AS owner_id, m.local_id AS local_id, "
            "       m.agent_name AS agent_name, m.turn_id AS turn_id "
            "FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE memories_fts MATCH ? "
            "ORDER BY bm25(memories_fts) ASC LIMIT ?",
            (fts_query, k),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Repair / GC
# ---------------------------------------------------------------------------
def find_unindexed_memories(
    conn: sqlite3.Connection, limit: int = 200, offset: int = 0
) -> list[dict]:
    """Records present in memories but missing in memories_vec OR memories_fts.

    Double LEFT JOIN per spec §5. Returns dicts with vec_missing/fts_missing flags.
    """
    rows = conn.execute(
        "SELECT m.rowid AS rowid, m.id AS id, m.content AS content, "
        "       (v.rowid IS NULL) AS vec_missing, "
        "       (f.rowid IS NULL) AS fts_missing "
        "FROM memories m "
        "LEFT JOIN memories_vec v ON v.rowid = m.rowid "
        "LEFT JOIN memories_fts f ON f.rowid = m.rowid "
        "WHERE v.rowid IS NULL OR f.rowid IS NULL "
        "ORDER BY m.rowid ASC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def gc_expired(conn: sqlite3.Connection, retain_days: int) -> int:
    """Delete records older than retain_days. Returns number deleted."""
    if retain_days <= 0:
        return 0
    cutoff_dt = datetime.now(timezone.utc).timestamp() - retain_days * 86400
    deleted = 0
    rows = conn.execute(
        "SELECT id, timestamp FROM memories"
    ).fetchall()
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.timestamp() < cutoff_dt:
                if delete_memory(conn, r["id"]):
                    deleted += 1
        except (ValueError, TypeError):
            continue
    return deleted


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------
def check_integrity(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return bool(row and row[0] == "ok")


def quarantine_corrupt_db(db_path: Path) -> Path:
    bak = db_path.with_suffix(db_path.suffix + ".corrupt.bak")
    if db_path.exists():
        db_path.rename(bak)
    return bak
