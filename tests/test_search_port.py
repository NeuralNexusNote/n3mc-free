"""Layer 5: Japanese FTS grams, the bigram index, skipping system inputs, reply re-ranking. No model needed."""
import io
import json
import os
import sys

import pytest

from n3memorycore import n3memory
from n3memorycore.core import processor
from n3memorycore.core.database import (
    search_fts, search_fts_bigram, bigram_text, _build_fts_query, delete_memory, delete_meta,
    get_all_memories, get_connection,
)
from n3memorycore.core.processor import hybrid_search, rerank_by_reply_similarity
from tests.conftest import add, basis, fake_embed, vec_with_cos, FakeModel, use_model, ts_days_ago


def _cfg(**kw):
    c = dict(n3memory.DEFAULT_CONFIG)
    c.update(n3memory.SPEC_SEARCH_SETTINGS)  # these tests check the search of the specification
    c.update(kw)
    return c


def _ids(res):
    return [r["content"] for r in res["results"]]


class TestJapaneseFts:
    def test_unspaced_japanese_sentence_hits_the_word(self, conn):
        add(conn, "記憶装置のバックアップ手順を説明します")
        add(conn, "関係のない天気の話です")
        hits = search_fts(conn, "バックアップ手順について教えてください")
        assert [h["content"] for h in hits] == ["記憶装置のバックアップ手順を説明します"]

    def test_hiragana_only_trigrams_dropped(self):
        q = _build_fts_query("設定してください")
        assert '"してく"' not in q and '"ください"' not in q and '"ださい"' not in q
        assert '"設定し"' in q

    def test_symbols_or_two_chars_do_not_raise(self, conn, fake_model):
        add(conn, "some record", fake_embed("passage: some record"))
        for q in ("!!! ??? ...", "AI", "東京", "\\ / # = @"):
            search_fts(conn, q)
            search_fts_bigram(conn, q)
            hybrid_search(conn, q, _cfg())


class TestBigramIndex:
    def test_bigram_text(self):
        assert bigram_text("N3MCの設定") == "N3MC の設 設定"

    def test_two_char_words_hit_through_bigrams(self, conn, fake_model):
        add(conn, "長期の記憶を保存する仕組み", fake_embed("x1"))
        add(conn, "PR を作ってマージした", fake_embed("x2"))
        add(conn, "関係のない話題です", fake_embed("x3"))
        for i in range(20):  # filler: with only a few records bm25 stays below 1 and a keyword-only hit
            add(conn, f"filler record number {i}", fake_embed(f"f{i}"))  # cannot pass min_score
        assert [h["content"] for h in search_fts(conn, "記憶")] == []  # trigram cannot find 2 characters
        assert [h["content"] for h in search_fts_bigram(conn, "記憶")] == ["長期の記憶を保存する仕組み"]
        assert [h["content"] for h in search_fts_bigram(conn, "PR")] == ["PR を作ってマージした"]
        assert "長期の記憶を保存する仕組み" in _ids(hybrid_search(conn, "記憶", _cfg(min_results=0)))
        assert "PR を作ってマージした" in _ids(hybrid_search(conn, "PR", _cfg(min_results=0)))

    def test_bigram_failure_keeps_trigram_results(self, conn, fake_model, monkeypatch):
        add(conn, "アルファ9の気温設定", fake_embed("y"))

        def boom(*a, **k):
            raise RuntimeError("bigram broken")
        monkeypatch.setattr(processor, "search_fts_bigram", boom)
        assert "アルファ9の気温設定" in _ids(hybrid_search(conn, "アルファ9", _cfg(min_results=0)))

    def test_delete_removes_bigram_rows(self, conn):
        add(conn, "削除する記録")
        mid = conn.execute("SELECT id FROM memories").fetchone()[0]
        delete_memory(conn, mid)
        assert conn.execute("SELECT COUNT(*) FROM memories_fts_bi").fetchone()[0] == 0

    def test_repair_fills_old_rows_once(self, conn, cfg):
        add(conn, "古い記録の索引")
        add(conn, "もう一つの記録")
        conn.execute("DELETE FROM memories_fts_bi")
        conn.commit()
        delete_meta(conn, "fts_bigram")
        n3memory.run_repair(conn, cfg)
        n3memory.run_repair(conn, cfg)
        rows = conn.execute("SELECT rowid, COUNT(*) FROM memories_fts_bi GROUP BY rowid").fetchall()
        assert len(rows) == 2 and all(c == 1 for _, c in rows)
        assert search_fts_bigram(conn, "記録")


class TestSystemInputs:
    def _submit(self, monkeypatch, prompt, session="s1"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": session, "prompt": prompt})))
        return n3memory.cmd_hook_submit()

    def _contents(self):
        if not os.path.exists(n3memory.DB_PATH):
            return []
        c = get_connection(n3memory.DB_PATH)
        try:
            return [r["content"] for r in get_all_memories(c)]
        finally:
            c.close()

    @pytest.mark.parametrize("prompt", ['<agent-message from="worker">done</agent-message>',
                                        "<system-reminder>only a reminder</system-reminder>"])
    def test_system_inputs_skipped(self, monkeypatch, capsys, prompt):
        n3memory._write_turn_id("s1", "turn-before")
        searched = []
        monkeypatch.setattr(n3memory, "_search_output", lambda *a, **k: searched.append(1) or "")
        self._submit(monkeypatch, prompt)
        out = capsys.readouterr().out
        assert "_No relevant memories found._" in out
        assert searched == []
        assert self._contents() == []
        assert n3memory._read_turn_id("s1") == "turn-before"

    @pytest.mark.parametrize("prompt", ["<agent-message is a tag name?", "<div> how do I center this?"])
    def test_user_questions_with_tags_not_skipped(self, monkeypatch, capsys, prompt):
        self._submit(monkeypatch, prompt)
        assert f"[user] {prompt}" in self._contents()


class TestReplyRerank:
    def _setup(self, conn):
        add(conn, "[user] question version A", basis(0), ts=ts_days_ago(2), turn_id="A")
        add(conn, "[claude] reply A", vec_with_cos(0.84), ts=ts_days_ago(2), turn_id="A")
        add(conn, "[user] question version B", basis(0), ts=ts_days_ago(1), turn_id="B")
        add(conn, "[claude] reply B", vec_with_cos(0.5, other=2), ts=ts_days_ago(1), turn_id="B")

    def _strong(self, res):
        return [r["content"] for r in res["results"] if not r["weak"]]

    def test_default_off(self, conn, monkeypatch):
        use_model(monkeypatch, FakeModel({"q": basis(0)}))
        self._setup(conn)
        assert n3memory.DEFAULT_CONFIG["reply_sim_rerank_enabled"] is False
        res = hybrid_search(conn, "q", _cfg(version_order_enabled=False))
        assert self._strong(res)[0] == "[user] question version B"

    def test_on_moves_the_closer_reply_up(self, conn, monkeypatch):
        use_model(monkeypatch, FakeModel({"q": basis(0)}))
        self._setup(conn)
        res = hybrid_search(conn, "q", _cfg(version_order_enabled=False, reply_sim_rerank_enabled=True))
        assert self._strong(res)[0] == "[user] question version A"
        assert all(r["filter_score"] == pytest.approx(0.7) for r in res["results"] if not r["weak"])

    def test_db_error_returns_input(self, conn):
        rows = [{"id": "1", "content": "x", "score": 0.5, "timestamp": "t", "turn_id": "A", "filter_score": 0.5}]
        conn.close()
        assert rerank_by_reply_similarity(conn, rows, basis(0), _cfg()) == rows


class TestRolePrefixNotIndexed:
    def test_prefix_and_placeholder_words_do_not_hit(self, conn):
        add(conn, "[claude] 晴れの日は散歩")
        add(conn, "[user] 今日の天気")
        add(conn, "[claude 1/2] 次のコマンドです\n[code omitted]")
        for q in ("claude", "user"):
            assert search_fts(conn, q) == []
            assert search_fts_bigram(conn, q) == []
        assert search_fts(conn, "code") == [] and search_fts_bigram(conn, "code") == []

    def test_claude_in_the_body_hits(self, conn):
        add(conn, "[user] the word [claude] appears in my text")
        assert search_fts(conn, "claude")
        assert search_fts_bigram(conn, "claude")
