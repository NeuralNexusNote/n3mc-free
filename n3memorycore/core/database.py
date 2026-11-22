"""DB layer: schema, CRUD, PRAGMAs, migrations (SQLite + sqlite-vec + FTS5)."""
import re
import sqlite3
import struct
from datetime import datetime, timezone

from ..paths import DB_PATH

EMBED_DIM = 768
BUSY_TIMEOUT_SECONDS = 10
_FTS_MAX_TERMS = 40  # guards FTS5 against huge inputs

_FTS_PUNCT_RE = re.compile(r'[「」『』【】（）()\[\]{}<>〈〉《》・、。,.!！?？;；:：\-―─…\'\"“”‘’]')
_FTS_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
_CJK_CHAR_RE = re.compile(r'[぀-ヿ㐀-鿿豈-﫿가-힯]')
_CJK_RUN_RE = re.compile(r'([぀-ヿ㐀-鿿豈-﫿가-힯]+)')
_HIRAGANA_ONLY_RE = re.compile(r'^[぀-ゟー]+$')


# English function words (articles, pronouns, auxiliaries, question words, ...). They are in most
# English records, so a query made of them matched almost everything: "Who is the CEO of the
# company?" filled Top matches with 20 unrelated rows through `the` / `of` / `is`. Dropped from the
# keyword terms when config "fts_stopwords" is true (the default since 2026-11-22; not in the spec).
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


def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


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
    terms = fts_terms(query, stopwords)
    if not terms:
        return ""  # -> skip the trigram search
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def bigram_text(text: str) -> str:
    """Text for memories_fts_bi: punctuation removed, CJK runs split into overlapping 2-char terms."""
    cleaned = strip_fts_punctuation(text or "")
    out = []
    for part in _CJK_RUN_RE.split(cleaned):
        if not part:
            continue
        if _CJK_CHAR_RE.match(part[0]):
            if len(part) == 1:
                out.append(part)
            else:
                out.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            out.extend(part.split())
    return " ".join(out)


def bigram_terms(query: str, stopwords: bool = False) -> list:
    """The bigram-index search terms of ``query`` (in order, de-duplicated, at most 40)."""
    cleaned = _FTS_PUNCT_RE.sub(' ', _FTS_CTRL_RE.sub(' ', query or ''))
    terms, seen = [], set()
    for word in cleaned.split():
        if _CJK_CHAR_RE.search(word):
            cand = bigram_text(word).split()
        else:
            cand = [word]
        for t in cand:
            if not _CJK_CHAR_RE.search(t) and len(t) < 2:
                continue  # a lone non-CJK character
            if t not in seen:
                seen.add(t)
                terms.append(t)
    non_hira = [t for t in terms if not _HIRAGANA_ONLY_RE.match(t)]
    if non_hira:
        terms = non_hira
    if stopwords:
        terms = _drop_stopwords(terms)
    return terms[:_FTS_MAX_TERMS]


def build_bigram_query(query: str, stopwords: bool = False) -> str:
    terms = bigram_terms(query, stopwords)
    if not terms:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def serialize_vector(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def deserialize_vector(blob) -> list:
    return list(struct.unpack(f"{len(blob) // 4}f", bytes(blob)))


def _load_vec_extension(conn):
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "already" in msg or "duplicate" in msg:
            return
        raise RuntimeError(f"sqlite-vec could not be loaded ({e}). Recovery: pip install sqlite-vec") from e
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:  # noqa: BLE001
            pass


def get_connection(db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        _load_vec_extension(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def init_db(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            local_id TEXT,
            agent_name TEXT,
            session_id TEXT,
            turn_id TEXT
        )"""
    )
    migrate_schema(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, tokenize='trigram')")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_bi USING fts5(content, tokenize='unicode61')")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(embedding float[{EMBED_DIM}])")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()


def migrate_schema(conn):
    """Idempotently add missing columns; rebuild a legacy porter FTS table as trigram."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for col in ("local_id", "agent_name", "session_id", "turn_id"):
        if col not in cols:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id)")
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='memories_fts'").fetchone()
    if row and row[0] and "trigram" not in row[0].lower():
        conn.execute("DROP TABLE memories_fts")
        conn.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, tokenize='trigram')")
        for r in conn.execute("SELECT rowid, content FROM memories").fetchall():
            conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                         (r[0], strip_fts_punctuation(r[1])))
    conn.commit()


# ---- meta ---------------------------------------------------------------

def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def delete_meta(conn, key):
    conn.execute("DELETE FROM meta WHERE key=?", (key,))


_VEC_DIM_RE = re.compile(r"float\[\s*(\d+)\s*\]", re.IGNORECASE)


def vec_dim(conn):
    """Dimension of memories_vec, read from the table definition (the N of float[N]); None if unknown."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='memories_vec'").fetchone()
    m = _VEC_DIM_RE.search(row[0] or "") if row else None
    return int(m.group(1)) if m else None


# ---- writes ---------------------------------------------------------------

def insert_memory(conn, id, content, timestamp, owner_id, local_id=None, agent_name=None,
                  session_id=None, *, turn_id=None, embedding=None):
    """INSERT + COMMIT immediately (Immutable Memory). Returns the memories rowid."""
    try:
        cur = conn.execute(
            "INSERT INTO memories(id, content, timestamp, owner_id, local_id, agent_name, session_id, turn_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
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
    except BaseException:
        conn.rollback()
        raise


def insert_memory_once(conn, id, content, timestamp, owner_id, local_id=None, agent_name=None,
                       session_id=None, *, turn_id=None, embedding=None):
    """insert_memory, but re-check for an exact duplicate inside a write lock (R04). None if duplicate."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if check_exact_duplicate(conn, content, turn_id=turn_id):
            conn.rollback()
            return None
    except BaseException:
        conn.rollback()
        raise
    return insert_memory(conn, id, content, timestamp, owner_id, local_id, agent_name, session_id,
                         turn_id=turn_id, embedding=embedding)


def delete_memory(conn, memory_id):
    row = conn.execute("SELECT rowid FROM memories WHERE id=?", (memory_id,)).fetchone()
    if not row:
        return False
    rowid = row[0]
    _load_vec_extension(conn)
    try:
        conn.execute("DELETE FROM memories_fts WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM memories_fts_bi WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM memories_vec WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM memories WHERE rowid=?", (rowid,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return True


# ---- reads ----------------------------------------------------------------

def check_exact_duplicate(conn, content, turn_id=None):
    if turn_id:
        row = conn.execute("SELECT 1 FROM memories WHERE content=? AND turn_id=? LIMIT 1",
                           (content, turn_id)).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM memories WHERE content=? LIMIT 1", (content,)).fetchone()
    return row is not None


def count_memories(conn):
    return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def get_all_memories(conn, since=None, until=None):
    sql, args = "SELECT rowid, * FROM memories", []
    where = []
    if since:
        where.append("timestamp >= ?")
        args.append(since)
    if until:
        where.append("timestamp <= ?")
        args.append(until)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY rowid"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_memories_by_turn_id(conn, turn_id):
    rows = conn.execute("SELECT rowid, * FROM memories WHERE turn_id=? ORDER BY rowid", (turn_id,)).fetchall()
    return [dict(r) for r in rows]


def find_unindexed_memories(conn, limit=200):
    """Rows missing from memories_vec or memories_fts (both indexes checked). OFFSET is always 0.

    The same result as a double LEFT JOIN, written as NOT IN over each index's rowids: a LEFT JOIN
    looks up memories_vec (vec0) once per memories row, about 0.5 ms each on a file DB, so the check
    grew with the DB (about 0.5 s at 1,600 records, 10 s at 20,000) and ran before every prompt
    (--hook-submit calls /repair). Each NOT IN subquery is read once."""
    rows = conn.execute(
        "SELECT rowid, id, content, no_vec, no_fts FROM ("
        "SELECT m.rowid AS rowid, m.id AS id, m.content AS content, "
        "(m.rowid NOT IN (SELECT rowid FROM memories_vec)) AS no_vec, "
        "(m.rowid NOT IN (SELECT rowid FROM memories_fts)) AS no_fts "
        "FROM memories m) WHERE no_vec OR no_fts ORDER BY rowid LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _time_clause(since, until, col="timestamp"):
    where, args = [], []
    if since:
        where.append(f"{col} >= ?")
        args.append(since)
    if until:
        where.append(f"{col} <= ?")
        args.append(until)
    return where, args


def _rows_by_rowid(conn, rowids):
    if not rowids:
        return {}
    out = {}
    ids = list(rowids)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT rowid AS rowid, * FROM memories WHERE rowid IN ({q})", part).fetchall():
            out[r["rowid"]] = dict(r)
    return out


def search_vector(conn, query_vec, k=60, since=None, until=None):
    """KNN over memories_vec. Returns [(memory dict, L2 distance)]. With a time range, KNN runs only over
    the filtered rowids (two steps); falls back to an exact scan if `rowid IN (subquery)` is unsupported."""
    blob = serialize_vector(query_vec)
    where, targs = _time_clause(since, until)
    if where:
        try:
            sub = "SELECT rowid FROM memories WHERE " + " AND ".join(where)
            rows = conn.execute(
                "SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k = ? "
                f"AND rowid IN ({sub})", [blob, k] + targs).fetchall()
        except sqlite3.OperationalError:
            return _search_vector_exact(conn, query_vec, k, since, until)
    else:
        rows = conn.execute("SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k = ?",
                            (blob, k)).fetchall()
    mem = _rows_by_rowid(conn, [r[0] for r in rows])
    return [(mem[r[0]], r[1]) for r in rows if r[0] in mem]


def _search_vector_exact(conn, query_vec, k=60, since=None, until=None):
    blob = serialize_vector(query_vec)
    where, targs = _time_clause(since, until, "m.timestamp")
    sql = ("SELECT mv.rowid AS rid, vec_distance_l2(mv.embedding, ?) AS d FROM memories_vec mv "
           "JOIN memories m ON m.rowid = mv.rowid")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d LIMIT ?"
    rows = conn.execute(sql, [blob] + targs + [k]).fetchall()
    mem = _rows_by_rowid(conn, [r[0] for r in rows])
    return [(mem[r[0]], r[1]) for r in rows if r[0] in mem]


def _search_fts_table(conn, table, match, limit, since, until):
    where, targs = _time_clause(since, until, "m.timestamp")
    sql = (f"SELECT f.rowid AS rid, f.s AS s FROM (SELECT rowid, bm25({table}) AS s FROM {table} "
           f"WHERE {table} MATCH ?) f JOIN memories m ON m.rowid = f.rowid")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY f.s LIMIT ?"
    rows = conn.execute(sql, [match] + targs + [limit]).fetchall()
    mem = _rows_by_rowid(conn, [r[0] for r in rows])
    return [(mem[r[0]], r[1]) for r in rows if r[0] in mem]


def search_fts(conn, query, limit=60, since=None, until=None, stopwords=False):
    """Trigram keyword search. Returns [(memory dict, raw bm25 (negative = better))]."""
    match = _build_fts_query(query, stopwords)
    if not match:
        return []
    return _search_fts_table(conn, "memories_fts", match, limit, since, until)


def search_fts_bigram(conn, query, limit=60, since=None, until=None, stopwords=False):
    match = build_bigram_query(query, stopwords)
    if not match:
        return []
    return _search_fts_table(conn, "memories_fts_bi", match, limit, since, until)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()
