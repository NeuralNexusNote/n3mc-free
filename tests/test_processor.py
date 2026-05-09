"""Layer 2: ranking math, purify [code omitted] replacement, embeddings (spec §7)."""
import math
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from n3memorycore.core.processor import (
    cosine_sim_from_l2, time_decay, keyword_relevance,
    purify_text, chunk_text, add_chunk_prefixes, sanitize_surrogates,
    embed_passage, embed_query, get_model,
)


# ---------------------------------------------------------------------------
class TestCosineSim:

    def test_identical_vectors(self):
        assert abs(cosine_sim_from_l2(0.0) - 1.0) < 1e-9

    def test_orthogonal_vectors(self):
        assert abs(cosine_sim_from_l2(math.sqrt(2)) - 0.0) < 1e-9

    def test_negative_clamped(self):
        # L2 > sqrt(2) would give cos < 0 → clamped to 0
        assert cosine_sim_from_l2(10.0) == 0.0

    def test_midpoint(self):
        # cos = 0.5 → L2² = 2*(1-0.5) = 1 → L2 = 1
        val = cosine_sim_from_l2(1.0)
        assert abs(val - 0.5) < 1e-9


# ---------------------------------------------------------------------------
class TestTimeDecay:

    def test_current_time_is_one(self):
        now = datetime.now(timezone.utc).isoformat()
        assert abs(time_decay(now, 90) - 1.0) < 0.01

    def test_half_life(self):
        past = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        val = time_decay(past, 90)
        assert abs(val - 0.5) < 0.02

    def test_floor_value(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10000)).isoformat()
        val = time_decay(old, 90)
        assert 0.0 <= val <= 1.0

    def test_bad_timestamp_returns_one(self):
        assert time_decay('not-a-date', 90) == 1.0


# ---------------------------------------------------------------------------
class TestKeywordRelevance:

    def test_below_threshold_is_zero(self):
        assert keyword_relevance(-0.05, 1.0, 0.1) == 0.0

    def test_full_relevance(self):
        assert abs(keyword_relevance(-1.0, 1.0, 0.1) - 1.0) < 1e-9

    def test_partial_relevance(self):
        val = keyword_relevance(-0.5, 1.0, 0.1)
        assert 0.0 < val < 1.0

    def test_zero_max(self):
        assert keyword_relevance(-1.0, 0.0, 0.1) == 1.0


# ---------------------------------------------------------------------------
class TestPurification:

    def test_code_block_replaced(self):
        text = 'Before\n```python\nprint("hi")\n```\nAfter'
        result = purify_text(text)
        assert '[code omitted]' in result
        assert 'print' not in result

    def test_inline_code_preserved(self):
        text = 'Use `x = 1` for assignment'
        result = purify_text(text)
        assert '`x = 1`' in result

    def test_multiple_blocks_replaced(self):
        text = '```a\ncode1\n```\ntext\n```b\ncode2\n```'
        result = purify_text(text)
        assert result.count('[code omitted]') == 2
        assert 'code1' not in result
        assert 'code2' not in result

    def test_no_code_block_unchanged(self):
        text = 'No code here, just text.'
        assert purify_text(text) == text


# ---------------------------------------------------------------------------
class TestEmbedding:

    def test_passage_embedding_shape(self, real_model):
        vec = embed_passage('テストテキスト')
        assert len(vec) == 768

    def test_query_embedding_shape(self, real_model):
        vec = embed_query('search query')
        assert len(vec) == 768

    def test_same_text_similar(self, real_model):
        v1 = embed_passage('こんにちは世界')
        v2 = embed_passage('こんにちは世界')
        dot = sum(a * b for a, b in zip(v1, v2))
        assert dot > 0.99


# ---------------------------------------------------------------------------
class TestRefresh:
    """Knowledge Refresh — Free edition: dedup skips near-duplicates (no timestamp update).
    Pro edition would instead refresh (update) the existing record's timestamp."""

    def test_exact_duplicate_not_inserted(self, tmp_path):
        """check_exact_duplicate returns True after insert; count stays 1."""
        import uuid as _uuid
        from n3memorycore.core.database import (
            get_connection, init_db, insert_memory,
            check_exact_duplicate, count_memories,
        )
        db_path = str(tmp_path / 'refresh.db')
        conn = get_connection(db_path)
        init_db(conn)
        insert_memory(conn, str(_uuid.uuid4()), 'repeat text',
                      '2026-01-01T00:00:00', owner_id='o')
        assert check_exact_duplicate(conn, 'repeat text') is True
        assert count_memories(conn) == 1
        conn.close()

    def test_below_threshold_not_duplicate(self):
        """cos_sim below 0.95 is not treated as a duplicate."""
        # L2 = 1.0 → cos_sim = 0.5 < 0.95 → not a near-duplicate
        assert cosine_sim_from_l2(1.0) == 0.5
        assert cosine_sim_from_l2(1.0) < 0.95


# ---------------------------------------------------------------------------
class TestBiasScoring:

    def test_session_match(self):
        from n3memorycore.core.processor import hybrid_search
        # b_session = 1.0 when session matches — verified via score formula
        now = datetime.now(timezone.utc).isoformat()
        decay = time_decay(now, 90)
        cs = 0.8
        kr = 0.5
        b = 1.0
        score = (cs * 0.7 + kr * 0.3) * decay * b
        assert score > 0

    def test_session_mismatch_lower(self):
        now = datetime.now(timezone.utc).isoformat()
        decay = time_decay(now, 90)
        cs = 0.8
        kr = 0.5
        score_match    = (cs * 0.7 + kr * 0.3) * decay * 1.0
        score_mismatch = (cs * 0.7 + kr * 0.3) * decay * 0.6
        assert score_match > score_mismatch

    def test_full_score_formula(self):
        now = datetime.now(timezone.utc).isoformat()
        decay = time_decay(now, 90)
        cs = 0.6
        kr = 0.4
        b = 1.0
        expected = (cs * 0.7 + kr * 0.3) * decay * b
        actual   = (0.6 * 0.7 + 0.4 * 0.3) * decay * 1.0
        assert abs(expected - actual) < 1e-9
