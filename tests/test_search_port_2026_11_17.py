"""Regression tests for the 2026-11-17 search changes (spec §3, §4.5, §5).

1. Japanese FTS grams (trigram MATCH), 2. CJK bigram index,
3. subagent reports / app notices neither saved nor searched,
5. re-rank by similarity to the reply (off by default).
(4. the access-count boost does not exist in Free.)

No embedding model: vectors are fixed fakes. Real data and real servers are
never touched (conftest._isolate_real_data).
"""
import io
import json
import os

import pytest

from n3memorycore import n3memory
from n3memorycore.core import database as db
from n3memorycore.core import processor as proc

from tests.test_regressions import add, at_cos, cfg, new_db, query_vec, unit  # noqa: F401


def filler(path, n=30):
    """bm25 needs other records: with one or two rows every term is common."""
    for i in range(n):
        add(path, f'filler record number {i}', emb=unit(10 + i))


def fts(path, q, bigram=False):
    conn = db.get_connection(path)
    try:
        f = db.search_fts_bigram if bigram else db.search_fts
        return [r['content'] for r in f(conn, q)]
    finally:
        conn.close()


# --------------------------------------------------------------------------- 1. trigram grams
class TestJapaneseTrigramQuery:
    def test_sentence_without_spaces_hits_a_word(self, tmp_path):
        path = new_db(tmp_path)
        add(path, '昨日はサーバーの設定ファイルを直した')
        add(path, 'unrelated english note')
        # The whole sentence is not in the record; its words are.
        assert fts(path, 'サーバーの設定ファイルについて教えてください') == [
            '昨日はサーバーの設定ファイルを直した']

    def test_hiragana_only_grams_dropped(self):
        built = db._build_fts_query('設定してください')
        assert '"してく"' not in built and '"ください"' not in built
        assert '"設定し"' in built

    def test_english_words_stay_whole(self):
        assert db._build_fts_query('quick fox') == '"quick" OR "fox"'

    @pytest.mark.parametrize('q', ['!!! ??? 。。。', '記憶', 'PR', '', '\x00'])
    def test_symbol_or_short_queries_do_not_raise(self, tmp_path, q):
        path = new_db(tmp_path)
        add(path, 'some content')
        fts(path, q)
        fts(path, q, bigram=True)


# --------------------------------------------------------------------------- 2. bigram index
class TestBigramIndex:
    def test_bigram_text(self):
        assert db.bigram_text('N3MCの設定') == 'N3MC の設 設定'
        assert db.bigram_text('「記憶」') == '記憶'

    def test_two_char_words_hit(self, tmp_path):
        path = new_db(tmp_path)
        add(path, '記憶の保存先を変えた')
        add(path, 'PR を作った')
        add(path, 'nothing here')
        assert fts(path, '記憶') == []                     # trigram cannot
        assert fts(path, '記憶', bigram=True) == ['記憶の保存先を変えた']
        assert fts(path, 'PR', bigram=True) == ['PR を作った']

    def test_hiragana_only_tokens_dropped_when_others_left(self):
        assert db.build_bigram_query('記憶ですか') == '"記憶" OR "憶で"'
        assert db.build_bigram_query('ですか') == '"です" OR "すか"'

    def test_hybrid_search_finds_two_char_word(self, tmp_path, monkeypatch):
        monkeypatch.setattr(proc, 'embed_query',
                            lambda t: (_ for _ in ()).throw(RuntimeError('no model')))
        path = new_db(tmp_path)
        add(path, '記憶の保存先を変えた', emb=unit(5))
        add(path, '別の話題', emb=unit(6))
        filler(path)
        res = proc.hybrid_search(path, '記憶', cfg(min_score=0.2))
        assert [r['content'] for r in res['results']] == ['記憶の保存先を変えた']

    def test_bigram_failure_keeps_trigram_results(self, tmp_path, monkeypatch):
        monkeypatch.setattr(proc, 'embed_query',
                            lambda t: (_ for _ in ()).throw(RuntimeError('no model')))

        def broken(*a, **k):
            raise RuntimeError('bigram broken')
        monkeypatch.setattr(proc, 'search_fts_bigram', broken)
        path = new_db(tmp_path)
        add(path, 'quick fox jumps', emb=unit(5))
        filler(path)
        res = proc.hybrid_search(path, 'quick fox', cfg(min_score=0.2))
        assert [r['content'] for r in res['results']] == ['quick fox jumps']

    def test_delete_removes_bigram_row(self, tmp_path):
        path = new_db(tmp_path)
        rid = add(path, '記憶の保存先', emb=unit(5))
        conn = db.get_connection(path)
        try:
            assert db.delete_memory(conn, rid)
            assert conn.execute("SELECT COUNT(*) FROM memories_fts_bi").fetchone()[0] == 0
        finally:
            conn.close()

    def test_repair_fills_old_rows_once(self, monkeypatch):
        monkeypatch.setattr(n3memory, '_srv_cfg', {}, raising=False)
        os.makedirs(n3memory.MEMORY_DIR, exist_ok=True)
        conn = db.get_connection(n3memory.DB_PATH)
        try:
            db.init_db(conn)
        finally:
            conn.close()
        add(n3memory.DB_PATH, '記憶の保存先', emb=unit(5))
        add(n3memory.DB_PATH, 'PR を作った', emb=unit(6))
        conn = db.get_connection(n3memory.DB_PATH)
        try:   # an old DB: records saved before memories_fts_bi existed
            conn.execute("DELETE FROM memories_fts_bi")
            conn.commit()
        finally:
            conn.close()
        n3memory._repair_db()
        n3memory._repair_db()
        conn = db.get_connection(n3memory.DB_PATH)
        try:   # v1.8.0: the key lives in the meta table (spec §3.5)
            assert db.meta_get(conn, 'fts_bigram') == '1'
        finally:
            conn.close()
        conn = db.get_connection(n3memory.DB_PATH)
        try:
            assert conn.execute("SELECT COUNT(*) FROM memories_fts_bi").fetchone()[0] == 2
        finally:
            conn.close()
        assert fts(n3memory.DB_PATH, '記憶', bigram=True) == ['記憶の保存先']


# --------------------------------------------------------------------------- 3. system inputs
class TestSystemInputsSkipped:
    @pytest.mark.parametrize('prompt', [
        '<agent-message from="worker-1">done</agent-message>',
        '<system-reminder>\nThe user is away.\n</system-reminder>',
    ])
    def test_not_saved_not_searched(self, monkeypatch, capsys, prompt):
        def no_server(c):
            raise AssertionError('ensure_server must not be called')
        monkeypatch.setattr(n3memory, 'ensure_server', no_server)
        n3memory._write_turn_id('keep-me', 'sess')
        payload = {'prompt': prompt, 'session_id': 'sess'}
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
        n3memory.cmd_hook_submit({'server_port': 1, 'search_query_max_chars': 2000})
        assert n3memory._read_turn_id('sess') == 'keep-me'
        assert '_No relevant memories found._' in capsys.readouterr().out
        assert not os.path.exists(n3memory.DB_PATH)

    def test_user_text_starting_with_tag_is_kept(self):
        for text in ('<agent-message について教えて', '<div> の書き方'):
            assert not text.startswith(n3memory._SYSTEM_PROMPT_PREFIXES)


# --------------------------------------------------------------------------- 5. reply rerank
class TestReplySimilarityRerank:
    def _two_exchanges(self, tmp_path):
        """Exchange A: the request is closest to the query, the reply is not.
        Exchange B: the request is a little less close, the reply is close."""
        path = new_db(tmp_path)
        add(path, '[user] question A', emb=at_cos(0.95), turn_id='A')
        add(path, '[claude] reply A', emb=at_cos(0.80, axis=2), turn_id='A')
        add(path, '[user] question B', emb=at_cos(0.90, axis=3), turn_id='B')
        add(path, '[claude] reply B', emb=at_cos(0.86, axis=4), turn_id='B')
        return path

    def test_off_by_default_keeps_order(self, tmp_path, query_vec):
        path = self._two_exchanges(tmp_path)
        res = proc.hybrid_search(path, 'zz', cfg())
        assert res['results'][0]['content'] == '[user] question A'
        assert n3memory._DEFAULT_CONFIG['reply_sim_rerank_enabled'] is False

    def test_on_moves_exchange_with_close_reply_up(self, tmp_path, query_vec):
        path = self._two_exchanges(tmp_path)
        res = proc.hybrid_search(path, 'zz', cfg(reply_sim_rerank_enabled=True))
        assert res['results'][0]['turn_id'] == 'B'

    def test_db_error_returns_input(self):
        class Broken:
            def execute(self, *a, **k):
                raise RuntimeError('db down')
        rows = [{'id': '1', 'turn_id': 'A', 'score': 0.5, 'timestamp': 't'},
                {'id': '2', 'turn_id': 'B', 'score': 0.4, 'timestamp': 't'}]
        out = proc._rerank_by_reply_similarity(Broken(), [dict(r) for r in rows],
                                               unit(0), 0.3, 0.80, 0.15)
        assert [(r['id'], r['score']) for r in out] == [('1', 0.5), ('2', 0.4)]
