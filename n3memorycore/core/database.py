"""DB layer: schema, CRUD, PRAGMAs, migrations, FTS5 helpers, KNN search.

Durability first: every write is INSERT + COMMIT at the moment it happens.
No write buffering, no async writes, no batched transactions.
"""
import os
import re
import sqlite3
import struct

from .. import paths

BUSY_TIMEOUT_SECONDS = 10
EMBED_DIM = 768

# ---------------------------------------------------------------- FTS helpers

_FTS_PUNCT_RE = re.compile(
    r'[「」『』【】（）()\[\]{}<>〈〉《》・、。,.!！?？;；:：\-―─…\'\"“”‘’]'
)
_FTS_MAX_TERMS = 40  # keeps FTS5 from freezing on huge inputs
_FTS_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
_CJK_CLASS = '぀-ヿ㐀-鿿豈-﫿가-힯'
_CJK_CHAR_RE = re.compile('[' + _CJK_CLASS + ']')
_CJK_RUN_RE = re.compile('[' + _CJK_CLASS + ']+|[^' + _CJK_CLASS + r'\s]+')
_HIRAGANA_ONLY_RE = re.compile('^[぀-ゟー]+$')


def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


def _quote_terms(terms):
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


# Experimental (2026-11-20, eval P3; not in the spec): English function words dropped from the
# keyword search when config "fts_stopwords" is true. Off by default.
EN_STOPWORDS = frozenset("""
a an the and or but if of to in on at by for with from as into onto about over under than then
so too very just is are was were be been being am do does did done have has had having it its
this that these those there here what which who whom whose when where why how i we you he she
they me us our ours your yours my mine their theirs his her hers them not no can could should
would will shall may might must also any some all each such only own same other more most
get got please tell know let lets
""".split())


def _drop_stopwords(terms):
    kept = [t for t in terms if t.lower() not in EN_STOPWORDS]
    return kept or terms  # a query made only of function words is searched as typed


def fts_terms(query: str, stopwords: bool = False) -> list:
    """The trigram search terms of ``query`` (in order, de-duplicated, at most 40)."""
    terms, seen = [], set()
    for word in _FTS_PUNCT_RE.sub(' ', _FTS_CTRL_RE.sub(' ', query or '')).split():
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
    if stopwords:
        terms = _drop_stopwords(terms)
    return terms[:_FTS_MAX_TERMS]


def _build_fts_query(query: str, stopwords: bool = False) -> str:
    """trigram MATCH query: quoted terms joined by OR. '' means 'skip the search'."""
    terms = fts_terms(query, stopwords)
    return _quote_terms(terms) if terms else ""


def _bigram_tokens(text: str):
    """Tokens of ``text`` (already punctuation-stripped): CJK runs become overlapping
    2-character pieces, everything else is kept whole."""
    out = []
    for tok in _CJK_RUN_RE.findall(text):
        if _CJK_CHAR_RE.match(tok[0]) and len(tok) >= 2:
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok)
    return out


def bigram_text(content: str) -> str:
    """What goes into memories_fts_bi: ``N3MCの設定`` -> ``N3MC の設 設定``."""
    return " ".join(_bigram_tokens(strip_fts_punctuation(content)))


def bigram_terms(query: str, stopwords: bool = False) -> list:
    """The bigram-table search terms of ``query`` (in order, de-duplicated, at most 40)."""
    cleaned = _FTS_PUNCT_RE.sub(' ', _FTS_CTRL_RE.sub(' ', query or ''))
    terms, seen = [], set()
    for tok in _bigram_tokens(cleaned):
        is_cjk = bool(_CJK_CHAR_RE.match(tok[0]))
        if not is_cjk and len(tok) < 2:
            continue
        if tok not in seen:
            seen.add(tok)
            terms.append(tok)
    non_hira = [t for t in terms if not _HIRAGANA_ONLY_RE.match(t)]
    if non_hira:
        terms = non_hira
    if stopwords:
        terms = _drop_stopwords(terms)
    return terms[:_FTS_MAX_TERMS]


def build_bigram_query(query: str, stopwords: bool = False) -> str:
    terms = bigram_terms(query, stopwords)
    return _quote_terms(terms) if terms else ""


# ------------------------------------------------------------ vectors / conn

def serialize_vector(vec) -> bytes:
    values = [float(x) for x in vec]
    return struct.pack(f"{len(values)}f", *values)


def deserialize_vector(blob) -> list:
    return list(struct.unpack(f"{len(blob) // 4}f", bytes(blob)))


def _load_vec_extension(conn):
    """Load sqlite-vec. Loading twice is harmless."""
    try:
        import sqlite_vec
    except ImportError as e:
        raise RuntimeError(f"sqlite-vec is not installed. Recovery: pip install sqlite-vec ({e})")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        msg = str(e).lower()
        if "already" in msg or "duplicate" in msg:
            return
        raise RuntimeError(f"could not load sqlite-vec ({e}). Recovery: pip install sqlite-vec")


def get_connection(db_path=None):
    db_path = db_path or paths.DB_PATH
    folder = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
    except Exception:
        conn.close()
        raise
    try:
        _load_vec_extension(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone() is not None


_MEMORIES_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    local_id    TEXT,
    agent_name  TEXT,
    session_id  TEXT,
    turn_id     TEXT
)
"""


def migrate_schema(conn):
    """Idempotent: add missing columns / index, move FTS to the trigram tokenizer."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for col in ("local_id", "agent_name", "session_id", "turn_id"):
        if col not in cols:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id)")
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'memories_fts'").fetchone()
    if row and row[0] and "trigram" not in row[0].lower():
        conn.execute("DROP TABLE memories_fts")
        conn.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, tokenize='trigram')")
        for r in conn.execute("SELECT rowid, content FROM memories").fetchall():
            conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                         (r[0], strip_fts_punctuation(r[1])))
    conn.commit()


def init_db(conn):
    conn.execute(_MEMORIES_DDL)
    migrate_schema(conn)
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                 "USING fts5(content, tokenize='trigram')")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_bi "
                 "USING fts5(content, tokenize='unicode61')")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec "
                 f"USING vec0(embedding float[{EMBED_DIM}])")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()


# ----------------------------------------------------------------- meta table

def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    conn.commit()


def delete_meta(conn, *keys):
    for k in keys:
        conn.execute("DELETE FROM meta WHERE key = ?", (k,))
    conn.commit()


# ----------------------------------------------------------------------- CRUD

def insert_memory(conn, id, content, timestamp, owner_id, local_id=None, agent_name=None,
                  session_id=None, embedding=None, *, turn_id=None):
    """INSERT into memories + both FTS tables (+ vec when an embedding is given), then COMMIT."""
    try:
        cur = conn.execute(
            "INSERT INTO memories(id, content, timestamp, owner_id, local_id, agent_name, "
            "session_id, turn_id) VALUES (?,?,?,?,?,?,?,?)",
            (id, content, timestamp, owner_id, local_id, agent_name, session_id, turn_id),
        )
        rowid = cur.lastrowid
        conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                     (rowid, strip_fts_punctuation(content)))
        conn.execute("INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
                     (rowid, bigram_text(content)))
        if embedding is not None:
            conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                         (rowid, serialize_vector(embedding)))
        conn.commit()
        return rowid
    except Exception:
        conn.rollback()
        raise


def check_exact_duplicate(conn, content, turn_id=None):
    """Exact same text only. With a turn_id the comparison is within that turn."""
    if turn_id:
        row = conn.execute("SELECT 1 FROM memories WHERE content = ? AND turn_id = ? LIMIT 1",
                           (content, turn_id)).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM memories WHERE content = ? LIMIT 1",
                           (content,)).fetchone()
    return row is not None


def insert_memory_once(conn, id, content, timestamp, owner_id, local_id=None, agent_name=None,
                       session_id=None, embedding=None, *, turn_id=None):
    """Re-check for a duplicate inside a write lock (R04). None means 'duplicate, not stored'."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if check_exact_duplicate(conn, content, turn_id=turn_id):
            conn.rollback()
            return None
    except Exception:
        conn.rollback()
        raise
    return insert_memory(conn, id, content, timestamp, owner_id, local_id, agent_name,
                         session_id, embedding, turn_id=turn_id)


def count_memories(conn):
    return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def _time_where(since, until, column="timestamp"):
    parts, params = [], []
    if since:
        parts.append(f"{column} >= ?")
        params.append(since)
    if until:
        parts.append(f"{column} <= ?")
        params.append(until)
    return " AND ".join(parts), params


def get_all_memories(conn, since=None, until=None):
    where, params = _time_where(since, until)
    sql = "SELECT rowid AS rowid, * FROM memories"
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY rowid"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_memories_by_turn_id(conn, turn_id):
    """All rows of one Q-A exchange: [user] first, then [claude]; rowid order inside a role."""
    rows = [dict(r) for r in conn.execute(
        "SELECT rowid AS rowid, * FROM memories WHERE turn_id = ? ORDER BY rowid", (turn_id,)
    ).fetchall()]
    rows.sort(key=lambda r: (0 if r["content"].startswith("[user") else 1, r["rowid"]))
    return rows


def delete_memory(conn, memory_id):
    _load_vec_extension(conn)
    row = conn.execute("SELECT rowid FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if not row:
        return False
    rowid = row[0]
    try:
        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories_fts_bi WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM memories WHERE rowid = ?", (rowid,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def find_unindexed_memories(conn, limit=200):
    """Rows missing from memories_vec or memories_fts (double LEFT JOIN)."""
    rows = conn.execute(
        "SELECT m.rowid AS rowid, m.id, m.content, m.timestamp, m.owner_id, m.turn_id, "
        "       (mv.rowid IS NOT NULL) AS has_vec, (mf.rowid IS NOT NULL) AS has_fts "
        "FROM memories m "
        "LEFT JOIN memories_vec mv ON mv.rowid = m.rowid "
        "LEFT JOIN memories_fts mf ON mf.rowid = m.rowid "
        "WHERE mv.rowid IS NULL OR mf.rowid IS NULL "
        "ORDER BY m.rowid LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------- search

def _rows_by_rowid(conn, rowids):
    out = {}
    rowids = list(rowids)
    for i in range(0, len(rowids), 500):
        chunk = rowids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT rowid AS rowid, * FROM memories WHERE rowid IN ({q})", chunk):
            out[r["rowid"]] = dict(r)
    return out


def _search_vector_exact(conn, vec, k, since=None, until=None):
    """Brute force over the filtered rows (used when rowid IN (...) is unsupported)."""
    where, params = _time_where(since, until, "m.timestamp")
    sql = ("SELECT m.rowid AS rowid, vec_distance_l2(v.embedding, ?) AS distance "
           "FROM memories_vec v JOIN memories m ON m.rowid = v.rowid")
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY distance LIMIT ?"
    return conn.execute(sql, [serialize_vector(vec), *params, k]).fetchall()


def search_vector(conn, vec, k=60, since=None, until=None):
    """KNN. With a time range the KNN runs over the filtered rowids only (two steps)."""
    blob = serialize_vector(vec)
    where, params = _time_where(since, until)
    try:
        if where:
            hits = conn.execute(
                "SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k = ? "
                f"AND rowid IN (SELECT rowid FROM memories WHERE {where})",
                [blob, k, *params]).fetchall()
        else:
            hits = conn.execute(
                "SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k = ?",
                [blob, k]).fetchall()
    except sqlite3.OperationalError:
        if not where:
            raise
        hits = _search_vector_exact(conn, vec, k, since, until)
    mem = _rows_by_rowid(conn, [h[0] for h in hits])
    out = []
    for h in sorted(hits, key=lambda h: h[1]):
        m = mem.get(h[0])
        if m:
            m["distance"] = float(h[1])
            out.append(m)
    return out


def _search_fts_table(conn, table, match, limit, since, until):
    if not match:
        return []
    where, params = _time_where(since, until, "m.timestamp")
    sql = (f"SELECT m.rowid AS rowid, bm25({table}) AS bm25 FROM {table} "
           f"JOIN memories m ON m.rowid = {table}.rowid WHERE {table} MATCH ?")
    args = [match]
    if where:
        sql += " AND " + where
        args += params
    sql += " ORDER BY bm25 LIMIT ?"
    args.append(limit)
    hits = conn.execute(sql, args).fetchall()
    mem = _rows_by_rowid(conn, [h[0] for h in hits])
    out = []
    for h in hits:
        m = mem.get(h[0])
        if m:
            m["bm25"] = float(h[1])
            out.append(m)
    return out


def search_fts(conn, query, limit=60, since=None, until=None, stopwords=False):
    return _search_fts_table(conn, "memories_fts", _build_fts_query(query, stopwords), limit,
                             since, until)


def search_fts_bigram(conn, query, limit=60, since=None, until=None, stopwords=False):
    return _search_fts_table(conn, "memories_fts_bi", build_bigram_query(query, stopwords), limit,
                             since, until)
