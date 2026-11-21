"""Processing layer: embeddings, ranking, purify, chunking, hybrid search, rendering."""
import math
import re
import sys
import threading
import time
from datetime import datetime, timezone

from .database import (
    get_connection, init_db, insert_memory, search_vector, search_fts, search_fts_bigram,
    get_all_memories, delete_memory, count_memories, check_exact_duplicate,
    find_unindexed_memories, serialize_vector, get_memories_by_turn_id, strip_fts_punctuation,
    deserialize_vector,
)

DEFAULT_MODEL = "intfloat/multilingual-e5-base"
MODEL_RETRY_SECONDS = 60

_model = None
_model_name = None
_model_failed_at = None
_model_error = None
_model_lock = threading.Lock()
_warned_embed = False


# ---------------------------------------------------------------- embeddings

def _load_model(name):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name)


def get_model(name=None):
    """Load the embedding model lazily. After a failed load, do not retry for MODEL_RETRY_SECONDS."""
    global _model, _model_name, _model_failed_at, _model_error
    name = name or _current_model_name()
    with _model_lock:
        if _model is not None and _model_name == name:
            return _model
        if _model_failed_at is not None and time.monotonic() - _model_failed_at < MODEL_RETRY_SECONDS:
            raise RuntimeError(f"embedding model unavailable ({_model_error})")
        try:
            _model = _load_model(name)
            _model_name = name
            _model_failed_at = None
            return _model
        except Exception as e:  # noqa: BLE001
            _model = None
            _model_failed_at = time.monotonic()
            _model_error = f"{type(e).__name__}: {e}"
            print(f"N3MC: could not load embedding model {name}: {_model_error}", file=sys.stderr)
            raise


def _current_model_name():
    try:
        from .. import n3memory
        return n3memory.get_config().get("embed_model", DEFAULT_MODEL)
    except Exception:  # noqa: BLE001
        return DEFAULT_MODEL


def _encode(text, name=None):
    vec = get_model(name).encode([text], normalize_embeddings=True)[0]
    return [float(x) for x in vec]


def embed_passage(text, name=None):
    return _encode("passage: " + text, name)


def embed_query(text, name=None):
    return _encode("query: " + text, name)


def cosine_sim_from_l2(distance):
    return max(0.0, 1.0 - (distance * distance) / 2.0)


# ------------------------------------------------------------------- ranking

def _parse_ts(ts):
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def time_decay(timestamp, half_life_days=90, now=None):
    try:
        then = _parse_ts(timestamp)
    except Exception:  # noqa: BLE001
        return 1.0
    now = now or datetime.now(timezone.utc)
    days = max(0.0, (now - then).total_seconds() / 86400.0)
    return 2.0 ** (-days / float(half_life_days))


def keyword_relevances(raw_scores, bm25_min_threshold=0.1, small_db=False):
    """raw_scores: {key: bm25 raw (negative)}. Returns {key: [0,1]} normalised by this table's max."""
    if not raw_scores:
        return {}
    mx = max(-v for v in raw_scores.values())
    out = {}
    if small_db:
        for k, v in raw_scores.items():
            out[k] = (-v / mx) if mx > 0 else 0.0
        return out
    denom = max(1.0, mx)
    for k, v in raw_scores.items():
        out[k] = 0.0 if abs(v) < bm25_min_threshold else max(0.0, -v) / denom
    return out


def b_session(record_session, current_session):
    return 1.0 if (current_session and record_session and record_session == current_session) else 0.6


# ------------------------------------------------------------ text processing

_SURR_RE = re.compile('[\ud800-\udfff]')


def sanitize_surrogates(obj):
    """Remove lone surrogate code points (recursively through dicts and lists)."""
    if isinstance(obj, str):
        return _SURR_RE.sub('', obj)
    if isinstance(obj, dict):
        return {sanitize_surrogates(k): sanitize_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_surrogates(v) for v in obj]
    return obj


_CODE_BLOCK_RE = re.compile(r"```[^\n`]*\n.*?```", re.DOTALL)


def purify_text(text):
    if text is None:
        return ""
    text = sanitize_surrogates(text)
    return _CODE_BLOCK_RE.sub("[code omitted]", text)


_SENT_RE = re.compile(r'(?<=[.!?])\s+|(?<=[。！？])')


def _hard_windows(s, max_chars, overlap):
    step = max(1, max_chars - overlap)
    out, i = [], 0
    while True:
        out.append(s[i:i + max_chars])
        if i + max_chars >= len(s):
            break
        i += step
    return out


def _split_parts(text, max_chars, overlap):
    """Return [(part, sep_before, hard)] where every part is <= max_chars."""
    parts = []
    paras = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    for pi, para in enumerate(paras):
        psep = "\n\n"
        if len(para) <= max_chars:
            parts.append((para, psep, False))
            continue
        sents = [s.strip() for s in _SENT_RE.split(para) if s and s.strip()]
        first = True
        for s in sents:
            sep = psep if first else " "
            first = False
            if len(s) <= max_chars:
                parts.append((s, sep, False))
            else:
                for w in _hard_windows(s, max_chars, overlap):
                    parts.append((w, sep, True))
    return parts


def chunk_text(text, max_chars=400, overlap=40):
    """Split into chunks of at most max_chars (paragraph -> sentence -> hard window), with overlap."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks = []
    cur = None
    cur_has_tail = False

    def start(part, sep):
        if chunks and overlap > 0:
            room = max_chars - len(sep) - len(part)
            tl = min(overlap, room)
            if tl > 0:
                tail = chunks[-1][-tl:]
                return tail + sep + part, True
        return part, False

    for part, sep, hard in _split_parts(text, max_chars, overlap):
        if hard:
            if cur is not None:
                chunks.append(cur)
                cur = None
            chunks.append(part)
            continue
        if cur is None:
            cur, cur_has_tail = start(part, sep)
            continue
        if len(cur) + len(sep) + len(part) <= max_chars:
            cur = cur + sep + part
        else:
            chunks.append(cur)
            cur, cur_has_tail = start(part, sep)
    if cur is not None:
        chunks.append(cur)
    return chunks


def add_chunk_prefixes(role, chunks):
    n = len(chunks)
    if n == 1:
        return [f"[{role}] {chunks[0]}"]
    return [f"[{role} {i}/{n}] {c}" for i, c in enumerate(chunks, 1)]


# ------------------------------------------------------------------- searching

def _as_row(rec, cos, kw, session_id, cfg, now):
    td = time_decay(rec["timestamp"], cfg["half_life_days"], now)
    bs = b_session(rec.get("session_id"), session_id)
    filt = cos * 0.7 + kw * 0.3
    return {
        "rowid": rec.get("rowid"), "id": rec["id"], "content": rec["content"], "timestamp": rec["timestamp"],
        "turn_id": rec.get("turn_id"), "agent_name": rec.get("agent_name"), "session_id": rec.get("session_id"),
        "cos_sim": cos, "keyword_relevance": kw, "filter_score": filt, "score": filt * td * bs, "weak": False,
    }


def _sort_key(r):
    return (r["score"], r["timestamp"], r["id"])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _stored_vector(conn, rowid):
    row = conn.execute("SELECT embedding FROM memories_vec WHERE rowid=?", (rowid,)).fetchone()
    return deserialize_vector(row[0]) if row and row[0] is not None else None


def _turn_representative(conn, turn_id, min_chars):
    for r in conn.execute("SELECT rowid, content FROM memories WHERE turn_id=? AND content LIKE '[claude%' "
                          "ORDER BY rowid", (turn_id,)).fetchall():
        if len(r["content"]) >= min_chars:
            return _stored_vector(conn, r["rowid"])
    return None


def _order_versions_newest_first(conn, results, cfg):
    """Group results whose turns hold near-identical replies and put the newest version first."""
    turns = []
    for r in results:
        if r.get("turn_id") and r["turn_id"] not in turns:
            turns.append(r["turn_id"])
    reps = {}
    for t in turns:
        v = _turn_representative(conn, t, cfg["version_min_chars"])
        if v is not None:
            reps[t] = v
    ts = list(reps)
    parent = {t: t for t in ts}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(ts)):
        for j in range(i + 1, len(ts)):
            if _dot(reps[ts[i]], reps[ts[j]]) >= cfg["version_sim_threshold"]:
                parent[find(ts[i])] = find(ts[j])
    groups = {}
    for t in ts:
        groups.setdefault(find(t), []).append(t)
    for members in groups.values():
        if len(members) < 2:
            continue
        first_row = {}
        for t in members:
            r0 = conn.execute("SELECT timestamp, id FROM memories WHERE turn_id=? ORDER BY rowid LIMIT 1",
                              (t,)).fetchone()
            first_row[t] = (r0["timestamp"], r0["id"]) if r0 else ("", "")
        order = sorted(members, key=lambda t: first_row[t], reverse=True)
        positions = [i for i, r in enumerate(results) if r.get("turn_id") in members]
        new_rows = []
        for t in order:
            new_rows.extend(r for r in results if r.get("turn_id") == t)
        old = [results[p] for p in positions]
        for p, row, o in zip(positions, new_rows, old):
            results[p] = {**row, "score": o["score"], "filter_score": o["filter_score"], "_version_group": True}
    return results


def _collapse_identical_content(results):
    first_pos, newest = {}, {}
    for i, r in enumerate(results):
        c = r["content"]
        first_pos.setdefault(c, i)
        if c not in newest or (r["timestamp"], r["id"]) > (newest[c]["timestamp"], newest[c]["id"]):
            newest[c] = r
    out = []
    for i, r in enumerate(results):
        if first_pos[r["content"]] != i:
            continue
        n = newest[r["content"]]
        out.append({**r, "id": n["id"], "timestamp": n["timestamp"], "turn_id": n["turn_id"],
                    "rowid": n.get("rowid")})
    return out


def _rerank_by_reply(conn, query_vec, results, cfg):
    try:
        if query_vec is None or not results:
            return results
        alpha, floor, span = cfg["reply_sim_alpha"], cfg["reply_sim_floor"], cfg["reply_sim_span"]
        sims = {}
        for t in {r["turn_id"] for r in results if r.get("turn_id")}:
            vecs = []
            for r in conn.execute("SELECT rowid FROM memories WHERE turn_id=? AND content LIKE '[claude%'",
                                  (t,)).fetchall():
                v = _stored_vector(conn, r["rowid"])
                if v:
                    vecs.append(v)
            if vecs:
                mean = [sum(c) / len(vecs) for c in zip(*vecs)]
                sims[t] = _dot(query_vec, mean)
        top = max((r["score"] for r in results), default=0.0) or 1.0
        out = []
        for r in results:
            s = sims.get(r.get("turn_id"))
            bonus = max(0.0, (s - floor) / span) if s is not None else 0.0
            out.append({**r, "score": (1 - alpha) * r["score"] / top + alpha * bonus})
        out.sort(key=_sort_key, reverse=True)
        return out
    except Exception:  # noqa: BLE001
        return results


def hybrid_search(conn, query, cfg, session_id=None, since=None, until=None, embed_fn=None):
    """Hybrid (vector + trigram FTS + bigram FTS) search with the v1.6 shaping pipeline."""
    global _warned_embed
    limit = cfg["search_result_limit"]
    now = datetime.now(timezone.utc)
    if not (query or "").strip():
        return {"results": [], "pairs": {}, "pairs_omitted": {}}
    embed_fn = embed_fn or embed_query

    # --- vector candidates
    qvec = None
    vec_hits = []  # (rec, cos)
    try:
        qvec = embed_fn(query)
        for rec, dist in search_vector(conn, qvec, k=3 * limit, since=since, until=until):
            vec_hits.append((rec, cosine_sim_from_l2(dist)))
    except Exception as e:  # noqa: BLE001
        qvec = None
        if not _warned_embed:
            _warned_embed = True
        print(f"Warning: vector search degraded (embed_query failed: {e})", file=sys.stderr)

    # --- keyword candidates (per table normalisation)
    small = count_memories(conn) <= 2
    kw = {}   # id -> relevance
    recs = {}
    for fn in (search_fts, search_fts_bigram):
        try:
            hits = fn(conn, query, limit=3 * limit, since=since, until=until)
        except Exception as e:  # noqa: BLE001
            print(f"N3MC: keyword search failed ({type(e).__name__}: {e}); continuing without it", file=sys.stderr)
            continue
        raw = {rec["id"]: s for rec, s in hits}
        for rec, _ in hits:
            recs[rec["id"]] = rec
        for k, v in keyword_relevances(raw, cfg["bm25_min_threshold"], small).items():
            kw[k] = max(kw.get(k, 0.0), v)

    actual_cos = {}
    for rec, c in vec_hits:
        recs[rec["id"]] = rec
        actual_cos[rec["id"]] = c

    floor = cfg["vector_min_cos"]
    main, pool = [], []
    for rid, rec in recs.items():
        c_act = actual_cos.get(rid, 0.0)
        k = kw.get(rid, 0.0)
        pool.append(_as_row(rec, c_act, k, session_id, cfg, now))
        c_main = c_act if c_act >= floor else 0.0
        in_vec = c_main > 0.0
        if not in_vec and rid not in kw:
            continue
        main.append(_as_row(rec, c_main, k, session_id, cfg, now))

    # step 2: min_score on filter_score
    results = [r for r in main if r["filter_score"] >= cfg["min_score"]]
    results.sort(key=_sort_key, reverse=True)
    if cfg.get("reply_sim_rerank_enabled"):
        results = _rerank_by_reply(conn, qvec, results, cfg)
    # step 3
    if cfg.get("version_order_enabled", True):
        try:
            results = _order_versions_newest_first(conn, results, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"N3MC: version ordering skipped ({e})", file=sys.stderr)
    # step 4
    if results:
        top = max(r["filter_score"] for r in results)
        ratio = cfg["relative_min_score_ratio"]
        if ratio > 0:
            results = [r for r in results if r.get("_version_group") or r["filter_score"] >= ratio * top]
    # step 5
    results = _collapse_identical_content(results)
    # step 6
    capped, per_turn = [], {}
    for r in results:
        t = r.get("turn_id")
        if t:
            if per_turn.get(t, 0) >= 2:
                continue
            per_turn[t] = per_turn.get(t, 0) + 1
        capped.append(r)
        if len(capped) >= limit:
            break
    results = capped
    # step 7: weak fill
    target = min(cfg["min_results"], limit)
    if cfg["min_results"] > 0 and len(results) < target:
        have_ids = {r["id"] for r in results}
        have_content = {r["content"] for r in results}
        pool = [p for p in pool if p["id"] not in have_ids and p["filter_score"] >= 1e-6]
        pool.sort(key=lambda p: (p["filter_score"], p["timestamp"], p["id"]), reverse=True)
        for p in pool:
            if len(results) >= target:
                break
            if p["content"] in have_content:
                continue
            t = p.get("turn_id")
            if t and per_turn.get(t, 0) >= 2:
                continue
            if t:
                per_turn[t] = per_turn.get(t, 0) + 1
            have_content.add(p["content"])
            results.append({**p, "weak": True})
    pairs, omitted = _build_pairs(conn, results, cfg)
    return {"results": results, "pairs": pairs, "pairs_omitted": omitted}


def _role_order(rec):
    return 0 if rec["content"].startswith("[user") else 1


def _build_pairs(conn, results, cfg):
    pairs, omitted = {}, {}
    total, seen = 0, []
    for r in results:
        t = r.get("turn_id")
        if r.get("weak") or not t or t in seen:
            continue
        seen.append(t)
        if len(seen) > cfg["pair_top_n"]:
            break
        sibs = sorted(get_memories_by_turn_id(conn, t), key=_role_order)  # stable: rowid order inside a role
        taken = []
        for i, s in enumerate(sibs):
            if total + len(s["content"]) > cfg["pair_max_chars"]:
                omitted[t] = len(sibs) - i
                break
            total += len(s["content"])
            taken.append({"id": s["id"], "content": s["content"], "timestamp": s["timestamp"]})
        if taken:
            pairs[t] = taken
        if t in omitted:
            break
    return pairs, omitted


# -------------------------------------------------------------------- render

def _fmt_saved(ts):
    try:
        dt = _parse_ts(ts).astimezone()
        off = dt.strftime("%z") or "+0000"
        return f"{dt.strftime('%Y-%m-%d %H:%M')} (UTC{off})"
    except Exception:  # noqa: BLE001
        return str(ts)


def render_memory_context(query, results, pairs=None, pairs_omitted=None, notices=None):
    pairs, pairs_omitted = pairs or {}, pairs_omitted or {}
    lines = ["# Recalled Memory Context", f"Search query: {query}", ""]
    for n in notices or []:
        lines += [f"> ⚠️ N3MC notice: {n}", ""]
    normal = [r for r in results if not r.get("weak")]
    weak = [r for r in results if r.get("weak")]
    num = 0

    def row(r):
        nonlocal num
        num += 1
        return [f"## [{num}] score={r['score']:.4f} saved={_fmt_saved(r['timestamp'])} "
                f"{r.get('agent_name') or '-'}", r["content"], ""]

    if normal:
        lines += ["## Top matches (use these to answer the question)", "",
                  "_If several versions of the same content appear, the one with the newest `saved` "
                  "time is the current one._", ""]
        for r in normal:
            lines += row(r)
    else:
        lines += ["_No relevant memories found._", ""]
    if weak:
        lines += ["## Possibly related (weak matches — may be unrelated)", ""]
        for r in weak:
            lines += row(r)
    if pairs or pairs_omitted:
        lines += ["## Previous matching Q-A exchanges (supplementary context)", ""]
        for t in list(pairs) + [t for t in pairs_omitted if t not in pairs]:
            first = pairs.get(t, [{}])[0].get("timestamp")
            head = f"### turn_id: {t}"
            if first:
                head += f" saved={_fmt_saved(first)}"
            lines.append(head)
            for s in pairs.get(t, []):
                lines.append(s["content"])
            if pairs_omitted.get(t):
                lines.append(f"_({pairs_omitted[t]} more chunk(s) of this exchange omitted. Full text: "
                             f"`python -m n3memorycore.n3memory --show-turn {t}`)_")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
