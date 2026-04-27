import os
import re
import math
import logging
from datetime import datetime, timezone
from typing import Optional

from .database import (
    get_connection,
    init_db,
    insert_memory,
    search_vector,
    search_fts,
    get_all_memories,
    delete_memory,
    count_memories,
    check_exact_duplicate,
    find_unindexed_memories,
    serialize_vector,
    get_memories_by_turn_id,
    strip_fts_punctuation,
)

logger = logging.getLogger(__name__)

_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```')

# ---------------------------------------------------------------------------
# Lone-surrogate sanitization (Windows / cp932 data-loss guard)
# ---------------------------------------------------------------------------
# Subprocess stdin pipes on Windows can deliver UTF-8 bytes that python's
# decoder maps to lone UTF-16 surrogate halves (U+D800..U+DFFF). Such strings
# round-trip through json.loads but blow up at sqlite3.execute() with
# UnicodeEncodeError, silently dropping the entire write — a direct violation
# of the complete-preservation contract. We strip these halves before any
# value is bound to SQLite.
_LONE_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')


def sanitize_surrogates(text):
    """Strip lone UTF-16 surrogate halves from a string (or pass-through).

    Recursively cleans dicts / lists too so audit-log JSON payloads with
    surrogates buried inside multimodal content do not break json.dumps.
    """
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
    """Lazy-load and cache the embedding model.

    On the first call (or when `name` differs from the cached model),
    the model is selected from (in priority order):
      1. `name` argument
      2. `$N3MC_EMBED_MODEL` environment variable
      3. `DEFAULT_EMBED_MODEL` (multilingual baseline)

    Subsequent calls with `name=None` return the cached model without
    re-resolving — so the model loaded by the server lifespan (with
    `embed_model` from config.json) wins, and `embed_passage` /
    `embed_query` reuse it transparently.

    Callers that want to switch to a language-specialised model should
    set `embed_model` in `config.json` and run `--repair` once after
    the first save under the new model (see spec §5 vector re-index
    migration).
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
    prefixed = "passage: " + text
    vec = model.encode(prefixed, normalize_embeddings=True)
    return vec.tolist()


def embed_query(text: str) -> list:
    model = get_model()
    prefixed = "query: " + text
    vec = model.encode(prefixed, normalize_embeddings=True)
    return vec.tolist()


def purify_text(text: str) -> str:
    """Replace closed fenced code blocks with [code omitted] AND strip lone
    surrogates as a defense-in-depth layer (spec §5).

    Inline backtick spans and unclosed fences are preserved verbatim.
    """
    if not text:
        return text
    cleaned = sanitize_surrogates(text)
    return _CODE_BLOCK_RE.sub('[code omitted]', cleaned)


def chunk_text(text: str, max_chars: int = 400, overlap: int = 40) -> list:
    if not text or not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]

    # Paragraph split
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
            # Overlap: keep tail of previous chunk
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
    return max(0.0, 1.0 - (l2_distance ** 2) / 2.0)


def time_decay(timestamp_str: str, half_life_days: float) -> float:
    try:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_elapsed = (now - ts).total_seconds() / 86400.0
        return 2.0 ** (-days_elapsed / half_life_days)
    except Exception:
        return 1.0


def keyword_relevance(bm25_score: float, max_abs_bm25: float, bm25_min_threshold: float) -> float:
    abs_score = abs(bm25_score)
    if abs_score < bm25_min_threshold:
        return 0.0
    return abs_score / max(1.0, max_abs_bm25)


def hybrid_search(
    db_path: str,
    query: str,
    config: dict,
    current_session_id: Optional[str] = None,
) -> dict:
    half_life = config.get('half_life_days', 90)
    bm25_min = config.get('bm25_min_threshold', 0.1)
    limit = config.get('search_result_limit', 20)
    min_score = config.get('min_score', 0.2)
    max_query = config.get('search_query_max_chars', 2000)

    query = query[:max_query]

    conn = get_connection(db_path)
    try:
        # Vector search
        vec_results = {}
        if query.strip():
            try:
                q_vec = embed_query(query)
                rows = search_vector(conn, q_vec, k=limit * 3)
                for row in rows:
                    rid = row['id']
                    dist = row['distance']
                    cs = cosine_sim_from_l2(dist)
                    vec_results[rid] = {
                        'id': rid,
                        'content': row['content'],
                        'timestamp': row['timestamp'],
                        'session_id': row['session_id'],
                        'turn_id': row['turn_id'],
                        'agent_name': row['agent_name'],
                        'cos_sim': cs,
                        'bm25_score': None,
                    }
            except Exception as e:
                logger.warning(f"Vector search error: {e}")

        # FTS search
        fts_results = {}
        if query.strip():
            try:
                rows = search_fts(conn, query, limit=limit * 3)
                max_abs = max((abs(r['bm25_score']) for r in rows), default=0.0)
                for row in rows:
                    rid = row['id']
                    fts_results[rid] = {
                        'id': rid,
                        'content': row['content'],
                        'timestamp': row['timestamp'],
                        'session_id': row['session_id'],
                        'turn_id': row['turn_id'],
                        'agent_name': row['agent_name'],
                        'bm25_score': row['bm25_score'],
                        'max_abs_bm25': max_abs,
                        'cos_sim': None,
                    }
            except Exception as e:
                logger.warning(f"FTS search error: {e}")

        # Combine
        all_ids = set(vec_results.keys()) | set(fts_results.keys())
        scored = []
        fts_max_abs = 0.0
        if fts_results:
            fts_max_abs = max(abs(v['bm25_score']) for v in fts_results.values())

        for rid in all_ids:
            vec_r = vec_results.get(rid)
            fts_r = fts_results.get(rid)

            meta = vec_r or fts_r
            cs = vec_r['cos_sim'] if vec_r else 0.0
            bm25 = fts_r['bm25_score'] if fts_r else 0.0
            kr = keyword_relevance(bm25, fts_max_abs, bm25_min)

            ts = meta['timestamp']
            decay = time_decay(ts, half_life)

            sess = meta.get('session_id')
            b_session = 1.0 if (current_session_id and sess == current_session_id) else 0.6

            score = (cs * 0.7 + kr * 0.3) * decay * b_session

            scored.append({
                'id': rid,
                'content': meta['content'],
                'timestamp': ts,
                'session_id': sess,
                'turn_id': meta.get('turn_id'),
                'agent_name': meta.get('agent_name'),
                'score': score,
                'cos_sim': cs,
                'keyword_relevance': kr,
            })

        scored.sort(key=lambda x: x['score'], reverse=True)
        filtered = [r for r in scored if r['score'] >= min_score]
        results = filtered[:limit]

        # Q-A pair collection
        pairs = {}
        seen_turn_ids = set()
        for r in results:
            tid = r.get('turn_id')
            if tid and tid not in seen_turn_ids:
                seen_turn_ids.add(tid)
                siblings = get_memories_by_turn_id(conn, tid)
                if siblings:
                    pairs[tid] = [
                        {
                            'id': s['id'],
                            'content': s['content'],
                            'timestamp': s['timestamp'],
                        }
                        for s in siblings
                    ]

        return {'results': results, 'pairs': pairs}
    finally:
        conn.close()


def render_memory_context(search_result: dict, query: str) -> str:
    results = search_result.get('results', [])
    pairs = search_result.get('pairs', {})

    lines = [f"# Recalled Memory Context\n検索クエリ: {query}\n"]

    if not results:
        lines.append("_No relevant memories found._\n")
    else:
        lines.append("## Top matches (use these to answer the question)\n")
        for i, r in enumerate(results, 1):
            score = r['score']
            content = r['content']
            lines.append(f"### [{i}] score={score:.4f}")
            lines.append(content)
            lines.append("")

    if pairs:
        lines.append("\n## Previous matching Q-A exchanges (supplementary context)\n")
        for tid, siblings in pairs.items():
            lines.append(f"**Turn:** `{tid}`\n")
            for s in siblings:
                lines.append(s.get('content', '[content missing]'))
                lines.append("")
            lines.append("---")

    return '\n'.join(lines)
