import sqlite3
import struct
import re
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

# spec §3: trigram tokenizer for Japanese substring matching
_FTS_PUNCT_RE = re.compile(
    r'[「」『』【】（）()\[\]{}<>〈〉《》・、。,.!！?？;；:：\-―─…\'\"“”‘’]'
)
_FTS_MAX_TERMS = 40
_FTS_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
# CJK: hiragana, katakana, CJK ideographs, compatibility ideographs, hangul
_CJK_CHAR_RE = re.compile(r'[぀-ヿ㐀-鿿豈-﫿가-힯]')
_HIRAGANA_ONLY_RE = re.compile(r'^[぀-ゟー]+$')


def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


def _query_words(query: str) -> list:
    """Control chars and punctuation → spaces, then split on whitespace."""
    return _FTS_PUNCT_RE.sub(' ', _FTS_CTRL_RE.sub(' ', query or '')).split()


def _quote_or(terms: list) -> str:
    return ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)


def _build_fts_query(query: str) -> str:
    """spec §3 (2026-11-17): build a safe FTS5 MATCH string for the trigram
    index.

    Control chars and punctuation → spaces → split on whitespace. A word of
    more than 3 characters that contains CJK is split into its overlapping
    3-character grams (Japanese has no spaces, so the whole sentence used to
    be one term that matched only a record containing that exact sentence);
    hiragana-only grams (してく, ください) are dropped. Other words of 3+
    characters stay whole; shorter words are dropped (trigram can never
    match them; the bigram index covers them). Duplicates removed, at most
    40 terms, each quoted as an FTS5 string, joined with OR. Returns "" when
    the keyword search should be skipped.
    """
    terms: list = []
    seen: set = set()
    for word in _query_words(query):
        if len(word) > 3 and _CJK_CHAR_RE.search(word):
            grams = [word[i:i + 3] for i in range(len(word) - 2)]
            grams = [g for g in grams if not _HIRAGANA_ONLY_RE.match(g)]
        elif len(word) >= 3:
            grams = [word]
        else:
            grams = []
        for g in grams:
            if g not in seen:
                seen.add(g)
                terms.append(g)
    terms = terms[:_FTS_MAX_TERMS]
    if not terms:
        return ''                                 # → skip keyword search
    return _quote_or(terms)


def _bigram_tokens(text: str) -> list:
    """Tokens for memories_fts_bi: each CJK run becomes its overlapping
    2-character grams (a 1-character run stays as it is); other runs (Latin
    words, numbers) stay whole. Punctuation is stripped as on the trigram
    side. Example: `N3MCの設定` → `N3MC の設 設定`."""
    folded = _FTS_CTRL_RE.sub(' ', strip_fts_punctuation(text or ''))
    out: list = []
    for word in folded.split():
        run, run_is_cjk = '', None
        for ch in word + '\0':
            is_cjk = ch != '\0' and bool(_CJK_CHAR_RE.match(ch))
            if run and (ch == '\0' or is_cjk != run_is_cjk):
                if run_is_cjk and len(run) > 1:
                    out.extend(run[i:i + 2] for i in range(len(run) - 1))
                else:
                    out.append(run)
                run = ''
            if ch != '\0':
                run += ch
                run_is_cjk = is_cjk
    return out


def bigram_text(text: str) -> str:
    """The text stored in memories_fts_bi: tokens joined by spaces."""
    return ' '.join(_bigram_tokens(text))


def build_bigram_query(query: str) -> str:
    """spec §3 (2026-11-17): MATCH string for memories_fts_bi — the query's
    bigram tokens, quoted and OR-ed. Hiragana-only tokens (して, ます) are
    dropped when anything else is left; a 1-character non-CJK token is
    dropped. Unlike the trigram side, 2-character words (記憶, PR) match.
    Returns "" when there is nothing to search."""
    terms: list = []
    seen: set = set()
    for t in _bigram_tokens(_FTS_PUNCT_RE.sub(' ', query or '')):
        if t in seen:
            continue
        seen.add(t)
        if len(t) < 2 and not _CJK_CHAR_RE.match(t):
            continue
        terms.append(t)
    content = [t for t in terms if not _HIRAGANA_ONLY_RE.match(t)]
    terms = (content or terms)[:_FTS_MAX_TERMS]
    if not terms:
        return ''
    return _quote_or(terms)


# Public alias (pre-v1.4.0 draft name).
build_fts_query = _build_fts_query


def serialize_vector(vec: list) -> bytes:
    return struct.pack(f'{len(vec)}f', *vec)


def deserialize_vector(data: bytes) -> list:
    n = len(data) // 4
    return list(struct.unpack(f'{n}f', data))


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    # spec §5: idempotent — duplicate load is a no-op
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
    # spec §3: SQLite durability settings
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 1000")
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
    # spec §3: standalone FTS5 with trigram tokenizer
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content,
            tokenize='trigram'
        )
    """)
    # spec §3 (2026-11-17): CJK bigram index. trigram cannot match a
    # 2-character word (記憶, PR); this table stores bigram_text(content)
    # and unicode61 makes each gram one token. Rows share memories.rowid.
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_bi USING fts5(
            content,
            tokenize='unicode61'
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
            embedding float[768]
        )
    """)
    # spec §3.5 "Index metadata" (v1.8.0): how the indexes were built, kept
    # inside the database so that a copied or restored DB carries it.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if 'turn_id' in cols:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id)"
        )
    conn.commit()


def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Caller commits."""
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def meta_delete(conn: sqlite3.Connection, key: str) -> None:
    """Caller commits."""
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def vec_table_dim(conn: sqlite3.Connection) -> Optional[int]:
    """Dimension of memories_vec from its CREATE statement."""
    import re
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'memories_vec'").fetchone()
    m = re.search(r"float\s*\[\s*(\d+)\s*\]", row[0] if row and row[0] else "")
    return int(m.group(1)) if m else None


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

    # spec §3: migrate FTS tokenizer from porter unicode61 to trigram
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    )
    row = cursor.fetchone()
    if row and row[0]:
        sql_lower = row[0].lower()
        if 'trigram' not in sql_lower:
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
    # spec §3: FTS stores punctuation-stripped text; memories stores original
    stripped = strip_fts_punctuation(content)
    conn.execute(
        "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (rowid, stripped)
    )
    conn.execute(
        "INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
        (rowid, bigram_text(content)),
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


_MEMORY_COLS = ("m.id, m.content, m.timestamp, m.owner_id, m.local_id, "
                "m.agent_name, m.session_id, m.turn_id")


def _time_filter(since: Optional[str], until: Optional[str]):
    clauses, params = [], []
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(until)
    return " AND ".join(clauses), params


def _rows_with_distance(conn, dist_rows) -> List[dict]:
    """Join (rowid, distance) pairs back to memories, keeping distance order."""
    if not dist_rows:
        return []
    rowids = [r[0] for r in dist_rows]
    placeholders = ','.join('?' * len(rowids))
    rows = conn.execute(
        f"SELECT {_MEMORY_COLS}, m.rowid AS rowid FROM memories m "
        f"WHERE m.rowid IN ({placeholders})",
        rowids,
    ).fetchall()
    by_rowid = {r['rowid']: r for r in rows}
    out = []
    for rowid, dist in dist_rows:
        r = by_rowid.get(rowid)
        if r is None:
            continue
        d = dict(r)
        d['distance'] = dist
        out.append(d)
    return out


def _search_vector_exact(
    conn: sqlite3.Connection,
    embedding: list,
    k: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[dict]:
    """spec §3 (v1.4.0) fallback when KNN with `rowid IN (subquery)` raises
    OperationalError (older sqlite-vec): compute vec_distance_l2 for every
    filtered row and sort by distance."""
    where, tparams = _time_filter(since, until)
    sub = f"SELECT rowid FROM memories WHERE {where}" if where else "SELECT rowid FROM memories"
    dist_rows = conn.execute(
        f"""SELECT mv.rowid, vec_distance_l2(mv.embedding, ?) AS distance
            FROM memories_vec mv
            WHERE mv.rowid IN ({sub})
            ORDER BY distance
            LIMIT ?""",
        [serialize_vector(embedding)] + tparams + [k],
    ).fetchall()
    return _rows_with_distance(conn, [(r[0], r[1]) for r in dist_rows])


def search_vector_with_filter(
    conn: sqlite3.Connection,
    embedding: list,
    k: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[dict]:
    """spec §3 (v1.4.0): KNN restricted to the filtered rowids.

    vec0 picks the global top-k first, so filtering afterwards drops almost
    every in-range record. Two stages: memories_vec (rowid IN subquery) →
    memories. Falls back to exact vec_distance_l2 on OperationalError.
    """
    where, tparams = _time_filter(since, until)
    if not where:
        return [dict(r) for r in search_vector(conn, embedding, k=k)]
    vec_bytes = serialize_vector(embedding)
    try:
        dist_rows = conn.execute(
            f"""SELECT rowid, distance FROM memories_vec
                WHERE embedding MATCH ? AND k = ?
                  AND rowid IN (SELECT rowid FROM memories WHERE {where})""",
            [vec_bytes, k] + tparams,
        ).fetchall()
        return _rows_with_distance(conn, [(r[0], r[1]) for r in dist_rows])
    except sqlite3.OperationalError as e:
        logger.warning(f"Filtered KNN unavailable ({e}); using exact search")
        try:
            return _search_vector_exact(conn, embedding, k=k, since=since, until=until)
        except Exception as e2:
            logger.warning(f"Vector search (filtered) failed: {e2}")
            return []


def get_vector(conn: sqlite3.Connection, rowid: int) -> Optional[list]:
    row = conn.execute(
        "SELECT embedding FROM memories_vec WHERE rowid = ?", (rowid,)
    ).fetchone()
    if not row or row[0] is None:
        return None
    return deserialize_vector(row[0])


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[sqlite3.Row]:
    # spec §3 (v1.5.0): punctuation → spaces, terms ≥ 3 chars, quoted, OR
    fts_query = _build_fts_query(query)
    if not fts_query:
        return []

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


def search_fts_bigram(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[sqlite3.Row]:
    """spec §3 (2026-11-17): same rows and columns as search_fts, from the
    bigram index. Exceptions are left to the caller (hybrid_search keeps
    the trigram results when this fails)."""
    fts_query = build_bigram_query(query)
    if not fts_query:
        return []
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
    return conn.execute(
        f"""SELECT m.id, m.content, m.timestamp, m.owner_id, m.local_id,
                  m.agent_name, m.session_id, m.turn_id,
                  bm25(memories_fts_bi) AS bm25_score, m.rowid
           FROM memories_fts_bi
           JOIN memories m ON memories_fts_bi.rowid = m.rowid
           WHERE memories_fts_bi MATCH ?{time_sql}
           ORDER BY bm25_score
           LIMIT ?""",
        params,
    ).fetchall()


def fill_bigram_index(conn: sqlite3.Connection, batch_size: int = 500) -> int:
    """spec §3 (2026-11-17): add the memories_fts_bi rows that are missing
    (records saved before the table existed). Idempotent."""
    total = 0
    while True:
        rows = conn.execute(
            "SELECT m.rowid, m.content FROM memories m "
            "WHERE NOT EXISTS (SELECT 1 FROM memories_fts_bi b "
            "WHERE b.rowid = m.rowid) LIMIT ?",
            (batch_size,),
        ).fetchall()
        if not rows:
            break
        conn.executemany(
            "INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
            [(r[0], bigram_text(r[1])) for r in rows],
        )
        conn.commit()
        total += len(rows)
    return total


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
    # spec §5: load vec extension first, then transactional 3-table DELETE
    _load_vec_extension(conn)
    row = conn.execute(
        "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if not row:
        return False
    rowid = row[0]
    try:
        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories_fts_bi WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def count_memories(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def check_exact_duplicate(
    conn: sqlite3.Connection, content: str, turn_id: Optional[str] = None
) -> bool:
    """spec §5.4 (v1.4.0): exact-text duplicate only.

    With turn_id: compare within the same turn only (a re-made setting whose
    unchanged chunks equal the old version's must still be saved).
    Without turn_id (manual --buffer): compare globally.
    """
    if turn_id:
        row = conn.execute(
            "SELECT 1 FROM memories WHERE content = ? AND turn_id = ? LIMIT 1",
            (content, turn_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM memories WHERE content = ? LIMIT 1", (content,)
        ).fetchone()
    return row is not None


def find_unindexed_memories(
    conn: sqlite3.Connection, limit: int = 200, offset: int = 0
) -> List[sqlite3.Row]:
    # spec §3.5: OFFSET should always be 0 in repair loops — repaired records
    # disappear from this result set automatically, so incrementing OFFSET
    # skips remaining unrepaired records.
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
    """Return records for turn_id plus N surrounding turns (before/after)."""
    target_rows = conn.execute(
        "SELECT MIN(rowid) AS min_r, MAX(rowid) AS max_r FROM memories WHERE turn_id = ?",
        (turn_id,),
    ).fetchone()
    if not target_rows or target_rows['min_r'] is None:
        return []

    min_r = target_rows['min_r']
    max_r = target_rows['max_r']

    before_turns_rows = conn.execute(
        """SELECT DISTINCT turn_id FROM memories
           WHERE rowid < ? AND turn_id IS NOT NULL AND turn_id != ?
           GROUP BY turn_id
           ORDER BY MAX(rowid) DESC
           LIMIT ?""",
        (min_r, turn_id, before),
    ).fetchall()
    before_turn_ids = [r[0] for r in before_turns_rows]

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
