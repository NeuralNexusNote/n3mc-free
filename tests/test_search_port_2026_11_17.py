"""2026-11-17 search changes: Japanese FTS, bigram index, system prompts, reply-similarity rerank (no model)."""
import io
import json
import sys

from n3memorycore import n3memory as n3m
from n3memorycore.core import database as db
from n3memorycore.core import processor as p

from .conftest import add, fake_embed


def search(conn, cfg, q, **kw):
    return p.hybrid_search(conn, q, cfg, embed_fn=fake_embed, **kw)


def test_unspaced_japanese_sentence_hits_the_record_with_those_words(conn, cfg):
    add(conn, cfg, "プロジェクトの進捗管理について毎日報告する", embed=False)
    assert db.search_fts(conn, "進捗管理について教えてください")


def test_hiragana_only_trigrams_are_dropped():
    q = db._build_fts_query("してください進捗管理")
    assert '"ください"' not in q and '"してく"' not in q and '"進捗管"' in q


def test_symbol_only_and_two_char_queries_do_not_raise(conn, cfg):
    add(conn, cfg, "記憶の設定")
    for q in ("!!!???", "記憶", "PR", "# $ %"):
        search(conn, cfg, q)


def test_bigram_text():
    assert db.bigram_text("N3MCの設定") == "N3MC の設 設定"


def test_two_char_words_are_found_through_the_bigram_index(conn, cfg):
    add(conn, cfg, "記憶の設定を変更する", embed=False)
    add(conn, cfg, "open a PR for review", embed=False)
    assert db.search_fts(conn, "記憶") == []                 # trigram cannot see 2 characters
    assert db.search_fts_bigram(conn, "記憶")
    assert search(conn, cfg, "記憶")["results"][0]["content"] == "記憶の設定を変更する"
    assert search(conn, cfg, "記憶")["results"] and search(conn, cfg, "PR")["results"]


def test_bigram_failure_keeps_trigram_results(conn, cfg, monkeypatch):
    add(conn, cfg, "the quick brown fox jumps", embed=False)
    monkeypatch.setattr(p, "search_fts_bigram", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert search(conn, cfg, "quick brown fox")["results"]


def test_delete_leaves_no_bigram_row(conn, cfg):
    rid = add(conn, cfg, "記憶の設定")
    db.delete_memory(conn, rid)
    assert conn.execute("SELECT COUNT(*) FROM memories_fts_bi").fetchone()[0] == 0


def test_repair_fills_old_rows_once(conn, cfg):
    add(conn, cfg, "記憶の設定", embed=False)
    conn.execute("DELETE FROM memories_fts_bi")
    conn.commit()
    n3m._repair(conn, cfg)
    n3m._repair(conn, cfg)
    assert conn.execute("SELECT COUNT(*) FROM memories_fts_bi").fetchone()[0] == 1


def _hook(monkeypatch, prompt):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(
        {"session_id": "s", "prompt": prompt}).encode()), encoding="utf-8"))
    return n3m.cmd_hook_submit()


def test_agent_message_and_system_reminder_skipped(conn, monkeypatch, capsys):
    n3m._write_turn_id("s", "KEEP")
    _hook(monkeypatch, '<agent-message from="x">hi</agent-message>')
    _hook(monkeypatch, "<system-reminder>x</system-reminder>")
    assert db.count_memories(conn) == 0 and n3m._read_turn_id("s") == "KEEP"
    assert "_No relevant memories found._" in capsys.readouterr().out


def test_real_user_text_that_looks_like_markup_is_not_skipped(conn, monkeypatch, capsys):
    _hook(monkeypatch, "<agent-message is a tag I typed")
    _hook(monkeypatch, "<div>my html question</div>")
    assert db.count_memories(conn) == 2


def test_reply_sim_rerank_is_off_by_default(cfg):
    assert cfg["reply_sim_rerank_enabled"] is False


def test_reply_sim_rerank_on_moves_the_close_reply_up(conn, cfg):
    cfg = {**cfg, "reply_sim_rerank_enabled": True, "min_score": 0.0, "vector_min_cos": 0.0,
           "relative_min_score_ratio": 0.0, "reply_sim_floor": 0.3}
    add(conn, cfg, "[user] how do I configure the zebra crossing", turn_id="A")
    add(conn, cfg, "[claude] unrelated pottery wheel clay kiln glaze firing", turn_id="A")
    add(conn, cfg, "[user] another matter entirely", turn_id="B")
    add(conn, cfg, "[claude] how do I configure the zebra crossing: set it like so", turn_id="B")
    q = "how do I configure the zebra crossing"
    off = search(conn, {**cfg, "reply_sim_rerank_enabled": False}, q)["results"]
    on = search(conn, cfg, q)["results"]
    assert off[0]["turn_id"] == "A" and on[0]["turn_id"] == "B"


def test_rerank_returns_input_on_db_error(cfg):
    rows = [{"turn_id": "x", "score": 1.0, "timestamp": "t", "id": "i"}]
    assert p._rerank_by_reply(object(), fake_embed("q"), rows, cfg) == rows
