import os
import sys
import math
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from n3memorycore.core.processor import (
    cosine_sim_from_l2, time_decay, keyword_relevance,
    purify_text, chunk_text, add_chunk_prefixes,
    embed_passage, embed_query,
)


class TestCosineSim:
    def test_identical_vectors(self):
        assert abs(cosine_sim_from_l2(0.0) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        # L2 dist of orthogonal unit vectors = sqrt(2), dist^2 = 2
        val = cosine_sim_from_l2(math.sqrt(2))
        assert abs(val - 0.0) < 1e-6

    def test_clamp_negative(self):
        # dist > sqrt(2) → would give negative cos; must clamp to 0
        val = cosine_sim_from_l2(2.0)
        assert val == 0.0

    def test_intermediate_value(self):
        # dist^2 = 1 → cos = max(0, 1 - 0.5) = 0.5
        val = cosine_sim_from_l2(1.0)
        assert abs(val - 0.5) < 1e-6


class TestTimeDecay:
    def test_now_returns_one(self):
        ts = datetime.now(timezone.utc).isoformat()
        assert abs(time_decay(ts, 90) - 1.0) < 0.01

    def test_half_life(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        val = time_decay(ts, 90)
        assert abs(val - 0.5) < 0.01

    def test_floor_value(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=900)).isoformat()
        val = time_decay(ts, 90)
        assert 0.0 < val < 0.01

    def test_invalid_timestamp_returns_one(self):
        val = time_decay("not-a-date", 90)
        assert val == 1.0


class TestKeywordRelevance:
    def test_below_threshold(self):
        assert keyword_relevance(-0.05, 1.0, 0.1) == 0.0

    def test_perfect_match(self):
        # bm25=-5.0, max=5.0 → 5/max(1,5) = 1.0
        assert abs(keyword_relevance(-5.0, 5.0, 0.1) - 1.0) < 1e-6

    def test_partial_match(self):
        val = keyword_relevance(-2.5, 5.0, 0.1)
        assert abs(val - 0.5) < 1e-6

    def test_zero_max(self):
        # max_abs_bm25=0 → max(1.0, 0) = 1.0 denominator
        val = keyword_relevance(-1.0, 0.0, 0.1)
        assert abs(val - 1.0) < 1e-6


class TestPurification:
    def test_code_block_replaced_with_omitted(self):
        text = "before\n```python\ncode here\n```\nafter"
        result = purify_text(text)
        assert '[code omitted]' in result
        assert 'code here' not in result

    def test_inline_code_preserved(self):
        text = "use `print()` function"
        result = purify_text(text)
        assert '`print()`' in result

    def test_multiple_code_blocks_replaced(self):
        text = "a\n```\ncode1\n```\nb\n```\ncode2\n```\nc"
        result = purify_text(text)
        assert result.count('[code omitted]') == 2
        assert 'code1' not in result
        assert 'code2' not in result

    def test_no_code_blocks_unchanged(self):
        text = "plain text without any code"
        assert purify_text(text) == text


class TestEmbedding:
    def test_passage_prefix(self, real_model):
        # Embedding should not raise
        vec = embed_passage("hello world")
        assert len(vec) == 768

    def test_query_prefix(self, real_model):
        vec = embed_query("hello world")
        assert len(vec) == 768

    def test_embed_passage_function(self, real_model):
        vec = embed_passage("test passage")
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-5

    def test_embed_query_function(self, real_model):
        vec = embed_query("test query")
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-5

    def test_same_text_similar_vectors(self, real_model):
        v1 = embed_passage("Abraham Lincoln was the 16th president")
        v2 = embed_query("Abraham Lincoln president")
        dot = sum(a * b for a, b in zip(v1, v2))
        assert dot > 0.5


class TestChunking:
    def test_short_text_single_chunk(self):
        chunks = chunk_text("short text", max_chars=400)
        assert len(chunks) == 1

    def test_long_text_multiple_chunks(self):
        text = "word " * 200  # 1000 chars
        chunks = chunk_text(text, max_chars=100, overlap=10)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 100 + 10  # allow slight overlap

    def test_add_chunk_prefixes_single(self):
        result = add_chunk_prefixes(["hello"], "user")
        assert result == ["[user] hello"]

    def test_add_chunk_prefixes_multi(self):
        result = add_chunk_prefixes(["a", "b", "c"], "claude")
        assert result[0] == "[claude 1/3] a"
        assert result[2] == "[claude 3/3] c"
