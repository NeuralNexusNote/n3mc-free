"""Processing layer: embeddings, ranking maths, chunking, purify, hybrid search, rendering."""
import re
import sys
import time
from datetime import datetime, timezone

from .database import (
    bigram_terms,
    bigram_text,
    fts_terms,
    get_memories_by_turn_id,
    search_fts,
    search_fts_bigram,
    search_vector,
    strip_fts_punctuation,
)

DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-base"
MODEL_RETRY_SECONDS = 60

# ------------------------------------------------------------------ embedding

_MODEL = None
_MODEL_NAME = None
_MODEL_FAILED_AT = None
_MODEL_FAILED_ERR = None


def get_model(model_name=None):
    """Load (once) and return the SentenceTransformer. After a failed load, do not retry for
    MODEL_RETRY_SECONDS: fail fast so saves fall back to vector-less records.

    The model name comes only from config.json (callers pass cfg['embed_model']); there is
    deliberately no environment-variable override."""
    global _MODEL, _MODEL_NAME, _MODEL_FAILED_AT, _MODEL_FAILED_ERR
    name = model_name or DEFAULT_EMBED_MODEL
    if _MODEL is not None and _MODEL_NAME == name:
        return _MODEL
    if _MODEL_FAILED_AT is not None and _MODEL_NAME == name:
        if time.monotonic() - _MODEL_FAILED_AT < MODEL_RETRY_SECONDS:
            raise RuntimeError(f"embedding model unavailable: {_MODEL_FAILED_ERR}")
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(name)
    except Exception as e:
        _MODEL, _MODEL_NAME = None, name
        _MODEL_FAILED_AT, _MODEL_FAILED_ERR = time.monotonic(), e
        print(f"N3MC: could not load embedding model {name}: {e}", file=sys.stderr)
        raise
    _MODEL, _MODEL_NAME = model, name
    _MODEL_FAILED_AT = _MODEL_FAILED_ERR = None
    return model


def _encode(text, model_name):
    model = get_model(model_name)
    return model.encode(text, normalize_embeddings=True)


def embed_passage(text, model_name=None):
    """Embed a stored record. The 'passage: ' prefix is required by the e5 models."""
    return _encode("passage: " + text, model_name)


def embed_query(text, model_name=None):
    """Embed a search query. The 'query: ' prefix is required by the e5 models."""
    return _encode("query: " + text, model_name)


# --------------------------------------------------------------- ranking maths

def cosine_sim_from_l2(l2_distance):
    """For L2-normalised vectors: cos = 1 - d^2 / 2, clamped at 0."""
    return max(0.0, 1.0 - (l2_distance * l2_distance) / 2.0)


def parse_timestamp(ts):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def time_decay(timestamp, half_life_days=90, now=None):
    dt = parse_timestamp(timestamp or "")
    if dt is None:
        return 1.0
    now = now or datetime.now(timezone.utc)
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return 2.0 ** (-days / half_life_days)


def keyword_relevance(bm25_score, max_abs, threshold=0.1, tiny_corpus=False, norm_floor=1.0):
    """bm25() is negative (more negative = more relevant). Normalise into [0, 1].

    With one or two records FTS5 clamps every IDF to ~1e-6, so a genuine match scores about
    -2e-6 and the threshold would erase it. In that case only, scale by the best hit."""
    if tiny_corpus:
        return max(0.0, min(1.0, -bm25_score / max_abs)) if max_abs > 0 else 0.0
    if abs(bm25_score) < threshold:
        return 0.0
    return max(0.0, min(1.0, -bm25_score / max(norm_floor, max_abs)))


def session_bias(row_session_id, current_session_id):
    return 1.0 if (current_session_id and row_session_id == current_session_id) else 0.6


# --------------------------------------------------------------- text handling

_CODE_BLOCK_RE = re.compile(r"```[^\n`]*\n[\s\S]*?```")


def sanitize_surrogates(obj):
    """Drop lone UTF-16 surrogates (they cannot be encoded as UTF-8). Recursive."""
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")
    if isinstance(obj, dict):
        return {sanitize_surrogates(k): sanitize_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_surrogates(v) for v in obj]
    return obj


def purify_text(text):
    """Closed multi-line code fences -> '[code omitted]'. Inline code and unclosed fences stay."""
    if not text:
        return text or ""
    return _CODE_BLOCK_RE.sub("[code omitted]", sanitize_surrogates(text))


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")


def chunk_text(text, max_chars=400, overlap=40):
    """Paragraph -> sentence -> hard window, with up to ``overlap`` chars carried over.

    Every chunk is at most ``max_chars`` long, overlap and separator included."""
    if not text or not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]

    atoms = []  # (text, separator placed before it)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for pi, para in enumerate(paragraphs):
        para_sep = "\n\n" if pi else ""
        if len(para) <= max_chars:
            atoms.append((para, para_sep))
            continue
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(para) if s and s.strip()]
        first_in_para = True
        for sent in sentences:
            sep = para_sep if first_in_para else " "
            first_in_para = False
            if len(sent) <= max_chars:
                atoms.append((sent, sep))
            else:
                for wi in range(0, len(sent), max_chars):
                    atoms.append((sent[wi:wi + max_chars], sep if wi == 0 else ""))

    chunks = []
    cur = ""
    for atom, sep in atoms:
        if cur and len(cur) + len(sep) + len(atom) <= max_chars:
            cur += sep + atom
            continue
        if cur:
            chunks.append(cur)
        prev = cur
        room = max_chars - len(atom) - len(sep)
        tail_len = min(overlap, room, len(prev))
        if prev and tail_len > 0:
            cur = prev[-tail_len:] + sep + atom
        else:
            cur = atom
    if cur:
        chunks.append(cur)
    return chunks


def add_chunk_prefixes(chunks, role):
    n = len(chunks)
    if n == 1:
        return [f"[{role}] {chunks[0]}"]
    return [f"[{role} {i}/{n}] {c}" for i, c in enumerate(chunks, 1)]


# ---------------------------------------------------------------- hybrid search

def _warn(msg):
    print(msg, file=sys.stderr)


def _kw_map(rows, threshold, tiny_corpus=False, norm_floor=1.0):
    mx = max((-r["bm25"] for r in rows), default=0.0)
    return {r["rowid"]: keyword_relevance(r["bm25"], mx, threshold, tiny_corpus, norm_floor)
            for r in rows}


def _matched_terms(content, terms):
    """How many distinct query terms (trigram terms, bigram terms) occur in ``content``."""
    tri, bi = terms
    low = strip_fts_punctuation(content).lower()
    toks = set(bigram_text(content).lower().split())
    hit = {t.lower() for t in tri if t.lower() in low}
    hit |= {t.lower() for t in bi if t.lower() in toks}
    return len(hit)


def _vec_of(conn, rowid):
    from .database import deserialize_vector
    row = conn.execute("SELECT embedding FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone()
    return deserialize_vector(row[0]) if row else None


def _cos(a, b):
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _turn_first_row(conn, turn_id):
    r = conn.execute("SELECT timestamp, id FROM memories WHERE turn_id = ? ORDER BY rowid LIMIT 1",
                     (turn_id,)).fetchone()
    return (r[0], r[1]) if r else ("", "")


def _order_versions_newest_first(conn, results, cfg):
    """Same content re-issued as a newer version: put the newest turn at the best position."""
    turns = {}
    for i, r in enumerate(results):
        if r.get("turn_id"):
            turns.setdefault(r["turn_id"], []).append(i)
    reps = {}
    for tid in turns:
        for row in conn.execute(
                "SELECT m.rowid, m.content FROM memories m WHERE m.turn_id = ? "
                "AND m.content LIKE '[claude%' ORDER BY m.rowid", (tid,)).fetchall():
            if len(row[1]) >= cfg["version_min_chars"]:
                vec = _vec_of(conn, row[0])
                if vec is not None:
                    reps[tid] = vec
                break
    tids = list(reps)
    parent = {t: t for t in tids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(tids)):
        for j in range(i + 1, len(tids)):
            if _cos(reps[tids[i]], reps[tids[j]]) >= cfg["version_sim_threshold"]:
                parent[find(tids[i])] = find(tids[j])
    groups = {}
    for t in tids:
        groups.setdefault(find(t), []).append(t)
    out = list(results)
    for members in groups.values():
        if len(members) < 2:
            continue
        positions = sorted(i for t in members for i in turns[t])
        saved = [(out[p]["score"], out[p]["filter_score"]) for p in positions]
        order = sorted(members, key=lambda t: _turn_first_row(conn, t), reverse=True)
        new_rows = []
        for t in order:
            new_rows.extend(results[i] for i in turns[t])
        for p, row, (sc, fs) in zip(positions, new_rows, saved):
            row = dict(row)
            row["score"], row["filter_score"] = sc, fs
            row["_version_group"] = True
            out[p] = row
    return out


def _rerank_by_reply_sim(conn, results, qvec, cfg):
    try:
        import numpy as np
        if qvec is None or not results:
            return results
        alpha, floor, span = cfg["reply_sim_alpha"], cfg["reply_sim_floor"], cfg["reply_sim_span"]
        tids = sorted({r["turn_id"] for r in results if r.get("turn_id")})
        if not tids:
            return results
        q = ",".join("?" * len(tids))
        sums, counts = {}, {}
        for row in conn.execute(
                f"SELECT m.turn_id, v.embedding FROM memories m JOIN memories_vec v "
                f"ON v.rowid = m.rowid WHERE m.turn_id IN ({q}) AND m.content LIKE '[claude%'",
                tids).fetchall():
            vec = np.frombuffer(bytes(row[1]), dtype=np.float32)
            sums[row[0]] = sums.get(row[0], 0) + vec
            counts[row[0]] = counts.get(row[0], 0) + 1
        qv = np.asarray(qvec, dtype=np.float32)
        sims = {}
        for t, s in sums.items():
            mean = s / counts[t]
            sims[t] = float(_cos(list(mean), list(qv)))
        top = max(r["score"] for r in results) or 1.0
        out = []
        for r in results:
            sim = sims.get(r.get("turn_id"), 0.0)
            r = dict(r)
            r["score"] = (1 - alpha) * r["score"] / top + alpha * max(0.0, (sim - floor) / span)
            out.append(r)
        out.sort(key=lambda r: (r["score"], r["timestamp"], r["id"]), reverse=True)
        return out
    except Exception:
        return results


def _collapse_identical_content(results):
    newest = {}
    for r in results:
        key = r["content"]
        cur = newest.get(key)
        if cur is None or (r["timestamp"], r["id"]) > (cur["timestamp"], cur["id"]):
            newest[key] = r
    out, seen = [], set()
    for r in results:
        if r["content"] in seen:
            continue
        seen.add(r["content"])
        n = newest[r["content"]]
        if n is not r:
            r = dict(r)
            for f in ("id", "timestamp", "turn_id", "rowid", "agent_name", "session_id"):
                r[f] = n.get(f)
        out.append(r)
    return out


def _result_row(c, filter_score, score, cos, kw, weak=False):
    return {
        "rowid": c["rowid"], "id": c["id"], "content": c["content"], "timestamp": c["timestamp"],
        "turn_id": c.get("turn_id"), "agent_name": c.get("agent_name"),
        "session_id": c.get("session_id"), "score": score, "filter_score": filter_score,
        "cos_sim": cos, "keyword_relevance": kw, "weak": weak,
    }


def hybrid_search(conn, query, cfg, session_id=None, since=None, until=None):
    """Vector + FTS5 hybrid search. Returns {'results', 'pairs', 'pairs_omitted'}."""
    empty = {"results": [], "pairs": {}, "pairs_omitted": {}}
    query = (query or "")[:cfg["search_query_max_chars"]]
    if not query.strip():
        return empty
    limit = cfg["search_result_limit"]
    k = min(4096, 3 * limit)
    vmin = cfg["vector_min_cos"]
    now = datetime.now(timezone.utc)

    cands = {}
    qvec = None
    try:
        qvec = embed_query(query, cfg.get("embed_model"))
        for r in search_vector(conn, qvec, k, since, until):
            c = dict(r)
            c.update(cos=cosine_sim_from_l2(r["distance"]), kw=0.0, fts_hit=False)
            cands[r["rowid"]] = c
    except Exception as e:
        _warn(f"Warning: vector search degraded (embed_query failed: {e})")

    # Experimental settings (2026-11-20, eval P2 / P3; not in the spec). The defaults keep the
    # spec's behaviour.
    stop = bool(cfg.get("fts_stopwords", False))
    norm_floor = float(cfg.get("kw_norm_floor", 1.0))
    min_terms = int(cfg.get("kw_only_min_terms", 0))
    q_terms = (fts_terms(query, stop), bigram_terms(query, stop)) if min_terms > 1 else None

    tiny = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] <= 2
    for fn in (search_fts, search_fts_bigram):
        try:
            rows = fn(conn, query, k, since, until, stopwords=stop)
        except Exception as e:
            _warn(f"Warning: keyword search failed ({fn.__name__}: {e})")
            continue
        for rowid, rel in _kw_map(rows, cfg["bm25_min_threshold"], tiny, norm_floor).items():
            if rowid in cands:
                cands[rowid]["kw"] = max(cands[rowid]["kw"], rel)
                cands[rowid]["fts_hit"] = True
            else:
                row = next(r for r in rows if r["rowid"] == rowid)
                c = dict(row)
                c.update(cos=0.0, kw=rel, fts_hit=True)
                cands[rowid] = c

    results = []
    for c in cands.values():
        if not (c["cos"] >= vmin or c["fts_hit"]):
            continue
        if q_terms and c["cos"] < vmin and _matched_terms(c["content"], q_terms) < min_terms:
            continue  # keyword-only and too few terms: stays a weak-match candidate
        used_cos = c["cos"] if c["cos"] >= vmin else 0.0
        fs = used_cos * 0.7 + c["kw"] * 0.3
        if fs < cfg["min_score"]:
            continue
        decay = time_decay(c["timestamp"], cfg["half_life_days"], now)
        score = fs * decay * session_bias(c.get("session_id"), session_id)
        results.append(_result_row(c, fs, score, used_cos, c["kw"]))
    results.sort(key=lambda r: (r["score"], r["timestamp"], r["id"]), reverse=True)

    if cfg["reply_sim_rerank_enabled"]:
        results = _rerank_by_reply_sim(conn, results, qvec, cfg)
    if cfg["version_order_enabled"]:
        try:
            results = _order_versions_newest_first(conn, results, cfg)
        except Exception as e:
            _warn(f"Warning: version ordering skipped ({e})")
    if results and cfg["relative_min_score_ratio"] > 0:
        top = max(r["filter_score"] for r in results)
        results = [r for r in results
                   if r.get("_version_group") or r["filter_score"] >= top * cfg["relative_min_score_ratio"]]
    results = _collapse_identical_content(results)

    # Experimental (2026-11-20, not in the spec): "top_matches_max" caps the strong rows only.
    # 0 (the default) means no cap of its own: search_result_limit applies as before.
    strong_cap = min(limit, int(cfg.get("top_matches_max", 0)) or limit)
    capped, per_turn = [], {}
    for r in results:
        tid = r.get("turn_id")
        if tid:
            if per_turn.get(tid, 0) >= 2:
                continue
            per_turn[tid] = per_turn.get(tid, 0) + 1
        capped.append(r)
        if len(capped) >= strong_cap:
            break
    results = capped

    target = min(cfg["min_results"], limit)
    if target > 0 and len(results) < target:
        used_rowids = {r["rowid"] for r in results}
        seen_content = {r["content"] for r in results}
        pool = []
        for c in cands.values():
            if c["rowid"] in used_rowids:
                continue
            fs = c["cos"] * 0.7 + c["kw"] * 0.3
            if fs < 1e-6:
                continue
            pool.append((fs, c["timestamp"], c["id"], c))
        pool.sort(key=lambda t: t[:3], reverse=True)
        for fs, _ts, _id, c in pool:
            if len(results) >= target:
                break
            tid = c.get("turn_id")
            if c["content"] in seen_content or (tid and per_turn.get(tid, 0) >= 2):
                continue
            decay = time_decay(c["timestamp"], cfg["half_life_days"], now)
            score = fs * decay * session_bias(c.get("session_id"), session_id)
            results.append(_result_row(c, fs, score, c["cos"], c["kw"], weak=True))
            seen_content.add(c["content"])
            if tid:
                per_turn[tid] = per_turn.get(tid, 0) + 1

    pairs, omitted, total = {}, {}, 0
    tids = []
    for r in results:
        if r.get("weak"):
            continue
        tid = r.get("turn_id")
        if tid and tid not in tids:
            tids.append(tid)
        if len(tids) >= cfg["pair_top_n"]:
            break
    stop = False
    for tid in tids:
        if stop:
            break
        rows = get_memories_by_turn_id(conn, tid)
        kept = []
        for idx, row in enumerate(rows):
            if total + len(row["content"]) > cfg["pair_max_chars"]:
                omitted[tid] = len(rows) - idx
                stop = True
                break
            kept.append({"id": row["id"], "content": row["content"], "timestamp": row["timestamp"]})
            total += len(row["content"])
        if kept:
            pairs[tid] = kept
    return {"results": results, "pairs": pairs, "pairs_omitted": omitted}


# -------------------------------------------------------------------- rendering

def _fmt_saved(ts):
    dt = parse_timestamp(ts or "")
    if dt is None:
        return ts or ""
    local = dt.astimezone()
    return f"{local.strftime('%Y-%m-%d %H:%M')} (UTC{local.strftime('%z')})"


def render_memory_context(res, query="", notices=()):
    """Markdown block printed to stdout by the UserPromptSubmit hook."""
    lines = ["# Recalled Memory Context"]
    if query:
        lines.append(f"Search query: {query}")
    lines.append("")
    for n in notices:
        lines += [f"> ⚠️ N3MC notice: {n}", ""]
    results = res.get("results", [])
    strong = [r for r in results if not r.get("weak")]
    weak = [r for r in results if r.get("weak")]
    if not strong and not weak:
        lines.append("_No relevant memories found._")
        return "\n".join(lines) + "\n"
    n = 1
    if strong:
        lines += ["## Top matches (use these to answer the question)", "",
                  "_If several versions of the same content appear, the one with the newest "
                  "`saved` time is the current one._", ""]
        for r in strong:
            lines += [f"## [{n}] score={r['score']:.4f} saved={_fmt_saved(r['timestamp'])} "
                      f"{r.get('agent_name') or '-'}", r["content"], ""]
            n += 1
    else:
        lines += ["_No relevant memories found._", ""]
    if weak:
        lines += ["## Possibly related (weak matches — may be unrelated)", ""]
        for r in weak:
            lines += [f"## [{n}] score={r['score']:.4f} saved={_fmt_saved(r['timestamp'])} "
                      f"{r.get('agent_name') or '-'}", r["content"], ""]
            n += 1
    pairs, omitted = res.get("pairs", {}), res.get("pairs_omitted", {})
    if pairs or omitted:
        lines += ["## Previous matching Q-A exchanges (supplementary context)", ""]
        first_ts = {r["turn_id"]: r["timestamp"] for r in reversed(results) if r.get("turn_id")}
        for tid in list(pairs) + [t for t in omitted if t not in pairs]:
            rows = pairs.get(tid, [])
            ts = rows[0]["timestamp"] if rows else first_ts.get(tid, "")
            lines.append(f"### turn_id: {tid} saved={_fmt_saved(ts)}")
            for row in rows:
                lines += [row["content"], ""]
            if tid in omitted:
                lines += [f"_({omitted[tid]} more chunk(s) of this exchange omitted. Full text: "
                          f"`python -m n3memorycore.n3memory --show-turn {tid}`)_", ""]
    return "\n".join(lines).rstrip("\n") + "\n"
