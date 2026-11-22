"""Layer 2: ranking maths, purify, chunking, embedding (the real model is used only in TestEmbedding)."""
from datetime import datetime, timedelta, timezone

import pytest

from n3memorycore.core import processor as p


class TestCosineSim:
    def test_identical_vectors(self):
        assert p.cosine_sim_from_l2(0.0) == 1.0

    def test_orthogonal_vectors(self):
        assert abs(p.cosine_sim_from_l2(2 ** 0.5)) < 1e-9

    def test_negative_clamped(self):
        assert p.cosine_sim_from_l2(2.0) == 0.0

    def test_midpoint(self):
        assert abs(p.cosine_sim_from_l2(1.0) - 0.5) < 1e-9


class TestTimeDecay:
    def test_current_time_is_one(self):
        assert abs(p.time_decay(datetime.now(timezone.utc).isoformat(), 90) - 1.0) < 1e-3

    def test_half_life(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        assert abs(p.time_decay(ts, 90) - 0.5) < 1e-3

    def test_floor_value(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=36500)).isoformat()
        assert 0 <= p.time_decay(ts, 90) < 1e-6

    def test_bad_timestamp_returns_one(self):
        assert p.time_decay("not a date", 90) == 1.0


class TestKeywordRelevance:
    def test_below_threshold_is_zero(self):
        assert p.keyword_relevances({"a": -0.05, "b": -3.0}, 0.1)["a"] == 0.0

    def test_full_relevance(self):
        assert p.keyword_relevances({"a": -5.0}, 0.1)["a"] == 1.0

    def test_partial_relevance(self):
        r = p.keyword_relevances({"a": -5.0, "b": -2.5}, 0.1)
        assert abs(r["b"] - 0.5) < 1e-9

    def test_zero_max(self):
        assert p.keyword_relevances({}, 0.1) == {}

    def test_small_db_best_match_is_one(self):
        assert p.keyword_relevances({"a": -2e-6}, 0.1, small_db=True)["a"] == 1.0


class TestPurification:
    def test_code_block_replaced(self):
        assert p.purify_text("a\n```py\nx=1\ny=2\n```\nb") == "a\n[code omitted]\nb"

    def test_inline_code_preserved(self):
        assert p.purify_text("use `x = 1` here") == "use `x = 1` here"

    def test_multiple_blocks_replaced(self):
        t = "p\n```\na\nb\n```\nmiddle text\n```\nc\nd\n```\nq"
        assert p.purify_text(t) == "p\n[code omitted]\nmiddle text\n[code omitted]\nq"

    def test_no_code_block_unchanged(self):
        assert p.purify_text("plain 日本語") == "plain 日本語"

    def test_unclosed_fence_untouched(self):
        assert p.purify_text("```\nnever closed") == "```\nnever closed"


class TestEmbedding:
    def test_passage_embedding_shape(self):
        assert len(p.embed_passage("hello world")) == 768

    def test_query_embedding_shape(self):
        v = p.embed_query("hello world")
        assert len(v) == 768 and abs(sum(x * x for x in v) - 1.0) < 1e-3

    def test_same_text_similar(self):
        a, b = p.embed_passage("東京は日本の首都です"), p.embed_query("日本の首都は東京")
        assert sum(x * y for x, y in zip(a, b)) > 0.8


class TestRefresh:
    def test_exact_duplicate_not_inserted(self, conn, cfg):
        from n3memorycore import n3memory as n3m
        assert n3m._store_record(conn, cfg, "[user] x", turn_id="t", embed=False)["status"] == "ok"
        assert n3m._store_record(conn, cfg, "[user] x", turn_id="t", embed=False)["status"] == "skipped"

    def test_below_threshold_not_duplicate(self, conn, cfg):
        from n3memorycore import n3memory as n3m
        n3m._store_record(conn, cfg, "[user] x", turn_id="t", embed=False)
        assert n3m._store_record(conn, cfg, "[user] x!", turn_id="t", embed=False)["status"] == "ok"


class TestBiasScoring:
    def test_session_match(self):
        assert p.b_session("s1", "s1") == 1.0

    def test_session_mismatch_lower(self):
        assert p.b_session("s1", "s2") == 0.6 and p.b_session(None, "s2") == 0.6

    def test_full_score_formula(self, conn, cfg):
        from .conftest import add, fake_embed
        add(conn, cfg, "unique zebra sentence", session_id="s1")
        r = p.hybrid_search(conn, "unique zebra sentence", cfg, session_id="s1", embed_fn=fake_embed)["results"][0]
        assert abs(r["score"] - r["filter_score"] * p.time_decay(r["timestamp"], 90) * 1.0) < 1e-6
        r2 = p.hybrid_search(conn, "unique zebra sentence", cfg, session_id="other", embed_fn=fake_embed)["results"][0]
        assert abs(r2["score"] - 0.6 * r["score"]) < 1e-4 and r2["filter_score"] == r["filter_score"]
