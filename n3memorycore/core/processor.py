"""Processing layer: embeddings, ranking, chunking, purify and rendering of the recalled context."""
import math
import re
import sys
import time
from datetime import datetime, timezone

from .database import (
    get_connection,
    init_db,
    insert_memory,
    search_vector,
    search_fts,
    search_fts_bigram,
    get_all_memories,
    delete_memory,
    count_memories,
    check_exact_duplicate,
    find_unindexed_memories,
    serialize_vector,        # used by /repair
    get_memories_by_turn_id, # used by Q-A pairing
    strip_fts_punctuation,   # used by the /repair FTS migration
    get_vector,
    fts_terms,
    bigram_terms,
    bigram_text,
)

DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-base"
MODEL_RETRY_SECONDS = 60

W_COS = 0.7
W_KEYWORD = 0.3
B_SESSION_MATCH = 1.0
B_SESSION_MISMATCH = 0.6
WEAK_MIN_FILTER_SCORE = 1e-6

# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------

_model = None
_model_name = None
_configured_model = DEFAULT_EMBED_MODEL
_model_failed_at = None
_model_failed_name = None
_model_last_error = None


def set_embed_model(name):
    """Set the model named by config.json (embed_model). There is no environment override."""
    global _configured_model
    _configured_model = name or DEFAULT_EMBED_MODEL


def _load_model(name):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name)


def get_model(name=None):
    """Load the embedding model once. After a failure, raise at once for MODEL_RETRY_SECONDS."""
    global _model, _model_name, _model_failed_at, _model_failed_name, _model_last_error
    name = name or _configured_model
    if _model is not None and _model_name == name:
        return _model
    if (_model_failed_at is not None and _model_failed_name == name
            and time.monotonic() - _model_failed_at < MODEL_RETRY_SECONDS):
        raise RuntimeError(f"embedding model unavailable (retry later): {_model_last_error}")
    try:
        model = _load_model(name)
    except Exception as e:  # noqa: BLE001
        _model_failed_at = time.monotonic()
        _model_failed_name = name
        _model_last_error = f"{type(e).__name__}: {e}"
        print(f"N3MC: the embedding model {name} could not be loaded ({_model_last_error})", file=sys.stderr)
        raise
    _model, _model_name = model, name
    _model_failed_at = None
    return model


def _encode(text, name=None):
    vec = get_model(name).encode(text, normalize_embeddings=True)
    return [float(x) for x in vec]


def embed_passage(content, name=None):
    return _encode("passage: " + content, name)


def embed_query(keyword, name=None):
    return _encode("query: " + keyword, name)


# ---------------------------------------------------------------------------
# Ranking math
# ---------------------------------------------------------------------------

def cosine_sim_from_l2(l2_distance) -> float:
    return max(0.0, 1.0 - (l2_distance ** 2) / 2.0)


def _parse_ts(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_time_decay(timestamp, half_life_days=90, now=None) -> float:
    dt = _parse_ts(timestamp)
    if dt is None:
        return 1.0
    now = now or datetime.now(timezone.utc)
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return 2.0 ** (-days / float(half_life_days))


def keyword_relevance(bm25_score, max_abs, threshold=0.1, small_db=False) -> float:
    """Normalize a raw FTS5 bm25() value (negative = better) into [0, 1]."""
    raw = -float(bm25_score)
    if small_db:
        if max_abs <= 0:
            return 0.0
        return max(0.0, min(1.0, raw / max_abs))
    if abs(bm25_score) < threshold:
        return 0.0
    return max(0.0, min(1.0, raw / max(1.0, max_abs)))


def b_session(record_session, current_session) -> float:
    if current_session is not None and record_session == current_session:
        return B_SESSION_MATCH
    return B_SESSION_MISMATCH


def combined_score(cos_sim, kw, decay, bias):
    filter_score = cos_sim * W_COS + kw * W_KEYWORD
    return filter_score * decay * bias, filter_score


# ---------------------------------------------------------------------------
# Text: surrogates, purify, chunking
# ---------------------------------------------------------------------------

def _sanitize_str(s: str) -> str:
    if not any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        o = ord(c)
        if 0xD800 <= o <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(s[i + 1]) <= 0xDFFF:
            out.append(chr(0x10000 + ((o - 0xD800) << 10) + (ord(s[i + 1]) - 0xDC00)))
            i += 2
            continue
        if 0xD800 <= o <= 0xDFFF:
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def sanitize_surrogates(obj):
    """Remove lone surrogates from strings, recursively through dicts and lists."""
    if obj is None:
        return None
    if isinstance(obj, str):
        return _sanitize_str(obj)
    if isinstance(obj, dict):
        return {sanitize_surrogates(k): sanitize_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_surrogates(v) for v in obj]
    return obj


# Fences are whole lines. Opening: up to 3 spaces, 3+ backticks, an optional info string without
# backticks. Closing: only backticks, at least as many as the opening (spaces around allowed).
_FENCE_OPEN_RE = re.compile(r" {0,3}(`{3,})[^`]*")
_FENCE_CLOSE_RE = re.compile(r"[ \t]*(`{3,})[ \t\r]*")
_CODE_OMITTED = "[code omitted]"


def _replace_code_blocks(text):
    """Replace each closed fenced block (opening line .. first closing line) with [code omitted].
    A ``` in the middle of a line, or a shorter ``` line inside code, is not a fence; an opening
    line that is never closed is left as it is and the search goes on from the next line."""
    lines = text.split("\n")
    n = len(lines)
    close_width = [0] * n
    for k, line in enumerate(lines):
        m = _FENCE_CLOSE_RE.fullmatch(line)
        if m:
            close_width[k] = len(m.group(1))
    widest_after = [0] * (n + 1)  # widest closing fence at line k or later
    for k in range(n - 1, -1, -1):
        widest_after[k] = max(close_width[k], widest_after[k + 1])
    out = []
    i = 0
    while i < n:
        m = _FENCE_OPEN_RE.fullmatch(lines[i])
        if m:
            width = len(m.group(1))
            if widest_after[i + 1] >= width:
                j = i + 1
                while close_width[j] < width:
                    j += 1
                out.append(_CODE_OMITTED)
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def purify_text(text) -> str:
    if not text:
        return "" if text is None else sanitize_surrogates(text)
    return _replace_code_blocks(sanitize_surrogates(text))


_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+|(?<=[。！？])')


def _split_sentences(paragraph):
    return [s.strip() for s in _SENTENCE_RE.split(paragraph) if s and s.strip()]


def _hard_windows(text, max_chars, overlap):
    if len(text) <= max_chars:
        return [text]
    step = max(1, max_chars - overlap)
    out = []
    i = 0
    while i < len(text):
        out.append(text[i:i + max_chars])
        if i + max_chars >= len(text):
            break
        i += step
    return out


def chunk_text(text, max_chars=400, overlap=40) -> list:
    """Split text into overlapping chunks: paragraph -> sentence -> hard window.
    No chunk is longer than max_chars; every character of the text is kept in order."""
    if text is None:
        return []
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    # pieces: (separator before the piece, piece, is_window)
    pieces = []
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    for pi, para in enumerate(paragraphs):
        psep = "\n\n" if pi > 0 else ""
        if len(para) <= max_chars:
            pieces.append((psep, para, False))
            continue
        for si, sent in enumerate(_split_sentences(para)):
            ssep = psep if si == 0 else " "
            if len(sent) <= max_chars:
                pieces.append((ssep, sent, False))
            else:
                for wi, win in enumerate(_hard_windows(sent, max_chars, overlap)):
                    pieces.append((ssep if wi == 0 else "", win, True))
    chunks, cur = [], ""
    for sep, piece, is_window in pieces:
        if not cur:
            cur = piece
            continue
        if not is_window and len(cur) + len(sep) + len(piece) <= max_chars:
            cur = cur + sep + piece
            continue
        chunks.append(cur)
        if is_window:
            cur = piece  # hard windows already overlap each other
            continue
        sep2 = sep if sep else " "
        room = max_chars - len(piece) - len(sep2)
        tail_len = min(overlap, room, len(cur))
        if tail_len > 0:
            cur = cur[-tail_len:] + sep2 + piece
        else:
            cur = piece
    if cur:
        chunks.append(cur)
    return chunks


def add_chunk_prefixes(chunks, role) -> list:
    if len(chunks) == 1:
        return [f"[{role}] {chunks[0]}"]
    n = len(chunks)
    return [f"[{role} {i}/{n}] {c}" for i, c in enumerate(chunks, 1)]


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "half_life_days": 90, "bm25_min_threshold": 0.1, "search_result_limit": 20, "min_score": 0.2,
    "vector_min_cos": 0.82, "relative_min_score_ratio": 0.6, "min_results": 3, "pair_top_n": 3,
    "pair_max_chars": 3000, "version_order_enabled": True, "version_sim_threshold": 0.98,
    "version_min_chars": 100, "reply_sim_rerank_enabled": False, "reply_sim_alpha": 0.3,
    "reply_sim_floor": 0.80, "reply_sim_span": 0.15,
    # Search improvements (CPU only, not in the specification). The specification's behaviour comes
    # back with n3memory.SPEC_SEARCH_SETTINGS (vector_min_cos 0.85, fts_stopwords false, 0 for the rest).
    "fts_stopwords": True, "kw_only_min_terms": 3, "top_matches_max": 5, "cross_lang_min_cos": 0.80,
    "semantic_rescue_min_cos": 0.80, "semantic_rescue_margin": 0.02,
}

def _cfg(cfg, key):
    if cfg and key in cfg:
        return cfg[key]
    return _DEFAULTS[key]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cos(a, b):
    na = math.sqrt(_dot(a, a))
    nb = math.sqrt(_dot(b, b))
    if na == 0 or nb == 0:
        return 0.0
    return _dot(a, b) / (na * nb)


def _matched_terms(content, terms):
    """How many distinct question terms (trigram terms, bigram terms) occur in the record."""
    tri, bi = terms
    low = strip_fts_punctuation(content).lower()
    toks = set(bigram_text(content).lower().split())
    hit = {t.lower() for t in tri if t.lower() in low}
    hit |= {t.lower() for t in bi if t.lower() in toks}
    return len(hit)


_CJK_LETTER_RE = re.compile(r'[぀-ヿ㐀-鿿가-힯]')
_ROLE_PREFIX_RE = re.compile(r'^\[(?:user|claude)(?: \d+/\d+)?\] ')


def _is_cjk_text(text):
    """True when CJK characters are a fifth or more of the letters (role prefix left out)."""
    body = _ROLE_PREFIX_RE.sub('', text or '')
    letters = [ch for ch in body if ch.isalpha()]
    if not letters:
        return False
    return sum(1 for ch in letters if _CJK_LETTER_RE.match(ch)) * 5 >= len(letters)


def _order_key(r):
    return (r["score"], r.get("timestamp") or "", r.get("id") or "")


def _first_row_key(conn, turn_id, cache):
    if turn_id not in cache:
        row = conn.execute("SELECT timestamp, id FROM memories WHERE turn_id = ? ORDER BY rowid LIMIT 1",
                           (turn_id,)).fetchone()
        cache[turn_id] = (row[0], row[1]) if row else ("", "")
    return cache[turn_id]


def _representative_vector(conn, turn_id, min_chars):
    rows = conn.execute("SELECT rowid, content FROM memories WHERE turn_id = ? ORDER BY rowid",
                        (turn_id,)).fetchall()
    for rowid, content in rows:
        if content.startswith("[claude") and len(content) >= min_chars:
            return get_vector(conn, rowid)  # the first such row; None when it has no vector
    return None


def _order_versions_newest_first(conn, results, cfg):
    """Group turns whose representative [claude row vectors are nearly identical and put the newest
    version at the highest position of the group. Positions keep their score and filter_score."""
    threshold = _cfg(cfg, "version_sim_threshold")
    min_chars = _cfg(cfg, "version_min_chars")
    turns = []
    for r in results:
        t = r.get("turn_id")
        if t and t not in turns:
            turns.append(t)
    reps = {}
    for t in turns:
        v = _representative_vector(conn, t, min_chars)
        if v is not None:
            reps[t] = v
    keys = list(reps)
    parent = {t: t for t in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if _cos(reps[keys[i]], reps[keys[j]]) >= threshold:
                parent[find(keys[i])] = find(keys[j])
    groups = {}
    for t in keys:
        groups.setdefault(find(t), []).append(t)
    cache = {}
    out = list(results)
    for members in groups.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        positions = [i for i, r in enumerate(out) if r.get("turn_id") in member_set]
        ordered_turns = sorted(members, key=lambda t: _first_row_key(conn, t, cache), reverse=True)
        rows_by_turn = {t: [out[i] for i in positions if out[i].get("turn_id") == t] for t in members}
        new_rows = [row for t in ordered_turns for row in rows_by_turn[t]]
        slots = [(out[i]["score"], out[i]["filter_score"]) for i in positions]
        for pos, row, (score, fscore) in zip(positions, new_rows, slots):
            moved = dict(row)
            moved["score"] = score
            moved["filter_score"] = fscore
            moved["_version_group"] = True
            out[pos] = moved
    return out


def rerank_by_reply_similarity(conn, results, query_vec, cfg):
    """2b: mix the score with how close the turn's [claude reply is to the question (stored vectors only)."""
    if not results or query_vec is None:
        return results
    try:
        alpha = _cfg(cfg, "reply_sim_alpha")
        floor = _cfg(cfg, "reply_sim_floor")
        span = _cfg(cfg, "reply_sim_span")
        turns = sorted({r["turn_id"] for r in results if r.get("turn_id")})
        reply_vecs = {}
        if turns:
            marks = ",".join("?" * len(turns))
            rows = conn.execute(
                f"SELECT m.turn_id, v.embedding FROM memories m JOIN memories_vec v ON v.rowid = m.rowid "
                f"WHERE m.turn_id IN ({marks}) AND m.content LIKE '[claude%'", turns).fetchall()
            from .database import deserialize_vector
            acc = {}
            for t, blob in rows:
                vec = deserialize_vector(blob)
                if t not in acc:
                    acc[t] = [0.0] * len(vec)
                    reply_vecs[t] = 0
                acc[t] = [a + b for a, b in zip(acc[t], vec)]
                reply_vecs[t] += 1
            reply_vecs = {t: [x / reply_vecs[t] for x in acc[t]] for t in acc}
        top = max(r["score"] for r in results) or 1.0
        out = []
        for r in results:
            rr = dict(r)
            comp = 0.0
            v = reply_vecs.get(r.get("turn_id"))
            if v is not None:
                comp = max(0.0, (_cos(v, query_vec) - floor) / span)
            rr["score"] = (1 - alpha) * r["score"] / top + alpha * comp
            out.append(rr)
        out.sort(key=_order_key, reverse=True)
        return out
    except Exception:  # noqa: BLE001
        return results


def _collapse_identical_content(results):
    """Keep one row per identical content at the highest position, with the newest record's data."""
    by_content = {}
    for r in results:
        by_content.setdefault(r["content"], []).append(r)
    out, done = [], set()
    for r in results:
        c = r["content"]
        if c in done:
            continue
        done.add(c)
        group = by_content[c]
        if len(group) == 1:
            out.append(r)
            continue
        newest = max(group, key=lambda g: (g.get("timestamp") or "", g.get("id") or ""))
        merged = dict(r)
        for k in ("id", "timestamp", "turn_id", "agent_name", "session_id", "rowid"):
            merged[k] = newest.get(k)
        out.append(merged)
    return out


def _cap_per_turn(results, limit, cap=2):
    counts, out = {}, []
    for r in results:
        t = r.get("turn_id")
        if t is not None:
            if counts.get(t, 0) >= cap:
                continue
            counts[t] = counts.get(t, 0) + 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _build_pairs(conn, results, cfg):
    top_n = _cfg(cfg, "pair_top_n")
    max_chars = _cfg(cfg, "pair_max_chars")
    pairs, omitted = {}, {}
    seen = []
    for r in results:
        if r.get("weak"):
            continue
        t = r.get("turn_id")
        if t and t not in seen:
            seen.append(t)
        if len(seen) >= top_n:
            break
    total = 0
    for t in seen[:top_n]:
        siblings = get_memories_by_turn_id(conn, t)
        if not siblings:
            continue
        rows = []
        stop = False
        for i, s in enumerate(siblings):
            if total + len(s["content"]) > max_chars:
                omitted[t] = len(siblings) - i
                stop = True
                break
            total += len(s["content"])
            rows.append({"id": s["id"], "content": s["content"], "timestamp": s["timestamp"]})
        if rows:
            pairs[t] = rows
        if stop:
            break
    return pairs, omitted


def _public_row(r, weak=False):
    return {
        "id": r["id"], "content": r["content"], "score": r["score"], "timestamp": r["timestamp"],
        "turn_id": r.get("turn_id"), "weak": weak, "agent_name": r.get("agent_name"),
        "session_id": r.get("session_id"), "filter_score": r["filter_score"],
        "cos_sim": r.get("cos_sim", 0.0), "keyword_relevance": r.get("keyword_relevance", 0.0),
    }


def hybrid_search(conn, query, cfg=None, session_id=None, since=None, until=None, query_vec=None,
                  embed_fn=None):
    """Vector + FTS search with the ranking formula and the result shaping of §5.
    Returns {"results", "pairs", "pairs_omitted"}."""
    empty ={"results": [], "pairs": {}, "pairs_omitted": {}}
    if not query or not query.strip():
        return empty
    limit = int(_cfg(cfg, "search_result_limit"))
    k = 3 * limit
    if query_vec is None:
        try:
            query_vec = (embed_fn or embed_query)(query)
        except Exception as e:  # noqa: BLE001
            query_vec = None
            print(f"Warning: vector search degraded (embed_query failed: {type(e).__name__}: {e})",
                  file=sys.stderr)
    vec_rows = []
    if query_vec is not None:
        try:
            vec_rows = search_vector(conn, query_vec, k, since, until)
        except Exception as e:  # noqa: BLE001
            print(f"Warning: vector search failed ({type(e).__name__}: {e})", file=sys.stderr)
            vec_rows = []
    # fts_stopwords: English function words are not keyword terms (not in the specification)
    stop = bool(_cfg(cfg, "fts_stopwords"))
    try:
        tri_rows = search_fts(conn, query, k, since, until, stopwords=stop)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: keyword search failed ({type(e).__name__}: {e})", file=sys.stderr)
        tri_rows = []
    try:
        bi_rows = search_fts_bigram(conn, query, k, since, until, stopwords=stop)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: bigram keyword search failed ({type(e).__name__}: {e})", file=sys.stderr)
        bi_rows = []

    small_db = count_memories(conn) <= 2
    threshold = _cfg(cfg, "bm25_min_threshold")
    kw = {}
    rows_by_rowid = {}
    for table_rows in (tri_rows, bi_rows):
        if not table_rows:
            continue
        max_abs = max(-r["bm25"] for r in table_rows)
        for r in table_rows:
            rel = keyword_relevance(r["bm25"], max_abs, threshold, small_db)
            kw[r["rowid"]] = max(kw.get(r["rowid"], 0.0), rel)
            rows_by_rowid.setdefault(r["rowid"], r)
    real_cos = {}
    for r in vec_rows:
        real_cos[r["rowid"]] = cosine_sim_from_l2(r["distance"])
        rows_by_rowid.setdefault(r["rowid"], r)

    vmin = _cfg(cfg, "vector_min_cos")
    half_life = _cfg(cfg, "half_life_days")
    min_score = _cfg(cfg, "min_score")
    now = datetime.now(timezone.utc)

    def make(rowid, cos_value):
        base = rows_by_rowid[rowid]
        kr = kw.get(rowid, 0.0)
        decay = compute_time_decay(base["timestamp"], half_life, now)
        bias = b_session(base.get("session_id"), session_id)
        score, fscore = combined_score(cos_value, kr, decay, bias)
        d = {k2: base.get(k2) for k2 in ("rowid", "id", "content", "timestamp", "agent_name",
                                         "session_id", "turn_id")}
        d.update(score=score, filter_score=fscore, cos_sim=cos_value, keyword_relevance=kr)
        return d

    # cross_lang_min_cos (not in the specification; 0 = off): the best vector hit written in the
    # other script (CJK or not) counts as a vector match from this cosine on. e5 gives a question and
    # a record in different languages a lower cosine than a same-language pair, so vector_min_cos
    # alone never lets them through.
    cross_floor = float(_cfg(cfg, "cross_lang_min_cos") or 0)
    cross_best = None
    if cross_floor and real_cos:
        q_cjk = _is_cjk_text(query)
        other = [(c, rid) for rid, c in real_cos.items()
                 if c < vmin and _is_cjk_text(rows_by_rowid[rid]["content"]) != q_cjk]
        if other:
            c_top, rid_top = max(other)
            if c_top >= cross_floor:
                cross_best = rid_top
    # semantic rescue (not in the specification; semantic_rescue_min_cos 0 = off): the closest exchange
    # counts as a vector match below vector_min_cos when its cosine is at least semantic_rescue_min_cos
    # and beats every other exchange by semantic_rescue_margin. A question with an answer usually has
    # one exchange clearly ahead; for a question without one, the nearest records lie close together.
    rescue_floor = float(_cfg(cfg, "semantic_rescue_min_cos") or 0)
    rescued = None
    if rescue_floor and real_cos:
        best = {}
        for rid, c in real_cos.items():
            key = rows_by_rowid[rid].get("turn_id") or ("row", rid)
            if key not in best or c > best[key][0]:
                best[key] = (c, rid)
        ranked = sorted(best.values(), reverse=True)
        top_c, top_rid = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        if vmin > top_c >= rescue_floor and top_c - second >= float(_cfg(cfg, "semantic_rescue_margin")):
            rescued = top_rid
    # kw_only_min_terms (not in the specification; 0 or 1 = off): a row found only by the keywords
    # goes to Top matches only when it holds that many distinct question terms; otherwise it can
    # still be a weak match.
    min_terms = int(_cfg(cfg, "kw_only_min_terms") or 0)
    q_terms = (fts_terms(query, stop), bigram_terms(query, stop)) if min_terms > 1 else None

    main, weak_pool = [], {}
    for rowid in rows_by_rowid:
        rc = real_cos.get(rowid)
        in_fts = rowid in kw
        if rc is not None and (rc >= vmin or rowid == cross_best or rowid == rescued):
            cand = make(rowid, rc)
        elif in_fts:
            if q_terms and _matched_terms(rows_by_rowid[rowid]["content"], q_terms) < min_terms:
                weak_pool[rowid] = make(rowid, rc or 0.0)
                continue
            cand = make(rowid, 0.0)  # an FTS-only hit
        else:
            weak_pool[rowid] = make(rowid, rc or 0.0)  # step 1: below vector_min_cos
            continue
        if cand["filter_score"] < min_score:  # step 2
            weak_pool[rowid] = make(rowid, rc if rc is not None else 0.0)
            continue
        main.append(cand)
    main.sort(key=_order_key, reverse=True)

    if _cfg(cfg, "reply_sim_rerank_enabled"):  # 2b
        main = rerank_by_reply_similarity(conn, main, query_vec, cfg)
    if _cfg(cfg, "version_order_enabled"):  # 3
        try:
            main = _order_versions_newest_first(conn, main, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"Warning: version ordering skipped ({type(e).__name__}: {e})", file=sys.stderr)
    ratio = _cfg(cfg, "relative_min_score_ratio")  # 4
    if main and ratio > 0:
        top = max(r["filter_score"] for r in main)
        kept = []
        for r in main:
            if r["filter_score"] >= ratio * top or r.get("_version_group"):
                kept.append(r)
            else:
                rc = real_cos.get(r["rowid"])
                weak_pool[r["rowid"]] = make(r["rowid"], rc if rc is not None else 0.0)
        main = kept
    main = _collapse_identical_content(main)  # 5
    # top_matches_max (not in the specification; 0 = none): a cap on Top matches only
    strong_cap = min(limit, int(_cfg(cfg, "top_matches_max") or 0) or limit)
    main = _cap_per_turn(main, strong_cap)  # 6

    target = min(int(_cfg(cfg, "min_results")), limit)  # 7
    weak_rows = []
    if target > 0 and len(main) < target:
        in_main = {r["rowid"] for r in main}
        pool = [w for rid, w in weak_pool.items() if rid not in in_main
                and w["filter_score"] >= WEAK_MIN_FILTER_SCORE]
        pool.sort(key=lambda w: (w["filter_score"], w.get("timestamp") or "", w.get("id") or ""), reverse=True)
        contents = {r["content"] for r in main}
        counts = {}
        for r in main:
            if r.get("turn_id") is not None:
                counts[r["turn_id"]] = counts.get(r["turn_id"], 0) + 1
        for w in pool:
            if len(main) + len(weak_rows) >= target:
                break
            if w["content"] in contents:
                continue
            t = w.get("turn_id")
            if t is not None and counts.get(t, 0) >= 2:
                continue
            contents.add(w["content"])
            if t is not None:
                counts[t] = counts.get(t, 0) + 1
            w["_weak"] = True
            weak_rows.append(w)

    results = [_public_row(r) for r in main] + [_public_row(r, weak=True) for r in weak_rows]
    pairs, omitted = _build_pairs(conn, results, cfg)  # 8
    return {"results": results, "pairs": pairs, "pairs_omitted": omitted}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_saved(ts) -> str:
    dt = _parse_ts(ts)
    if dt is None:
        return str(ts)
    try:
        local = dt.astimezone()
    except (OSError, OverflowError, ValueError):
        local = dt
    return f"{local:%Y-%m-%d %H:%M} (UTC{local:%z})"


HEADER = "# Recalled Memory Context"
NO_RESULTS = "_No relevant memories found._"
VERSION_NOTE = ("_If several versions of the same content appear, the one with the newest `saved` "
                "time is the current one._")


def _notice_lines(notices):
    return "".join(f"> ⚠️ N3MC notice: {m}\n\n" for m in (notices or []))


def render_memory_context(query, data, notices=None) -> str:
    results = (data or {}).get("results") or []
    pairs = (data or {}).get("pairs") or {}
    omitted = (data or {}).get("pairs_omitted") or {}
    strong = [r for r in results if not r.get("weak")]
    weak = [r for r in results if r.get("weak")]
    out = [f"{HEADER}\nSearch query: {query}\n\n", _notice_lines(notices)]
    n = 0
    if strong:
        out.append("## Top matches (use these to answer the question)\n\n")
        out.append(VERSION_NOTE + "\n\n")
        for r in strong:
            n += 1
            out.append(f"## [{n}] score={r['score']:.4f} saved={_fmt_saved(r['timestamp'])} "
                       f"{r.get('agent_name') or '-'}\n{r['content']}\n\n")
    else:
        out.append(NO_RESULTS + "\n\n")
    if weak:
        out.append("## Possibly related (weak matches — may be unrelated)\n\n")
        for r in weak:
            n += 1
            out.append(f"## [{n}] score={r['score']:.4f} saved={_fmt_saved(r['timestamp'])} "
                       f"{r.get('agent_name') or '-'}\n{r['content']}\n\n")
    turn_ids = list(pairs)
    for t in omitted:
        if t not in turn_ids:
            turn_ids.append(t)
    if turn_ids:
        out.append("## Previous matching Q-A exchanges (supplementary context)\n\n")
        for t in turn_ids:
            rows = pairs.get(t) or []
            saved = f" saved={_fmt_saved(rows[0]['timestamp'])}" if rows else ""
            out.append(f"### turn_id: {t}{saved}\n")
            for row in rows:
                out.append(row["content"] + "\n")
            if t in omitted:
                out.append(f"_({omitted[t]} more chunk(s) of this exchange omitted. Full text: "
                           f"`python -m n3memorycore.n3memory --show-turn {t}`)_\n")
            out.append("\n")
    return "".join(out).rstrip("\n") + "\n"


def render_placeholder(text, notices=None) -> str:
    """Output without results: the header, the notices, then the placeholder line."""
    return f"{HEADER}\n\n{_notice_lines(notices)}{text}\n"
