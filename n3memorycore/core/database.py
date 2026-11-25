"""DB layer: schema, CRUD, PRAGMA settings and migrations (SQLite + FTS5 + sqlite-vec)."""
import re
import sqlite3
import struct

BUSY_TIMEOUT_SECONDS = 10
EMBED_DIM = 768
FTS_TEXT_RULES = "2"

# ---------------------------------------------------------------------------
# FTS text rules
# ---------------------------------------------------------------------------

_FTS_PUNCT_RE = re.compile(r'[「」『』【】（）()\[\]{}<>〈〉《》・、。,.!！?？;；:：\-―─…\'\"“”‘’]')


def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text or '')
    return re.sub(r'\s+', ' ', cleaned).strip()


# N3MC's own role prefix (once, at the start) and the `[code omitted]` placeholder are not words of the record
_FTS_ROLE_PREFIX_RE = re.compile(r'^\[(?:user|claude)(?: \d+/\d+)?\] ?')


def fts_index_text(content: str) -> str:
    return _FTS_ROLE_PREFIX_RE.sub('', content or '', count=1).replace('[code omitted]', ' ')


_FTS_MAX_TERMS = 40  # keeps FTS5 from freezing on huge inputs
_FTS_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
_CJK_CHAR_RE = re.compile(r'[぀-ヿ㐀-鿿豈-﫿가-힯]')
_CJK_RUN_RE = re.compile(r'[぀-ヿ㐀-鿿豈-﫿가-힯]+')
_HIRAGANA_ONLY_RE = re.compile(r'^[぀-ゟー]+$')


# English function words (articles, pronouns, auxiliaries, question words, ...). They are in most
# English records, so a question made of them matched almost everything ("Who is the CEO of the
# company?" filled Top matches with unrelated rows through `the` / `of` / `is`). Dropped from the
# keyword terms when config "fts_stopwords" is true (default; not in the specification).
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
    return kept or terms  # a question made only of function words is searched as typed


def fts_terms(query: str, stopwords: bool = False) -> list:
    """The trigram search terms of the question (in order, de-duplicated, at most 40)."""
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


def _bigram_tokens(text: str) -> list:
    """Split CJK runs into overlapping 2-character grams; keep everything else as it is."""
    out = []
    pos = 0
    for m in _CJK_RUN_RE.finditer(text):
        if m.start() > pos:
            out.append(text[pos:m.start()])
        run = m.group(0)
        if len(run) == 1:
            out.append(run)
        else:
            out.append(" ".join(run[i:i + 2] for i in range(len(run) - 1)))
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:])
    return out


def bigram_text(text: str) -> str:
    """`N3MCの設定` -> `N3MC の設 設定` (after strip_fts_punctuation)."""
    cleaned = strip_fts_punctuation(text)
    joined = " ".join(_bigram_tokens(cleaned))
    return re.sub(r'\s+', ' ', joined).strip()


def bigram_terms(query: str, stopwords: bool = False) -> list:
    """The bigram-index search terms of the question (in order, de-duplicated, at most 40)."""
    cleaned = _FTS_PUNCT_RE.sub(' ', _FTS_CTRL_RE.sub(' ', query or ''))
    words = bigram_text(cleaned).split()
    seen, terms = set(), []
    for w in words:
        if len(w) == 1 and not _CJK_CHAR_RE.search(w):
            continue  # a 1-character non-CJK word
        if w not in seen:
            seen.add(w)
            terms.append(w)
    non_hira = [t for t in terms if not _HIRAGANA_ONLY_RE.match(t)]
    if non_hira:
        terms = non_hira
    if stopwords:
        terms = _drop_stopwords(terms)
    return terms[:_FTS_MAX_TERMS]


def build_bigram_query(query: str, stopwords: bool = False) -> str:
    """The bigram words of the question, quoted and joined with OR ('' = no search)."""
    terms = bigram_terms(query, stopwords)
    if not terms:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _load_vec_extension(conn):
    """Load sqlite-vec into the connection. Loading it twice is harmless."""
    try:
        import sqlite_vec
    except ImportError as e:
        raise RuntimeError(f"sqlite-vec is not installed ({e}). Recovery: pip install sqlite-vec") from e
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "already" in msg or "duplicate" in msg:
            return
        raise RuntimeError(f"sqlite-vec could not be loaded ({type(e).__name__}: {e}). "
                           "Recovery: pip install sqlite-vec") from e


def get_connection(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        _load_vec_extension(conn)
    except Exception:
        conn.close()
        raise
    try:
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
    except Exception:
        conn.close()  # close before re-raising so that the file can be moved (Windows)
        raise
    return conn


def _table_exists(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id          TEXT PRIMARY KEY,
            content     TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            owner_id    TEXT NOT NULL,
            local_id    TEXT,
            agent_name  TEXT,
            session_id  TEXT,
            turn_id     TEXT
        )""")
    migrate_schema(conn)
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, tokenize='trigram')")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_bi USING fts5(content, tokenize='unicode61')")
    if not _table_exists(conn, "memories_vec"):
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(embedding float[{EMBED_DIM}])")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()


def migrate_schema(conn):
    """Add missing columns idempotently and move a porter-tokenized FTS table to trigram."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for col in ("local_id", "agent_name", "session_id", "turn_id"):
        if col not in cols:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id)")
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'memories_fts'").fetchone()
    if row and row[0] and "trigram" not in row[0].lower():
        conn.execute("DROP TABLE memories_fts")
        conn.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, tokenize='trigram')")
        for rowid, content in conn.execute("SELECT rowid, content FROM memories ORDER BY rowid").fetchall():
            conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                         (rowid, strip_fts_punctuation(fts_index_text(content))))
    conn.commit()


# ---------------------------------------------------------------------------
# meta table
# ---------------------------------------------------------------------------

def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key, value, commit=True):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))
    if commit:
        conn.commit()


def delete_meta(conn, key, commit=True):
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    if commit:
        conn.commit()


def vec_table_dim(conn):
    """The dimension of memories_vec, read from the table itself (not EMBED_DIM)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'memories_vec'").fetchone()
    if not row or not row[0]:
        return None
    m = re.search(r'float\[(\d+)\]', row[0])
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------

def serialize_vector(vec) -> bytes:
    vals = [float(x) for x in vec]
    return struct.pack(f"{len(vals)}f", *vals)


def deserialize_vector(blob) -> list:
    if blob is None:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def get_vector(conn, rowid):
    row = conn.execute("SELECT embedding FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone()
    return deserialize_vector(row[0]) if row else None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def insert_memory(conn, id, content, timestamp, owner_id, local_id=None, agent_name=None,
                  session_id=None, embedding=None, *, turn_id=None):
    """Insert one record into memories, both FTS tables and (with an embedding) memories_vec,
    and commit at once. Returns the rowid."""
    try:
        cur = conn.execute(
            "INSERT INTO memories (id, content, timestamp, owner_id, local_id, agent_name, session_id, turn_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (id, content, timestamp, owner_id, local_id, agent_name, session_id, turn_id))
        rowid = cur.lastrowid
        idx_text = fts_index_text(content)
        conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                     (rowid, strip_fts_punctuation(idx_text)))
        conn.execute("INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
                     (rowid, bigram_text(idx_text)))
        if embedding is not None:
            conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                         (rowid, serialize_vector(embedding)))
        conn.commit()
        return rowid
    except Exception:
        conn.rollback()
        raise


def insert_memory_once(conn, id, content, timestamp, owner_id, turn_id=None, **kwargs):
    """Insert unless the exact same content already exists (within turn_id when given).
    The check is repeated inside BEGIN IMMEDIATE so that two simultaneous saves write one row.
    Returns the rowid, or None for a duplicate."""
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
    return insert_memory(conn, id, content, timestamp, owner_id, turn_id=turn_id, **kwargs)


def delete_memory(conn, memory_id) -> bool:
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


def count_memories(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def _time_where(since, until, alias="m"):
    clauses, params = [], []
    if since:
        clauses.append(f"{alias}.timestamp >= ?")
        params.append(since)
    if until:
        clauses.append(f"{alias}.timestamp <= ?")
        params.append(until)
    return clauses, params


_ROW_COLS = "m.rowid, m.id, m.content, m.timestamp, m.owner_id, m.local_id, m.agent_name, m.session_id, m.turn_id"
_ROW_KEYS = ("rowid", "id", "content", "timestamp", "owner_id", "local_id", "agent_name", "session_id", "turn_id")


def _row_dict(row):
    return dict(zip(_ROW_KEYS, row))


def get_all_memories(conn, since=None, until=None) -> list:
    clauses, params = _time_where(since, until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"SELECT {_ROW_COLS} FROM memories m{where} ORDER BY m.rowid", params).fetchall()
    return [_row_dict(r) for r in rows]


def check_exact_duplicate(conn, content, turn_id=None) -> bool:
    if turn_id is not None:
        row = conn.execute("SELECT 1 FROM memories WHERE content = ? AND turn_id = ? LIMIT 1",
                           (content, turn_id)).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM memories WHERE content = ? LIMIT 1", (content,)).fetchone()
    return row is not None


def find_unindexed_memories(conn, limit=200) -> list:
    """Records missing from memories_vec or memories_fts (NOT IN sub-queries, never a LEFT JOIN
    with vec0). OFFSET is always 0: repaired rows leave the result set."""
    rows = conn.execute(
        "SELECT m.rowid, m.id, m.content, "
        "       m.rowid IN (SELECT rowid FROM memories_vec) AS has_vec, "
        "       m.rowid IN (SELECT rowid FROM memories_fts) AS has_fts "
        "FROM memories m "
        "WHERE m.rowid NOT IN (SELECT rowid FROM memories_vec) "
        "   OR m.rowid NOT IN (SELECT rowid FROM memories_fts) "
        "ORDER BY m.rowid LIMIT ? OFFSET 0", (limit,)).fetchall()
    return [{"rowid": r[0], "id": r[1], "content": r[2], "has_vec": bool(r[3]), "has_fts": bool(r[4])}
            for r in rows]


def search_vector(conn, query_vec, k, since=None, until=None) -> list:
    """KNN over memories_vec. With since/until the KNN runs over the filtered rowids only."""
    blob = serialize_vector(query_vec)
    if since or until:
        clauses, params = _time_where(since, until, alias="mm")
        try:
            hits = conn.execute(
                "SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k = ? "
                f"AND rowid IN (SELECT mm.rowid FROM memories mm WHERE {' AND '.join(clauses)})",
                [blob, k, *params]).fetchall()
        except sqlite3.OperationalError:
            return _search_vector_exact(conn, query_vec, k, since, until)
    else:
        hits = conn.execute("SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k = ?",
                            (blob, k)).fetchall()
    return _attach_rows(conn, hits)


def _search_vector_exact(conn, query_vec, k, since=None, until=None) -> list:
    blob = serialize_vector(query_vec)
    clauses, params = _time_where(since, until)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    hits = conn.execute(
        "SELECT v.rowid, vec_distance_l2(v.embedding, ?) AS d FROM memories_vec v "
        f"JOIN memories m ON m.rowid = v.rowid WHERE 1=1{where} ORDER BY d LIMIT ?",
        [blob, *params, k]).fetchall()
    return _attach_rows(conn, hits)


def _attach_rows(conn, hits):
    out = []
    for rowid, distance in hits:
        row = conn.execute(f"SELECT {_ROW_COLS} FROM memories m WHERE m.rowid = ?", (rowid,)).fetchone()
        if row:
            d = _row_dict(row)
            d["distance"] = float(distance)
            out.append(d)
    return out


def _search_fts_table(conn, table, match, limit, since, until):
    if not match:
        return []
    clauses, params = _time_where(since, until)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT {_ROW_COLS}, bm25({table}) AS score FROM {table} f JOIN memories m ON m.rowid = f.rowid "
        f"WHERE {table} MATCH ?{where} ORDER BY score LIMIT ?",
        [match, *params, limit]).fetchall()
    out = []
    for r in rows:
        d = _row_dict(r[:-1])
        d["bm25"] = float(r[-1])
        out.append(d)
    return out


def search_fts(conn, query, limit=60, since=None, until=None, stopwords=False) -> list:
    """Trigram FTS search; words are quoted and joined with OR."""
    return _search_fts_table(conn, "memories_fts", _build_fts_query(query, stopwords), limit, since, until)


def search_fts_bigram(conn, query, limit=60, since=None, until=None, stopwords=False) -> list:
    """Search of the CJK bigram index."""
    return _search_fts_table(conn, "memories_fts_bi", build_bigram_query(query, stopwords), limit, since,
                             until)


def get_memories_by_turn_id(conn, turn_id) -> list:
    """All rows of one turn: [user] rows first, then [claude] rows, each in rowid order."""
    rows = conn.execute(f"SELECT {_ROW_COLS} FROM memories m WHERE m.turn_id = ? ORDER BY m.rowid",
                        (turn_id,)).fetchall()
    out = [_row_dict(r) for r in rows]

    def role(d):
        c = d["content"]
        if c.startswith("[user"):
            return 0
        if c.startswith("[claude"):
            return 1
        return 2
    out.sort(key=lambda d: (role(d), d["rowid"]))
    return out
