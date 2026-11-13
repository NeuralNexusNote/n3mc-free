import os
import re
import sys
import logging
from datetime import datetime, timezone
from typing import Optional

from .database import (
    get_connection,
    init_db,
    insert_memory,
    search_vector,
    search_vector_with_filter,
    search_fts,
    get_all_memories,
    delete_memory,
    count_memories,
    check_exact_duplicate,
    find_unindexed_memories,
    serialize_vector,
    get_memories_by_turn_id,
    get_vector,
    strip_fts_punctuation,
)

logger = logging.getLogger(__name__)

# spec §5: closed fenced code blocks → [code omitted]; inline backticks preserved
_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```')

# Windows cp932 guard: strip lone UTF-16 surrogate halves
_LONE_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')


def sanitize_surrogates(text):
    """Strip lone UTF-16 surrogate halves. Recursively cleans dicts/lists."""
    if text is None:
        return text
    if isinstance(text, str):
        return _LONE_SURROGATE_RE.sub('', text)
    if isinstance(text, list):
        return [sanitize_surrogates(x) for x in text]
    if isinstance(text, dict):
        return {k: sanitize_surrogates(v) for k, v in text.items()}
    return text


DEFAULT_EMBED_MODEL = 'intfloat/multilingual-e5-base'

_model = None
_model_name = None


def get_model(name: Optional[str] = None):
    """Lazy-load and cache the sentence-transformers embedding model.

    Priority: name arg → $N3MC_EMBED_MODEL → DEFAULT_EMBED_MODEL.
    """
    global _model, _model_name
    if name is None and _model is not None:
        return _model
    target = name or os.environ.get('N3MC_EMBED_MODEL') or DEFAULT_EMBED_MODEL
    if _model is None or _model_name != target:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(target)
        _model_name = target
    return _model


def embed_passage(text: str) -> list:
    model = get_model()
    vec = model.encode("passage: " + text, normalize_embeddings=True)
    return vec.tolist()


def embed_query(text: str) -> list:
    model = get_model()
    vec = model.encode("query: " + text, normalize_embeddings=True)
    return vec.tolist()


def purify_text(text: str) -> str:
    """Replace closed fenced code blocks with [code omitted]; strip lone surrogates."""
    if not text:
        return text
    cleaned = sanitize_surrogates(text)
    return _CODE_BLOCK_RE.sub('[code omitted]', cleaned)


def chunk_text(text: str, max_chars: int = 400, overlap: int = 40) -> list:
    if not text or not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]

    # Paragraph split (highest priority)
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    if len(paragraphs) > 1:
        chunks = _merge_chunks(paragraphs, max_chars, overlap, sep='\n\n')
        if chunks:
            return chunks

    # Sentence split
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) > 1:
        chunks = _merge_chunks(sentences, max_chars, overlap, sep=' ')
        if chunks:
            return chunks

    # Hard window fallback
    return _hard_window(text, max_chars, overlap)


def _merge_chunks(parts: list, max_chars: int, overlap: int, sep: str) -> list:
    chunks = []
    current = ""
    for part in parts:
        if len(part) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            sub = _hard_window(part, max_chars, overlap)
            chunks.extend(sub)
        elif current and len(current) + len(sep) + len(part) > max_chars:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = (tail + sep + part) if tail else part
        else:
            current = (current + sep + part) if current else part
    if current:
        chunks.append(current)
    return chunks


def _hard_window(text: str, max_chars: int, overlap: int) -> list:
    chunks = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        chunks.append(text[start:end])
        if end >= length:
            break
        start = end - overlap
        if start <= 0:
            start = end
    return chunks


def add_chunk_prefixes(chunks: list, role: str) -> list:
    if not chunks:
        return []
    n = len(chunks)
    if n == 1:
        return [f"[{role}] {chunks[0]}"]
    return [f"[{role} {i + 1}/{n}] {chunks[i]}" for i in range(n)]


def cosine_sim_from_l2(l2_distance: float) -> float:
    """spec §3: cos_sim = max(0, 1 - L2²/2) for L2-normalized vectors."""
    return max(0.0, 1.0 - (l2_distance ** 2) / 2.0)


def time_decay(timestamp_str: str, half_life_days: float) -> float:
    """spec §3: time_decay = 2^(-days_elapsed / half_life_days)."""
    try:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_elapsed = (now - ts).total_seconds() / 86400.0
        return 2.0 ** (-days_elapsed / half_life_days)
    except Exception:
        return 1.0


def keyword_relevance(
    bm25_score: float, max_abs_bm25: float, bm25_min_threshold: float
) -> float:
    """spec §3: normalize BM25 score to [0, 1]."""
    abs_score = abs(bm25_score)
    if abs_score < bm25_min_threshold:
        return 0.0
    return abs_score / max(1.0, max_abs_bm25)


def _turn_key(conn, turn_id: str):
    """(timestamp, id) of the first row of a turn — newest-first sort key."""
    row = conn.execute(
        "SELECT timestamp, id FROM memories WHERE turn_id = ? ORDER BY rowid LIMIT 1",
        (turn_id,),
    ).fetchone()
    return (row['timestamp'], row['id']) if row else ('', '')


def _turn_representative(conn, turn_id: str, min_chars: int) -> Optional[list]:
    """Embedding of the turn's first [claude chunk with >= min_chars chars."""
    row = conn.execute(
        """SELECT rowid FROM memories
           WHERE turn_id = ? AND content LIKE '[claude%' AND length(content) >= ?
           ORDER BY rowid LIMIT 1""",
        (turn_id, min_chars),
    ).fetchone()
    if not row:
        return None
    try:
        return get_vector(conn, row['rowid'])
    except Exception:
        return None


def _order_versions_newest_first(conn, scored: list, config: dict) -> list:
    """spec §5 (v1.4.0): put the newest version of the same content first.

    Turns whose representative chunks (first [claude chunk >= version_min_chars)
    have cosine >= version_sim_threshold are versions of the same content.
    Within each group, turns are re-ordered newest first; every result
    position keeps its score (the newest version inherits the best score).
    Rows are never deleted. Limits: turns that only share their opening are
    also grouped; a new version whose opening chunk was not rewritten is
    grouped, one whose opening was rewritten is not detected.
    """
    thr = config.get('version_sim_threshold', 0.98)
    min_chars = config.get('version_min_chars', 100)

    turn_ids = []
    for r in scored:
        tid = r.get('turn_id')
        if tid and tid not in turn_ids:
            turn_ids.append(tid)
    if len(turn_ids) < 2:
        return scored

    reps = {}
    for tid in turn_ids:
        v = _turn_representative(conn, tid, min_chars)
        if v is not None:
            reps[tid] = v
    rep_ids = list(reps)
    if len(rep_ids) < 2:
        return scored

    parent = {t: t for t in rep_ids}

    def find(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    for i in range(len(rep_ids)):
        for j in range(i + 1, len(rep_ids)):
            a, b = reps[rep_ids[i]], reps[rep_ids[j]]
            if sum(x * y for x, y in zip(a, b)) >= thr:
                parent[find(rep_ids[i])] = find(rep_ids[j])

    groups: dict = {}
    for t in rep_ids:
        groups.setdefault(find(t), []).append(t)

    out = list(scored)
    gid = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        gid += 1
        member_set = set(members)
        positions = [i for i, r in enumerate(out) if r.get('turn_id') in member_set]
        slot_scores = [out[i]['score'] for i in positions]
        items = [out[i] for i in positions]
        ordered_turns = sorted(members, key=lambda t: _turn_key(conn, t), reverse=True)
        new_items = [it for t in ordered_turns for it in items if it.get('turn_id') == t]
        for pos, score, item in zip(positions, slot_scores, new_items):
            new = dict(item)
            new['score'] = score
            new['_version_group'] = gid
            out[pos] = new
    return out


def _collapse_identical_content(results: list) -> list:
    """spec §5 (v1.4.0): one row per identical content, at the highest
    position, carrying the newest record's id/timestamp/turn_id."""
    out: list = []
    index: dict = {}
    for r in results:
        c = r.get('content')
        if c not in index:
            index[c] = len(out)
            out.append(dict(r))
            continue
        kept = out[index[c]]
        newer = (r.get('timestamp') or '', r.get('id') or '')
        current = (kept.get('timestamp') or '', kept.get('id') or '')
        if newer > current:
            # spec: the payload (id, timestamp, turn_id) comes from the newest
            for k in ('id', 'timestamp', 'turn_id'):
                kept[k] = r.get(k)
    return out


def hybrid_search(
    db_path: str,
    query: str,
    config: dict,
    current_session_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    half_life = config.get('half_life_days', 90)
    bm25_min = config.get('bm25_min_threshold', 0.1)
    limit = config.get('search_result_limit', 20)
    min_score = config.get('min_score', 0.2)
    max_query = config.get('search_query_max_chars', 2000)
    min_cos = config.get('vector_min_cos', 0.85)
    rel_ratio = config.get('relative_min_score_ratio', 0.6)
    pair_top_n = config.get('pair_top_n', 3)
    pair_max_chars = config.get('pair_max_chars', 3000)

    query = (query or '')[:max_query]
    if not query.strip():
        return {'results': [], 'pairs': {}, 'pairs_omitted': {}}

    conn = get_connection(db_path)
    try:
        # 1. Vector search; drop candidates below vector_min_cos
        vec_results = {}
        try:
            q_vec = embed_query(query)
            if since or until:
                rows = search_vector_with_filter(conn, q_vec, k=limit * 3,
                                                 since=since, until=until)
            else:
                rows = search_vector(conn, q_vec, k=limit * 3)
            for row in rows:
                cs = cosine_sim_from_l2(row['distance'])
                if cs < min_cos:
                    continue
                vec_results[row['id']] = {
                    'id':         row['id'],
                    'content':    row['content'],
                    'timestamp':  row['timestamp'],
                    'session_id': row['session_id'],
                    'turn_id':    row['turn_id'],
                    'agent_name': row['agent_name'],
                    'cos_sim':    cs,
                }
        except Exception as e:
            # spec §5: warn on stderr; continue with BM25 only
            print(
                f"Warning: vector search degraded (embed_query failed: {e})",
                file=sys.stderr,
            )

        # 2. FTS search — an FTS failure must not fail the whole search
        fts_results = {}
        try:
            rows = search_fts(conn, query, limit=limit * 3,
                              since=since, until=until)
            for row in rows:
                fts_results[row['id']] = {
                    'id':         row['id'],
                    'content':    row['content'],
                    'timestamp':  row['timestamp'],
                    'session_id': row['session_id'],
                    'turn_id':    row['turn_id'],
                    'agent_name': row['agent_name'],
                    'bm25_score': row['bm25_score'],
                }
        except Exception as e:
            logger.warning(f"FTS search error (continuing with vectors): {e}")

        fts_max_abs = max((abs(v['bm25_score']) for v in fts_results.values()),
                          default=0.0)

        # 3. Score + min_score
        scored = []
        for rid in set(vec_results) | set(fts_results):
            vec_r = vec_results.get(rid)
            fts_r = fts_results.get(rid)
            meta = vec_r or fts_r
            cs = vec_r['cos_sim'] if vec_r else 0.0
            bm25 = fts_r['bm25_score'] if fts_r else 0.0
            kr = keyword_relevance(bm25, fts_max_abs, bm25_min)
            decay = time_decay(meta['timestamp'], half_life)
            sess = meta.get('session_id')
            # spec §3: b_session = 1.0 on match, 0.6 on mismatch/NULL
            b_session = 1.0 if (current_session_id and sess == current_session_id) else 0.6
            score = (cs * 0.7 + kr * 0.3) * decay * b_session
            if score < min_score:
                continue
            scored.append({
                'id':                rid,
                'content':           meta['content'],
                'timestamp':         meta['timestamp'],
                'session_id':        sess,
                'turn_id':           meta.get('turn_id'),
                'agent_name':        meta.get('agent_name'),
                'score':             score,
                'cos_sim':           cs,
                'keyword_relevance': kr,
            })
        scored.sort(key=lambda x: (x['score'], x['timestamp'] or '', x['id']),
                    reverse=True)

        # 4. Newest version of the same content first
        if scored and config.get('version_order_enabled', True):
            try:
                scored = _order_versions_newest_first(conn, scored, config)
            except Exception as e:
                logger.warning(f"Version ordering skipped: {e}")

        # 5. Relative cut (rows re-ordered as versions are kept)
        if scored and rel_ratio:
            top = scored[0]['score']
            scored = [r for r in scored
                      if r['score'] >= top * rel_ratio or r.get('_version_group')]

        # 6. Identical content → one row
        scored = _collapse_identical_content(scored)

        # 7. Max 2 chunks per turn_id, then the result limit
        results = []
        turn_id_counts: dict = {}
        for r in scored:
            tid = r.get('turn_id')
            if tid is not None:
                count = turn_id_counts.get(tid, 0)
                if count >= 2:
                    continue
                turn_id_counts[tid] = count + 1
            results.append(r)
            if len(results) >= limit:
                break

        # 8. Q-A pairs: first pair_top_n turns, pair_max_chars in total
        pairs: dict = {}
        pairs_omitted: dict = {}
        used = 0
        for r in results:
            tid = r.get('turn_id')
            if not tid or tid in pairs or tid in pairs_omitted:
                continue
            if len(pairs) >= pair_top_n:
                break
            siblings = get_memories_by_turn_id(conn, tid)
            kept = []
            cut = False
            for idx, s in enumerate(siblings):
                if used + len(s['content']) > pair_max_chars:
                    pairs_omitted[tid] = len(siblings) - idx
                    cut = True
                    break
                used += len(s['content'])
                kept.append({'id': s['id'], 'content': s['content'],
                             'timestamp': s['timestamp']})
            if kept:
                pairs[tid] = kept
            # (an exchange whose first row already exceeds the budget is listed
            # only in pairs_omitted; the renderer prints its omission line)
            if cut:
                break

        return {'results': results, 'pairs': pairs, 'pairs_omitted': pairs_omitted}
    finally:
        conn.close()


def _fmt_saved(ts: Optional[str]) -> str:
    """spec §5: DB timestamps are UTC; show local time with the offset."""
    if not ts:
        return '?'
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return f"{local:%Y-%m-%d %H:%M} (UTC{local:%z})"
    except Exception:
        return ts


SHOW_TURN_CMD = 'python -m n3memorycore.n3memory --show-turn'
VERSION_NOTE = ("_If several versions of the same content appear, the one with "
                "the newest `saved` time is the current one._")


def render_memory_context(search_result: dict, query: str,
                          notices: Optional[list] = None) -> str:
    """spec §5 v1.4.0 rendering: notices → Top matches → Q-A pairs."""
    results = search_result.get('results', [])
    pairs = search_result.get('pairs', {})
    omitted = search_result.get('pairs_omitted', {}) or {}

    lines = ["# Recalled Memory Context", f"Search query: {query}", ""]
    for n in notices or []:
        lines.append(f"> ⚠️ N3MC notice: {n}")
        lines.append("")

    if not results:
        lines.append("_No relevant memories found._")
        lines.append("")
    else:
        lines.append("## Top matches (use these to answer the question)")
        lines.append("")
        lines.append(VERSION_NOTE)
        lines.append("")
        for i, r in enumerate(results, 1):
            agent = r.get('agent_name') or '-'
            lines.append(f"## [{i}] score={r['score']:.4f} "
                         f"saved={_fmt_saved(r.get('timestamp'))} {agent}")
            lines.append(r['content'])
            lines.append("")

    exchanges = list(pairs.items())
    # An exchange cut before its first row appears only in pairs_omitted.
    exchanges += [(tid, []) for tid in omitted if tid not in pairs]
    if exchanges:
        lines.append("## Previous matching Q-A exchanges (supplementary context)")
        lines.append("")
        for tid, siblings in exchanges:
            if siblings:
                first_ts = siblings[0].get('timestamp')
                lines.append(f"### turn_id: {tid} saved={_fmt_saved(first_ts)}")
            else:
                lines.append(f"### turn_id: {tid}")
            for s in siblings:
                lines.append(s.get('content', '[content missing]'))
            n = omitted.get(tid)
            if n:
                lines.append(f"_({n} more chunk(s) of this exchange omitted. "
                             f"Full text: `{SHOW_TURN_CMD} {tid}`)_")
            lines.append("")

    return '\n'.join(lines)
