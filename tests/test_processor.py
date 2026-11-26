"""Layer 2: ranking math, purify, embeddings (TestEmbedding loads the real model), duplicates."""
import math

import pytest

from n3memorycore.core import processor
from n3memorycore.core.processor import (
    cosine_sim_from_l2, compute_time_decay, keyword_relevance, purify_text, combined_score, b_session,
    embed_passage, embed_query,
)
from n3memorycore.core.database import insert_memory_once, check_exact_duplicate, count_memories
from tests.conftest import ts_days_ago, use_model


class TestCosineSim:
    def test_identical_vectors(self):
        assert cosine_sim_from_l2(0.0) == 1.0

    def test_orthogonal_vectors(self):
        assert cosine_sim_from_l2(math.sqrt(2)) == pytest.approx(0.0, abs=1e-9)

    def test_negative_clamped(self):
        assert cosine_sim_from_l2(2.0) == 0.0

    def test_midpoint(self):
        assert cosine_sim_from_l2(1.0) == pytest.approx(0.5)


class TestTimeDecay:
    def test_current_time_is_one(self):
        assert compute_time_decay(ts_days_ago(0), 90) == pytest.approx(1.0, abs=1e-6)

    def test_half_life(self):
        assert compute_time_decay(ts_days_ago(90), 90) == pytest.approx(0.5, abs=1e-4)

    def test_floor_value(self):
        v = compute_time_decay(ts_days_ago(3650), 90)
        assert 0 < v < 1e-10

    def test_bad_timestamp_returns_one(self):
        assert compute_time_decay("not a time", 90) == 1.0


class TestKeywordRelevance:
    def test_below_threshold_is_zero(self):
        assert keyword_relevance(-0.05, 5.0, threshold=0.1) == 0.0

    def test_full_relevance(self):
        assert keyword_relevance(-5.0, 5.0, threshold=0.1) == 1.0

    def test_partial_relevance(self):
        assert keyword_relevance(-2.5, 5.0, threshold=0.1) == pytest.approx(0.5)

    def test_zero_max(self):
        # the divisor is max(1.0, max): a maximum below 1 does not raise the value
        assert keyword_relevance(-0.5, 0.5, threshold=0.1) == pytest.approx(0.5)
        # a DB with 2 records or fewer: a maximum of 0 gives 0.0 (no division by zero)
        assert keyword_relevance(0.0, 0.0, threshold=0.1, small_db=True) == 0.0
        assert keyword_relevance(-2e-6, 2e-6, threshold=0.1, small_db=True) == pytest.approx(1.0)


class TestPurification:
    def test_code_block_replaced(self):
        assert purify_text("before\n```python\nprint(1)\n```\nafter") == "before\n[code omitted]\nafter"

    def test_inline_code_preserved(self):
        assert purify_text("run `n3mc --list` now") == "run `n3mc --list` now"

    def test_multiple_blocks_replaced(self):
        out = purify_text("a\n```\nx\n```\nb\n```js\ny\n```\nc")
        assert out == "a\n[code omitted]\nb\n[code omitted]\nc"

    def test_no_code_block_unchanged(self):
        text = "plain text with ``` an unclosed fence"
        assert purify_text(text) == text

    def test_backticks_in_prose_are_not_a_fence(self):
        text = ("To start a block type ``` then the language\nSome explanation that matters.\n"
                "```python\nprint(1)\n```\nAfter.")
        assert purify_text(text) == ("To start a block type ``` then the language\n"
                                     "Some explanation that matters.\n[code omitted]\nAfter.")

    def test_backticks_inside_code_do_not_close(self):
        text = ("First:\n```python\nprint(\"```\")\n```\nText between the blocks.\n"
                "```python\ny = 2\n```\nEnd.")
        assert purify_text(text) == "First:\n[code omitted]\nText between the blocks.\n[code omitted]\nEnd."
        # a shorter ``` line inside a block opened with 4 backticks does not close it
        assert purify_text("a\n````md\n```\ninner\n```\n````\nb") == "a\n[code omitted]\nb"


class TestEmbedding:
    @pytest.fixture(autouse=True)
    def _model(self, monkeypatch, real_model):
        use_model(monkeypatch, real_model)

    def test_passage_embedding_shape(self):
        v = embed_passage("坂本龍馬は幕末の志士である。")
        assert len(v) == 768
        assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0, abs=1e-3)

    def test_query_embedding_shape(self):
        assert len(embed_query("坂本龍馬")) == 768

    def test_same_text_similar(self):
        a = embed_passage("The capital of France is Paris.")
        b = embed_passage("Paris is the capital city of France.")
        c = embed_passage("Bananas are rich in potassium.")
        dot = lambda x, y: sum(p * q for p, q in zip(x, y))  # noqa: E731
        assert dot(a, b) > dot(a, c)


class TestRefresh:
    def test_exact_duplicate_not_inserted(self, conn):
        assert insert_memory_once(conn, "a", "[claude] same", ts_days_ago(0), "o", turn_id="t") is not None
        assert insert_memory_once(conn, "b", "[claude] same", ts_days_ago(0), "o", turn_id="t") is None
        assert count_memories(conn) == 1

    def test_near_identical_not_duplicate(self, conn):
        # near-identical text is not a duplicate: only exactly the same text is skipped
        insert_memory_once(conn, "a", "The server port is 18520.", ts_days_ago(0), "o")
        assert not check_exact_duplicate(conn, "The server port is 18521.")
        assert insert_memory_once(conn, "b", "The server port is 18521.", ts_days_ago(0), "o") is not None
        assert count_memories(conn) == 2


class TestBiasScoring:
    def test_session_match(self):
        assert b_session("s1", "s1") == 1.0

    def test_session_mismatch_lower(self):
        assert b_session("s2", "s1") == 0.6
        assert b_session(None, "s1") == 0.6

    def test_full_score_formula(self):
        score, fscore = combined_score(0.9, 0.5, 0.5, 0.6)
        assert fscore == pytest.approx(0.9 * 0.7 + 0.5 * 0.3)
        assert score == pytest.approx(fscore * 0.5 * 0.6)
