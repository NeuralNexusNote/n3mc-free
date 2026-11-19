"""Database layer, ranking maths, chunking, purify (no model needed)."""
import threading

import pytest

from n3memorycore import n3memory
from n3memorycore.core import database as db
from n3memorycore.core import processor as pr

from .conftest import add, fake_embed


class TestSchema:
    def test_tables_and_tokenizers(self, conn):
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"memories", "memories_fts", "memories_fts_bi", "memories_vec", "meta"} <= names
        assert "trigram" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories_fts'").fetchone()[0]

    def test_migrate_idempotent_and_adds_columns(self, tmp_path):
        c = db.get_connection(str(tmp_path / "old.db"))
        c.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, "
                  "timestamp TEXT NOT NULL, owner_id TEXT NOT NULL)")
        db.init_db(c)
        db.migrate_schema(c)
        cols = {r[1] for r in c.execute("PRAGMA table_info(memories)")}
        assert {"local_id", "agent_name", "session_id", "turn_id"} <= cols
        c.close()

    def test_porter_fts_migrated_to_trigram(self, tmp_path):
        c = db.get_connection(str(tmp_path / "p.db"))
        c.execute(db._MEMORIES_DDL)
        c.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, tokenize='porter unicode61')")
        c.execute("INSERT INTO memories VALUES ('a','東京タワーの高さ','t','o',NULL,NULL,NULL,NULL)")
        c.execute("INSERT INTO memories_fts(rowid, content) VALUES (1,'東京タワーの高さ')")
        c.commit()
        db.init_db(c)
        assert "trigram" in c.execute("SELECT sql FROM sqlite_master WHERE name='memories_fts'").fetchone()[0]
        assert c.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 1
        c.close()

    def test_pragmas(self, conn):
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


class TestCrud:
    def test_insert_hits_all_tables_and_delete_removes_all(self, conn, cfg):
        rowid = add(conn, cfg, "東京は日本の首都です")
        for t in ("memories", "memories_fts", "memories_fts_bi", "memories_vec"):
            assert conn.execute(f"SELECT COUNT(*) FROM {t} WHERE rowid=?", (rowid,)).fetchone()[0] == 1
        mid = conn.execute("SELECT id FROM memories").fetchone()[0]
        assert db.delete_memory(conn, mid) is True
        for t in ("memories", "memories_fts", "memories_fts_bi", "memories_vec"):
            assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
        assert db.delete_memory(conn, "missing") is False

    def test_exact_duplicate_scope(self, conn, cfg):
        add(conn, cfg, "same text", turn_id="T1")
        assert db.check_exact_duplicate(conn, "same text", turn_id="T1")
        assert not db.check_exact_duplicate(conn, "same text", turn_id="T2")
        assert db.check_exact_duplicate(conn, "same text")  # no turn: global
        assert not db.check_exact_duplicate(conn, "other")

    def test_unindexed_detection(self, conn, cfg):
        add(conn, cfg, "no vector here", embed=False)
        rows = db.find_unindexed_memories(conn)
        assert len(rows) == 1 and rows[0]["has_vec"] == 0 and rows[0]["has_fts"] == 1

    def test_old_records_are_kept_and_time_filters(self, conn, cfg):
        add(conn, cfg, "ancient fact", ts="2020-01-01T00:00:00.000000+00:00")
        add(conn, cfg, "recent fact", ts="2026-06-01T00:00:00.000000+00:00")
        assert db.count_memories(conn) == 2
        assert len(db.get_all_memories(conn, since="2026-01-01T00:00:00+00:00")) == 1
        assert len(db.get_all_memories(conn, until="2021-01-01T00:00:00+00:00")) == 1

    def test_vector_roundtrip(self):
        v = fake_embed("hello world")
        assert db.deserialize_vector(db.serialize_vector(v))[:5] == pytest.approx(v[:5], rel=1e-5)


class TestDedupConcurrent:
    def test_same_turn_same_text_saved_once(self, conn, cfg):
        barrier = threading.Barrier(2)
        results = []

        def worker():
            c = db.get_connection(n3memory.DB_PATH)
            try:
                barrier.wait()
                results.append(n3memory._do_buffer(c, cfg, "[user] hello", turn_id="T", embed=False))
            finally:
                c.close()

        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(r["status"] for r in results) == ["ok", "skipped"]
        assert db.count_memories(conn) == 1

    def test_insert_once_returns_none_for_duplicate(self, conn, cfg):
        args = ("[user] x", "2026-01-01T00:00:00+00:00", cfg["owner_id"])
        assert db.insert_memory_once(conn, "id1", *args, turn_id="T") is not None
        assert db.insert_memory_once(conn, "id2", *args, turn_id="T") is None
        assert not conn.in_transaction


class TestFts:
    def test_query_building(self):
        assert db._build_fts_query("AI 東京") == ""  # all terms shorter than 3
        q = db._build_fts_query("「アルファ9」の気温は？")
        assert q == '"アルフ" OR "ルファ" OR "ファ9" OR "の気温" OR "気温は"'  # spec example
        assert len(db._build_fts_query(" ".join(f"word{i}" for i in range(100))).split(" OR ")) == 40

    def test_bigram_text(self):
        assert db.bigram_text("N3MCの設定") == "N3MC の設 設定"

    def test_symbols_do_not_raise(self, conn, cfg):
        add(conn, cfg, r"path C:\Users\test #tag = @a * $b % AND OR NOT")
        assert db.search_fts(conn, r'C:\Users\test #tag = @a * $b % AND OR NOT "q"')

    def test_punctuation_tolerance(self, conn, cfg):
        add(conn, cfg, "架空の惑星「アルファ9」の気温設定")
        assert db.search_fts(conn, "アルファ9の気温")

    def test_bigram_finds_two_char_terms(self, conn, cfg):
        add(conn, cfg, "記憶の保存について説明する")
        assert db.search_fts_bigram(conn, "記憶")
        assert not db.search_fts(conn, "記憶")  # trigram cannot see 2-char words


class TestMaths:
    def test_cosine(self):
        assert pr.cosine_sim_from_l2(0.0) == 1.0
        assert pr.cosine_sim_from_l2(2.0 ** 0.5) == pytest.approx(0.0, abs=1e-9)
        assert pr.cosine_sim_from_l2(2.0) == 0.0  # clamped

    def test_time_decay(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        assert pr.time_decay(now.isoformat(), 90, now) == pytest.approx(1.0)
        assert pr.time_decay((now - timedelta(days=90)).isoformat(), 90, now) == pytest.approx(0.5, rel=1e-6)
        assert pr.time_decay("garbage", 90, now) == 1.0

    def test_keyword_relevance(self):
        assert pr.keyword_relevance(-0.05, 5.0, 0.1) == 0.0
        assert pr.keyword_relevance(-5.0, 5.0, 0.1) == 1.0
        assert pr.keyword_relevance(-2.5, 5.0, 0.1) == pytest.approx(0.5)
        assert pr.keyword_relevance(-0.5, 0.5, 0.1) == pytest.approx(0.5)  # max(1.0, max)


class TestText:
    def test_purify(self):
        assert pr.purify_text("a\n```py\nprint(1)\n```\nb") == "a\n[code omitted]\nb"
        assert pr.purify_text("use `x` here") == "use `x` here"
        assert pr.purify_text("```unclosed\ncode") == "```unclosed\ncode"
        assert pr.purify_text("```\na\n```\nmid\n```\nb\n```").count("[code omitted]") == 2

    def test_sanitize_surrogates(self):
        assert pr.sanitize_surrogates("a\ud83db") == "ab"
        assert pr.sanitize_surrogates("😀") == "😀"
        assert pr.sanitize_surrogates({"k": ["x\ude00"]}) == {"k": ["x"]}
        assert pr.sanitize_surrogates(None) is None

    @pytest.mark.parametrize("text", [
        "x" * 10 + "\n\n" + "y" * 400,
        "。".join(f"これは{i}番目の文です" for i in range(300)) + "。",
        " ".join(f"Sentence number {i} is here." for i in range(200)),
        "word" * 500,
    ])
    def test_chunks_never_exceed_limit_and_keep_all_text(self, text):
        chunks = pr.chunk_text(text, 400, 40)
        assert chunks and all(len(c) <= 400 for c in chunks)
        joined = "".join(chunks)
        for ch in set(text) - set(" \n"):
            assert ch in joined

    def test_chunk_boundary_example_keeps_all_410_chars(self):
        chunks = pr.chunk_text("x" * 10 + "\n\n" + "y" * 400)
        joined = "".join(chunks)
        assert joined.count("x") >= 10 and joined.count("y") >= 400 and max(map(len, chunks)) <= 400

    def test_japanese_splits_after_sentence_end(self):
        text = "".join(f"これは第{i}の文章です。" for i in range(120))
        assert all(c.rstrip().endswith("。") or len(c) < 400 for c in pr.chunk_text(text)[:-1])

    def test_prefixes(self):
        assert pr.add_chunk_prefixes(["a"], "user") == ["[user] a"]
        assert pr.add_chunk_prefixes(["a", "b"], "claude") == ["[claude 1/2] a", "[claude 2/2] b"]

    def test_short_text_is_single_chunk(self):
        assert pr.chunk_text("ok") == ["ok"]
        assert pr.chunk_text("   ") == []


class TestTinyCorpusKeywordRecall:
    def test_single_record_keyword_hit_survives_without_embeddings(self, conn, cfg, monkeypatch):
        from n3memorycore import n3memory
        n3memory._do_buffer(conn, cfg, "ソラニアは空に浮かぶ架空の都市です", embed=False)
        monkeypatch.setattr(pr, "embed_query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
        res = pr.hybrid_search(conn, "ソラニアについて教えて", cfg)
        assert len(res["results"]) == 1

    def test_formula_unchanged_for_normal_corpora(self):
        assert pr.keyword_relevance(-0.05, 5.0, 0.1) == 0.0
        assert pr.keyword_relevance(-2.5, 5.0, 0.1) == pytest.approx(0.5)
        assert pr.keyword_relevance(-2e-6, 2e-6, 0.1, tiny_corpus=True) == 1.0
