"""
N3MemoryCore - processor.py
Processing layer: embedding generation, ranking calculation, text purification
"""
import os
import re
import sys
from datetime import datetime, timezone
from math import pow, log2
from typing import Optional

# Required: explicit sys.path configuration for inter-module imports
sys.path.insert(0, os.path.dirname(__file__))

from database import (
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
    strip_fts_punctuation,
    get_memory_by_rowid,
)

# ---------------------------------------------------------------------------
# Embedding model (lazy-loaded singleton)
# ---------------------------------------------------------------------------
_model = None
_MODEL_NAME = "intfloat/e5-base-v2"
_VECTOR_DIM = 768


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed_passage(text: str) -> list:
    """Embed text for storage (document). Adds 'passage: ' prefix."""
    model = get_model()
    vec = model.encode("passage: " + text, normalize_embeddings=True)
    return vec.tolist()


def embed_query(text: str) -> list:
    """Embed text for search (query). Adds 'query: ' prefix."""
    model = get_model()
    vec = model.encode("query: " + text, normalize_embeddings=True)
    return vec.tolist()


# ---------------------------------------------------------------------------
# Text purification
# ---------------------------------------------------------------------------
_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```', re.MULTILINE)


def purify(text: str) -> str:
    """Replace multi-line code blocks with '[code omitted]'. Keep inline code as-is."""
    return _CODE_BLOCK_RE.sub('[code omitted]', text)


# ---------------------------------------------------------------------------
# Ranking formula components
# ---------------------------------------------------------------------------
def cosine_sim_from_l2(l2_distance: float) -> float:
    """
    Convert L2 distance to cosine similarity for L2-normalized vectors.
    cos_sim = max(0, 1.0 - L2_distance^2 / 2)
    """
    return max(0.0, 1.0 - (l2_distance ** 2) / 2.0)


def time_decay(timestamp_str: str, half_life_days: float) -> float:
    """
    time_decay = 2^(-days_elapsed / half_life_days)
    Returns 1.0 on invalid timestamp.
    """
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        days_elapsed = (now - ts).total_seconds() / 86400.0
        return pow(2.0, -days_elapsed / half_life_days)
    except Exception:
        return 1.0


def keyword_relevance(bm25_score: float, max_abs_score: float, bm25_min_threshold: float) -> float:
    """
    Normalize FTS5 BM25 score (negative) to [0.0, 1.0].
    """
    abs_score = abs(bm25_score)
    if abs_score < bm25_min_threshold:
        return 0.0
    return abs_score / max(1.0, max_abs_score)


def final_score(
    cos_sim: float,
    kw_relevance: float,
    decay: float,
    b_local: float = 1.0,
) -> float:
    """
    Final Score = (cos_sim * 0.7 + keyword_relevance * 0.3) * time_decay * b_local
    Note: b_local bias is a Pro feature; Free always passes 1.0.
    """
    return (cos_sim * 0.7 + kw_relevance * 0.3) * decay * b_local


# ---------------------------------------------------------------------------
# Hybrid search (vector + FTS)
# ---------------------------------------------------------------------------
def hybrid_search(
    conn,
    query: str,
    config: dict,
    k: int = 50,
) -> list:
    """
    Perform hybrid vector + FTS search and return ranked results.
    Returns list of dicts: {id, content, score, timestamp}
    """
    half_life_days = config.get("half_life_days", 90)
    bm25_min_threshold = config.get("bm25_min_threshold", 0.1)
    search_result_limit = config.get("search_result_limit", 20)
    min_score = config.get("min_score", 0.2)
    local_id = config.get("local_id", "")

    # Vector search
    query_vec = embed_query(query)
    vec_results = search_vector(conn, query_vec, k=k)
    # {rowid: l2_distance}
    vec_map = {rowid: dist for rowid, dist in vec_results}

    # FTS search
    fts_results = search_fts(conn, query, limit=k)
    fts_map = {rowid: score for rowid, score in fts_results}

    # Normalize BM25
    if fts_map:
        max_abs_bm25 = max(abs(s) for s in fts_map.values())
    else:
        max_abs_bm25 = 1.0

    # Merge all rowids
    all_rowids = set(vec_map.keys()) | set(fts_map.keys())

    results = []
    for rowid in all_rowids:
        row = get_memory_by_rowid(conn, rowid)
        if row is None:
            continue

        # cos_sim
        if rowid in vec_map:
            cos = cosine_sim_from_l2(vec_map[rowid])
        else:
            cos = 0.0

        # keyword_relevance
        if rowid in fts_map:
            kw = keyword_relevance(fts_map[rowid], max_abs_bm25, bm25_min_threshold)
        else:
            kw = 0.0

        # time decay (sqlite3.Row supports dict-style access)
        ts = row["timestamp"]
        decay = time_decay(ts, half_life_days)

        # local bias (Free: always 1.0)
        b_local = 1.0

        score = final_score(cos, kw, decay, b_local)

        if score < min_score:
            continue

        content = row["content"]
        rec_id = row["id"]

        results.append({
            "id": rec_id,
            "content": content,
            "score": round(score, 4),
            "timestamp": ts,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:search_result_limit]
