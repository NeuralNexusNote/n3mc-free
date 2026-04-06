"""
Layer 2: Ranking math, purification, embedding
"""
import os
import sys
import math
import pytest

_CORE_DIR = os.path.join(os.path.dirname(__file__), "..", "core")
sys.path.insert(0, _CORE_DIR)

from processor import (
    cosine_sim_from_l2,
    time_decay,
    keyword_relevance,
    final_score,
    purify,
    embed_passage,
    embed_query,
)


class TestCosineSim:
    def test_identical_vectors(self):
        # L2 distance of 0 → cos_sim = 1.0
        assert cosine_sim_from_l2(0.0) == 1.0

    def test_orthogonal_vectors(self):
        # L2 distance² = 2 for orthogonal unit vectors → cos_sim = 0.0
        result = cosine_sim_from_l2(math.sqrt(2))
        assert abs(result) < 1e-9

    def test_clamp_negative(self):
        # Large distance → clamped to 0
        assert cosine_sim_from_l2(10.0) == 0.0

    def test_intermediate_value(self):
        result = cosine_sim_from_l2(1.0)
        assert 0.0 < result < 1.0


class TestTimeDecay:
    def test_now_returns_one(self):
        from datetime import datetime, timezone
        ts = datetime.now(tz=timezone.utc).isoformat()
        val = time_decay(ts, 90)
        assert abs(val - 1.0) < 0.01

    def test_half_life(self):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(tz=timezone.utc) - timedelta(days=90)).isoformat()
        val = time_decay(ts, 90)
        assert abs(val - 0.5) < 0.01

    def test_floor_value(self):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(tz=timezone.utc) - timedelta(days=9000)).isoformat()
        val = time_decay(ts, 90)
        assert 0.0 <= val < 0.01

    def test_invalid_timestamp_returns_one(self):
        val = time_decay("not-a-date", 90)
        assert val == 1.0


class TestKeywordRelevance:
    def test_below_threshold(self):
        # abs(-0.05) < 0.1 threshold → 0.0
        assert keyword_relevance(-0.05, 1.0, 0.1) == 0.0

    def test_perfect_match(self):
        # abs(-5.0) / max(1.0, 5.0) = 1.0
        assert keyword_relevance(-5.0, 5.0, 0.1) == 1.0

    def test_partial_match(self):
        val = keyword_relevance(-2.0, 5.0, 0.1)
        assert abs(val - 0.4) < 1e-9

    def test_zero_max(self):
        # max_abs=0 → denominator becomes max(1.0, 0) = 1.0
        val = keyword_relevance(-0.5, 0.0, 0.1)
        assert val == 0.5


class TestPurification:
    def test_code_block_replaced(self):
        text = "before\n```python\nprint('hi')\n```\nafter"
        result = purify(text)
        assert "[code omitted]" in result
        assert "print" not in result

    def test_inline_code_preserved(self):
        text = "use `my_func()` here"
        result = purify(text)
        assert "`my_func()`" in result

    def test_multiple_code_blocks(self):
        text = "```a```\nmiddle\n```b```"
        result = purify(text)
        assert result.count("[code omitted]") == 2

    def test_no_code_blocks(self):
        text = "no code here"
        result = purify(text)
        assert result == "no code here"


class TestEmbedding:
    def test_passage_prefix(self, embedding_model):
        # Model should accept passage prefix
        import numpy as np
        vec = embedding_model.encode("passage: test text", normalize_embeddings=True)
        assert vec.shape[0] == 768

    def test_query_prefix(self, embedding_model):
        import numpy as np
        vec = embedding_model.encode("query: test query", normalize_embeddings=True)
        assert vec.shape[0] == 768

    def test_embed_passage_function(self):
        vec = embed_passage("hello world")
        assert len(vec) == 768

    def test_embed_query_function(self):
        vec = embed_query("hello world")
        assert len(vec) == 768

    def test_same_text_similar_vectors(self):
        import numpy as np
        v1 = embed_passage("Abraham Lincoln was the 16th president")
        v2 = embed_query("Abraham Lincoln")
        # Should be reasonably similar (cos_sim > 0.7)
        from processor import cosine_sim_from_l2
        import math
        # Compute L2 distance
        diff = sum((a - b) ** 2 for a, b in zip(v1, v2)) ** 0.5
        sim = cosine_sim_from_l2(diff)
        assert sim > 0.5


class TestBiasScoring:
    def test_full_scoring_formula(self):
        score = final_score(0.8, 0.6, 1.0, 1.0)
        expected = (0.8 * 0.7 + 0.6 * 0.3) * 1.0 * 1.0
        assert abs(score - expected) < 1e-9
