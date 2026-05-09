import sqlite3
import struct
import re
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

# spec §3: trigram tokenizer for Japanese substring matching
# Punctuation is stripped (not replaced with space) before FTS indexing.
_FTS_PUNCT_RE = re.compile(
    r'[「」『』【】（）()\[\]{}<>〈〉《》・、。,.!！?？;；:：\-―─…\'\"“”‘’]'
)
_FTS_MAX_TERMS = 30


def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


def serialize_vector(vec: list) -> bytes:
    return struct.pack(f'{len(vec)}f', *vec)


def deserialize_vector(data: bytes) -> list:
    n = len(data) // 4
    return list(struct.unpack(f'{n}f', data))


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    try:
        conn.enable_load_extension(True)
        try:
            import sqlite_vec
            sqlite_vec.load(conn)
        except Exception as e1:
            msg = str(e1).lower()
            if 'already' in msg or 'duplicate' in msg:
                return
            try:
                conn.load_extension('vec0')
            except Exception as e2:
                msg2 = str(e2).lower()
                if 'already' in msg2 or 'duplicate' in msg2:
                    return
                raise e2
    except Exception as e:
        msg = str(e).lower()
        if 'already' in msg or 'duplicate' in msg:
            return
        raise


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        _load_vec_extension(conn)
    except Exception as e:
        conn.close()
        raise sqlite3.DatabaseError(
            f"Failed to load sqlite-vec extension: {e}. "
            "Recovery: pip install sqlite-vec"
        ) from e
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id         TEXT PRIMARY KEY,
            content    TEXT NOT NULL,
            timestamp  TEXT NOT NULL,
            owner_id   TEXT NOT NULL,
            local_id   TEXT,
            agent_name TEXT,
            session_id TEXT,
            turn_id    TEXT
        )
    """)
    # spec §3: standalone FTS5 with trigram tokenizer for Japanese support
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content,
            tokenize='trigram'
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
            embedding float[768]
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if 'turn_id' in cols:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id)"
        )
    conn.commit()


def migrate_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.execute("PRAGMA table_info(memories)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    for col, typedef in [
        ("local_id",   "TEXT"),
        ("agent_name", "TEXT"),
        ("session_id", "TEXT"),
        ("turn_id",    "TEXT"),
    ]:
        if col not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {typedef}")
                conn.commit()
            except Exception:
                pass

    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memories_turn_id'"
    )
    if not cursor.fetchone():
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id)")
        conn.commit()

    # spec §3: migrate FTS tokenizer FROM porter unicode61 TO trigram
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    )
    row = cursor.fetchone()
    if row and row[0]:
        sql_lower = row[0].lower()
        # If already trigram, nothing to do
        if 'trigram' not in sql_lower:
            # Old table used porter unicode61; re-create with trigram
            # Fetch all existing FTS rowids so we can re-insert
            existing_fts = conn.execute(
                "SELECT rowid, content FROM memories_fts"
            ).fetchall()
            conn.execute("DROP TABLE IF EXISTS memories_fts")
            conn.execute("""
                CREATE VIRTUAL TABLE memories_fts USING fts5(
                    content,
                    tokenize='trigram'
                )
            """)
            for r in existing_fts:
                stripped = strip_fts_punctuation(r[1])
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                    (r[0], stripped)
                )
            conn.commit()
            logger.info("Migrated FTS tokenizer to trigram")


def insert_memory(
    conn: sqlite3.Connection,
    id: str,
    content: str,
    timestamp: str,
    owner_id: str,
    embedding: Optional[list] = None,
    local_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> int:
    conn.execute(
        """INSERT INTO memories
           (id, content, timestamp, owner_id, local_id, agent_name, session_id, turn_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, content, timestamp, owner_id, local_id, agent_name, session_id, turn_id),
    )
    rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    stripped = strip_fts_punctuation(content)
    conn.execute(
        "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (rowid, stripped)
    )
    if embedding is not None:
        conn.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
            (rowid, serialize_vector(embedding)),
        )
    conn.commit()
    return rowid


def search_vector(
    conn: sqlite3.Connection, embedding: list, k: int = 50
) -> List[sqlite3.Row]:
    vec_bytes = serialize_vector(embedding)
    try:
        cursor = conn.execute(
            """SELECT m.id, m.content, m.timestamp, m.owner_id, m.local_id,
                      m.agent_name, m.session_id, m.turn_id, mv.distance, m.rowid
               FROM memories_vec mv
               JOIN memories m ON mv.rowid = m.rowid
               WHERE mv.embedding MATCH ?
                 AND k = ?
               ORDER BY mv.distance""",
            (vec_bytes, k),
        )
        return cursor.fetchall()
    except Exception as e:
        logger.warning(f"Vector search failed: {e}")
        return []


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[sqlite3.Row]:
    # spec §3: trigram requires at least 3 UTF-8 bytes
    stripped = strip_fts_punctuation(query)
    if len(stripped.encode('utf-8')) < 3:
        return []

    # spec §3: trigram does not need double-quote wrapping
    fts_query = stripped[:_FTS_MAX_TERMS * 20]

    time_clauses = []
    params: list = [fts_query]
    if since:
        time_clauses.append("m.timestamp >= ?")
        params.append(since)
    if until:
        time_clauses.append("m.timestamp <= ?")
        params.append(until)
    time_sql = (" AND " + " AND ".join(time_clauses)) if time_clauses else ""
    params.append(limit)

    try:
        cursor = conn.execute(
            f"""SELECT m.id, m.content, m.timestamp, m.owner_id, m.local_id,
                      m.agent_name, m.session_id, m.turn_id,
                      bm25(memories_fts) AS bm25_score, m.rowid
               FROM memories_fts
               JOIN memories m ON memories_fts.rowid = m.rowid
               WHERE memories_fts MATCH ?{time_sql}
               ORDER BY bm25_score
               LIMIT ?""",
            params,
        )
        return cursor.fetchall()
    except Exception as e:
        logger.warning(f"FTS search failed: {e}")
        return []


def search_vector_with_filter(
    conn: sqlite3.Connection,
    embedding: list,
    k: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[sqlite3.Row]:
    vec_bytes = serialize_vector(embedding)
    time_clauses = []
    params: list = [vec_bytes, k]
    if since:
        time_clauses.append("m.timestamp >= ?")
        params.append(since)
    if until:
        time_clauses.append("m.timestamp <= ?")
        params.append(until)
    time_sql = (" AND " + " AND ".join(time_clauses)) if time_clauses else ""

    try:
        cursor = conn.execute(
            f"""SELECT m.id, m.content, m.timestamp, m.owner_id, m.local_id,
                      m.agent_name, m.session_id, m.turn_id, mv.distance, m.rowid
               FROM memories_vec mv
               JOIN memories m ON mv.rowid = m.rowid
               WHERE mv.embedding MATCH ?
                 AND k = ?{time_sql}
               ORDER BY mv.distance""",
            params,
        )
        return cursor.fetchall()
    except Exception as e:
        logger.warning(f"Vector search (filtered) failed: {e}")
        return []


def get_all_memories(
    conn: sqlite3.Connection,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[sqlite3.Row]:
    clauses = []
    params: list = []
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    cursor = conn.execute(
        f"""SELECT id, content, timestamp, owner_id, local_id, agent_name,
                  session_id, turn_id, rowid
           FROM memories {where}
           ORDER BY rowid""",
        params,
    )
    return cursor.fetchall()


def delete_memory(conn: sqlite3.Connection, memory_id: str) -> bool:
    # spec §5: load vec extension, then 3 DELETEs in try/except with rollback
    _load_vec_extension(conn)
    row = conn.execute(
        "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if not row:
        return False
    rowid = row[0]
    try:
        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def count_memories(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def check_exact_duplicate(conn: sqlite3.Connection, content: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM memories WHERE content = ? LIMIT 1", (content,)
    ).fetchone()
    return row is not None


def find_unindexed_memories(
    conn: sqlite3.Connection, limit: int = 200, offset: int = 0
) -> List[sqlite3.Row]:
    cursor = conn.execute(
        """SELECT m.id, m.content, m.timestamp, m.owner_id, m.local_id,
                  m.agent_name, m.session_id, m.turn_id, m.rowid,
                  mv.rowid AS vec_rowid,
                  mf.rowid AS fts_rowid
           FROM memories m
           LEFT JOIN memories_vec mv ON m.rowid = mv.rowid
           LEFT JOIN memories_fts mf ON m.rowid = mf.rowid
           WHERE mv.rowid IS NULL OR mf.rowid IS NULL
           LIMIT ? OFFSET ?""",
        (limit, offset),
    )
    return cursor.fetchall()


def get_memories_by_turn_id(
    conn: sqlite3.Connection, turn_id: str
) -> List[sqlite3.Row]:
    cursor = conn.execute(
        """SELECT id, content, timestamp, owner_id, local_id, agent_name,
                  session_id, turn_id, rowid
           FROM memories
           WHERE turn_id = ?
           ORDER BY
               CASE WHEN content LIKE '[user%' THEN 0 ELSE 1 END,
               rowid""",
        (turn_id,),
    )
    return cursor.fetchall()


def get_thread_context(
    conn: sqlite3.Connection,
    turn_id: str,
    before: int = 2,
    after: int = 2,
) -> List[sqlite3.Row]:
    """Return records for turn_id plus N surrounding turns (before/after).

    Uses idx_memories_turn_id. Returns rows ordered by rowid (chronological).
    """
    # Find rowid range for the target turn
    target_rows = conn.execute(
        "SELECT MIN(rowid) AS min_r, MAX(rowid) AS max_r FROM memories WHERE turn_id = ?",
        (turn_id,),
    ).fetchone()
    if not target_rows or target_rows['min_r'] is None:
        return []

    min_r = target_rows['min_r']
    max_r = target_rows['max_r']

    # Distinct turn_ids that appear strictly before min_r, ordered DESC by their last rowid
    before_turns_rows = conn.execute(
        """SELECT DISTINCT turn_id FROM memories
           WHERE rowid < ? AND turn_id IS NOT NULL AND turn_id != ?
           GROUP BY turn_id
           ORDER BY MAX(rowid) DESC
           LIMIT ?""",
        (min_r, turn_id, before),
    ).fetchall()
    before_turn_ids = [r[0] for r in before_turns_rows]

    # Distinct turn_ids that appear strictly after max_r, ordered ASC by their first rowid
    after_turns_rows = conn.execute(
        """SELECT DISTINCT turn_id FROM memories
           WHERE rowid > ? AND turn_id IS NOT NULL AND turn_id != ?
           GROUP BY turn_id
           ORDER BY MIN(rowid) ASC
           LIMIT ?""",
        (max_r, turn_id, after),
    ).fetchall()
    after_turn_ids = [r[0] for r in after_turns_rows]

    all_turn_ids = before_turn_ids + [turn_id] + after_turn_ids
    if not all_turn_ids:
        return []

    placeholders = ','.join('?' * len(all_turn_ids))
    cursor = conn.execute(
        f"""SELECT id, content, timestamp, owner_id, local_id, agent_name,
                  session_id, turn_id, rowid
            FROM memories
            WHERE turn_id IN ({placeholders})
            ORDER BY rowid""",
        all_turn_ids,
    )
    return cursor.fetchall()
