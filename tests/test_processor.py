"""Layer 2: ranking math + purify + chunking + bias + (optional) embedding."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from core import processor as proc
from core import database as db


class TestCosineSim:
    def test_identical_vectors(self):
        # L2 distance 0 => cos_sim = 1
        assert proc.cosine_sim_from_l2(0.0) == 1.0

    def test_orthogonal_vectors(self):
        # For unit vectors, orthogonal => L2 = sqrt(2)
        assert abs(proc.cosine_sim_from_l2(math.sqrt(2)) - 0.0) < 1e-6

    def test_clamp_negative(self):
        # Opposite-direction unit vectors => L2 = 2 => raw -1; clamp to 0
        assert proc.cosine_sim_from_l2(2.0) == 0.0

    def test_intermediate(self):
        v = proc.cosine_sim_from_l2(1.0)
        assert 0.0 < v < 1.0


class TestTimeDecay:
    def test_now_returns_one(self):
        ts = datetime.now(timezone.utc).isoformat()
        assert abs(proc.time_decay(ts, 90) - 1.0) < 0.01

    def test_half_life(self):
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        v = proc.time_decay(old, 90)
        assert abs(v - 0.5) < 0.05

    def test_floor_value(self):
        very_old = (datetime.now(timezone.utc) - timedelta(days=100000)).isoformat()
        assert proc.time_decay(very_old, 90) > 0

    def test_invalid_timestamp(self):
        assert proc.time_decay("not-a-date", 90) == 1.0


class TestKeywordRelevance:
    def test_below_threshold(self):
        assert proc.keyword_relevance(-0.05, 1.0, 0.1) == 0.0

    def test_perfect_match(self):
        assert proc.keyword_relevance(-2.0, 2.0, 0.1) == 1.0

    def test_partial_match(self):
        v = proc.keyword_relevance(-1.0, 2.0, 0.1)
        assert 0.0 < v < 1.0

    def test_zero_max(self):
        # max==1.0 floor -> denom is at least 1.0
        v = proc.keyword_relevance(-0.5, 0.0, 0.1)
        assert v == 0.5


class TestPurification:
    def test_code_block_replaced(self):
        text = "before\n```python\nprint('hi')\n```\nafter"
        out = proc.purify_text(text)
        assert "[code omitted]" in out
        assert "print" not in out

    def test_inline_code_preserved(self):
        text = "use the `os.path` module"
        assert proc.purify_text(text) == text

    def test_multiple_code_blocks(self):
        text = "```\na\n```\nmid\n```\nb\n```"
        out = proc.purify_text(text)
        assert out.count("[code omitted]") == 2

    def test_no_code_blocks_unchanged(self):
        text = "Just plain text with no fences."
        assert proc.purify_text(text) == text

    def test_unclosed_fence_preserved(self):
        text = "```python\nno close"
        out = proc.purify_text(text)
        assert "[code omitted]" not in out


class TestChunkText:
    def test_short_text_single_chunk(self):
        assert proc.chunk_text("hello") == ["hello"]

    def test_long_text_multi_chunk(self):
        text = "para1.\n\n" + ("a" * 800) + "\n\nlast"
        chunks = proc.chunk_text(text, max_chars=400, overlap=40)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c) <= 400

    def test_empty_returns_empty(self):
        assert proc.chunk_text("") == []


class TestBiasScoring:
    def test_local_bias_match(self):
        assert proc.local_bias("u1", "u1") == proc.B_LOCAL_MATCH

    def test_local_bias_free_neutral(self):
        # Free edition: local_id mismatch must not penalize ranking
        # (spec §3 Identifiers Note: B_local is a Pro feature).
        assert proc.B_LOCAL_MISMATCH == 1.0
        assert proc.local_bias("u1", "u2") == 1.0

    def test_session_bias_free_neutral(self):
        # Free edition: session_id has no ranking effect.
        assert proc.session_bias("a", "b") == proc.B_SESSION_MATCH
        assert proc.session_bias("a", "a") == proc.B_SESSION_MATCH

    def test_full_scoring_formula(self):
        # cos=0.8, kw=0.5, decay=0.9, local=1.0
        # base = 0.8*0.7 + 0.5*0.3 = 0.56 + 0.15 = 0.71
        # final = 0.71 * 0.9 = 0.639
        s = proc.compute_score(0.8, 0.5, 0.9, local_b=1.0)
        assert abs(s - 0.639) < 0.001


class TestRefresh:
    def test_refresh_replaces_record(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        mid, rowid = db.insert_memory(conn, "old", dummy_vec(), "o", "l")
        ok = proc.refresh_memory(conn, mid, "new")
        assert ok is True
        row = db.get_memory_by_rowid(conn, rowid)
        assert row["content"] == "new"

    def test_refresh_updates_timestamp(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        old_ts = "2020-01-01T00:00:00+00:00"
        mid, rowid = db.insert_memory(conn, "x", dummy_vec(), "o", "l", timestamp=old_ts)
        proc.refresh_memory(conn, mid, "y")
        row = db.get_memory_by_rowid(conn, rowid)
        assert row["timestamp"] != old_ts


@pytest.mark.slow
class TestEmbedding:
    def test_passage_embedding_norm(self, embedding_model):
        v = proc.embed_passage("Abraham Lincoln")
        norm = math.sqrt(sum(x * x for x in v))
        assert 0.99 < norm < 1.01

    def test_query_embedding_norm(self, embedding_model):
        v = proc.embed_query("Lincoln")
        norm = math.sqrt(sum(x * x for x in v))
        assert 0.99 < norm < 1.01

    def test_same_text_similar_vectors(self, embedding_model):
        a = proc.embed_passage("hello world")
        b = proc.embed_passage("hello world")
        # identical input -> identical output
        for x, y in zip(a, b):
            assert abs(x - y) < 1e-5
