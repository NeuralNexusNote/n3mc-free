"""N3MemoryCore — processing layer.

Embeddings, ranking math, fenced-code substitution, and chunking.
See N3MemoryCore_v1.2.0_Free_EN.md §3 (Ranking Formula) and §5 (chunking).
"""
from __future__ import annotations

import math
import os
import re
import sys
from datetime import datetime, timezone
from typing import Iterable, Optional

# Spec §3: explicit sys.path so the package works regardless of CWD.
sys.path.insert(0, os.path.dirname(__file__))

from database import (  # noqa: E402  (intentional after sys.path tweak)
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
)


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
    surrogates buried inside multimodal content don't break json.dumps.
    """
    if text is None:
        return text
    if isinstance(text, str):
        return _LONE_SURROGATE_RE.sub("", text)
    if isinstance(text, list):
        return [sanitize_surrogates(x) for x in text]
    if isinstance(text, dict):
        return {k: sanitize_surrogates(v) for k, v in text.items()}
    return text


_sanitize_surrogates = sanitize_surrogates  # snake-case alias used internally


# ---------------------------------------------------------------------------
# Purification — fenced code blocks only (spec §5) + surrogate sanitization
# ---------------------------------------------------------------------------
_CODE_BLOCK_RE = re.compile(r'```[^\n]*\n.*?\n```', re.DOTALL)


def purify_text(text):
    """Replace closed fenced code blocks with `[code omitted]` AND strip lone
    surrogates as a defense-in-depth layer (spec §5).

    Inline backtick spans and unclosed fences are preserved verbatim.
    """
    if not text:
        return text
    cleaned = sanitize_surrogates(text)
    return _CODE_BLOCK_RE.sub("[code omitted]", cleaned)


# Public alias used by hooks.
_purify = purify_text


# ---------------------------------------------------------------------------
# Chunking — paragraph → sentence → hard window (spec §5)
# ---------------------------------------------------------------------------
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?。！？])\s+')


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r'\n{2,}', text)
    return [p.strip() for p in parts if p.strip()]


def _split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _hard_window(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    step = max(1, max_chars - overlap)
    out = []
    i = 0
    while i < len(text):
        out.append(text[i : i + max_chars])
        if i + max_chars >= len(text):
            break
        i += step
    return out


def chunk_text(text: str, max_chars: int = 400, overlap: int = 40) -> list[str]:
    """Hierarchical chunker used by the auto-save hooks.

    1. Try paragraph splits (\\n\\n).
    2. Within an oversized paragraph, fall back to sentence splits.
    3. Within an oversized sentence, fall back to a hard sliding window.

    Aggregates adjacent small pieces up to ~max_chars to reduce record bloat.
    """
    if not text:
        return []
    text = text.rstrip()
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    for para in _split_paragraphs(text):
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        for sent in _split_sentences(para):
            if len(sent) <= max_chars:
                pieces.append(sent)
            else:
                pieces.extend(_hard_window(sent, max_chars, overlap))
    if not pieces:
        return _hard_window(text, max_chars, overlap)

    # Greedy aggregation up to max_chars.
    out: list[str] = []
    buf = ""
    for p in pieces:
        if not buf:
            buf = p
        elif len(buf) + 1 + len(p) <= max_chars:
            buf = buf + "\n" + p
        else:
            out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out


# ---------------------------------------------------------------------------
# Ranking math (spec §3 Ranking Formula)
# ---------------------------------------------------------------------------
def cosine_sim_from_l2(l2_distance: float) -> float:
    """For L2-normalized vectors: cos = 1 - L2^2/2, clamped to [0, 1]."""
    val = 1.0 - (l2_distance * l2_distance) / 2.0
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


def time_decay(timestamp_iso: str, half_life_days: float = 90.0) -> float:
    """2^(-elapsed_days / half_life_days). Clamped to a tiny floor."""
    try:
        ts = datetime.fromisoformat(timestamp_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 1.0
    elapsed_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    days = max(0.0, elapsed_seconds / 86400.0)
    decay = math.pow(2.0, -days / half_life_days) if half_life_days > 0 else 1.0
    return max(decay, 1e-6)


def keyword_relevance(
    bm25_raw: Optional[float],
    bm25_max_abs: float,
    bm25_min_threshold: float = 0.1,
) -> float:
    """Normalize FTS5 bm25() (negative → relevant) into [0, 1]."""
    if bm25_raw is None:
        return 0.0
    abs_val = abs(bm25_raw)
    if abs_val < bm25_min_threshold:
        return 0.0
    denom = max(1.0, bm25_max_abs)
    return min(1.0, abs_val / denom)


# Free edition: session_id AND local_id are stored per-record but do NOT
# influence ranking. Per spec §3 Identifiers Note: "In Free, local_id is
# stored per-record but not used in ranking. The B_local bias multiplier
# that prioritizes same-environment memories is a Pro feature." Same for
# session_id (b_session is also Pro-only).
B_SESSION_MATCH = 1.0
B_SESSION_MISMATCH = 1.0  # disabled in Free
B_LOCAL_MATCH = 1.0
B_LOCAL_MISMATCH = 1.0  # disabled in Free


def session_bias(record_session: Optional[str], current_session: Optional[str]) -> float:
    if not current_session or not record_session:
        return B_SESSION_MATCH
    return B_SESSION_MATCH if record_session == current_session else B_SESSION_MISMATCH


def local_bias(record_local: Optional[str], current_local: Optional[str]) -> float:
    if not current_local or not record_local:
        return B_LOCAL_MATCH
    return B_LOCAL_MATCH if record_local == current_local else B_LOCAL_MISMATCH


def compute_score(
    cos_sim: float,
    keyword: float,
    time_decay_v: float,
    *,
    session_b: float = 1.0,
    local_b: float = 1.0,
) -> float:
    """Final = (cos*0.7 + kw*0.3) * time_decay * session_bias * local_bias."""
    base = cos_sim * 0.7 + keyword * 0.3
    return base * time_decay_v * session_b * local_b


# ---------------------------------------------------------------------------
# Embeddings (spec §3) — lazy-loaded e5-base-v2 with required prefixes
# ---------------------------------------------------------------------------
_MODEL = None
EMBEDDING_MODEL_NAME = "intfloat/e5-base-v2"


def _get_model():
    global _MODEL
    if _MODEL is None:
        # Suppress sentence-transformers warnings on stderr.
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _MODEL


def embed_passage(text: str) -> list[float]:
    """Save-time embedding. MUST use 'passage: ' prefix (spec §3)."""
    model = _get_model()
    vec = model.encode(
        "passage: " + text,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return [float(x) for x in vec.tolist()]


def embed_query(text: str) -> list[float]:
    """Search-time embedding. MUST use 'query: ' prefix (spec §3)."""
    model = _get_model()
    vec = model.encode(
        "query: " + text,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return [float(x) for x in vec.tolist()]


# ---------------------------------------------------------------------------
# Knowledge Refresh — replace an existing record's content (used by --repair
# vec re-index migration and possible future updates).
# ---------------------------------------------------------------------------
def refresh_memory(
    conn,
    memory_id: str,
    new_content: str,
    *,
    new_embedding: Optional[Iterable[float]] = None,
) -> bool:
    """Replace content (and optionally re-embed) for an existing memory.

    Updates timestamp to now. Returns True on success.
    """
    from database import (  # local import to avoid cycles
        strip_fts_punctuation,
        _load_vec_extension,
    )
    _load_vec_extension(conn)
    row = conn.execute(
        "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return False
    rowid = row["rowid"]
    new_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.execute(
            "UPDATE memories SET content = ?, timestamp = ? WHERE rowid = ?",
            (new_content, new_ts, rowid),
        )
        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
        conn.execute(
            "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
            (rowid, strip_fts_punctuation(new_content)),
        )
        if new_embedding is not None:
            conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid,))
            conn.execute(
                "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                (rowid, serialize_vector(new_embedding)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


__all__ = [
    "purify_text",
    "_purify",
    "chunk_text",
    "sanitize_surrogates",
    "cosine_sim_from_l2",
    "time_decay",
    "keyword_relevance",
    "session_bias",
    "local_bias",
    "compute_score",
    "embed_passage",
    "embed_query",
    "refresh_memory",
    "EMBEDDING_MODEL_NAME",
    "B_SESSION_MATCH",
    "B_SESSION_MISMATCH",
    "B_LOCAL_MATCH",
    "B_LOCAL_MISMATCH",
]
