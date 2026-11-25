"""Layer 5: the meta table, the model-mismatch notice and --reembed. No model needed."""
import json

import pytest

from n3memorycore import n3memory
from n3memorycore.core.database import (
    get_meta, set_meta, delete_meta, get_vector, get_connection, init_db, vec_table_dim, count_memories,
    search_fts,
)
from tests.conftest import add, fake_embed, FakeModel, use_model, basis


def _notices():
    try:
        with open(n3memory.NOTICES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def _set_config(isolate, **kw):
    path = isolate["data"] / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(kw)
    path.write_text(json.dumps(data), encoding="utf-8")


def _to_384(conn):
    conn.execute("DROP TABLE memories_vec")
    conn.execute("CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[384])")
    for key in ("embed_model", "embed_dim", "embed_model_at"):
        delete_meta(conn, key)
    conn.commit()


class TestMetaTable:
    def test_init_db_creates_meta(self, conn):
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'meta'").fetchone()

    def test_repair_writes_meta_keys(self, conn, cfg):
        add(conn, "a record", fake_embed("a"))
        n3memory.run_repair(conn, cfg)
        assert get_meta(conn, "fts_punct_cleaned") == "1"
        assert get_meta(conn, "fts_bigram") == "1"
        assert get_meta(conn, "embed_model") == cfg["embed_model"]
        assert get_meta(conn, "embed_dim") == "768"
        assert get_meta(conn, "fts_text_rules") == "2"

    def test_old_db_next_to_marker_files_gets_bigrams(self, conn, cfg, isolate):
        for marker in ("fts_bigram_v1", "fts_punct_cleaned", "vec_model.txt"):
            (isolate["mem"] / marker).write_text("1", encoding="utf-8")
        add(conn, "復元した記録")
        conn.execute("DELETE FROM memories_fts_bi")
        conn.execute("DELETE FROM meta")
        conn.commit()
        n3memory.run_repair(conn, cfg)
        assert conn.execute("SELECT COUNT(*) FROM memories_fts_bi").fetchone()[0] == 1

    def test_vec_model_txt_used_only_with_vectors(self, conn, cfg, isolate):
        (isolate["mem"] / "vec_model.txt").write_text("old/model", encoding="utf-8")
        add(conn, "no vector yet")
        n3memory.run_repair(conn, cfg)
        assert get_meta(conn, "embed_model") == cfg["embed_model"]
        delete_meta(conn, "embed_model")
        add(conn, "with vector", fake_embed("v"))
        n3memory.run_repair(conn, cfg)
        assert get_meta(conn, "embed_model") == "old/model"

    def test_mismatch_notice_and_clear(self, conn, cfg):
        add(conn, "x", fake_embed("x"))
        set_meta(conn, "embed_model", "some/other-model")
        n3memory.run_repair(conn, cfg)
        assert "embed_model_mismatch" in _notices()
        assert "n3mc --reembed" in _notices()["embed_model_mismatch"]["message"]
        set_meta(conn, "embed_model", cfg["embed_model"])
        n3memory.run_repair(conn, cfg)
        assert "embed_model_mismatch" not in _notices()


class TestReembed:
    def test_reembed_384_dimension_table(self, conn, monkeypatch, isolate):
        for i in range(3):
            add(conn, f"record {i}")
        _to_384(conn)
        conn.close()
        use_model(monkeypatch, FakeModel(dim=384))
        assert n3memory.cmd_reembed() == 0
        c = get_connection(n3memory.DB_PATH)
        assert c.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == count_memories(c) == 3
        assert get_meta(c, "embed_dim") == "384"
        c.close()

    def test_different_dimension_refused(self, conn, monkeypatch, capsys):
        add(conn, "kept", basis(0))
        conn.close()
        use_model(monkeypatch, FakeModel(dim=384))
        assert n3memory.cmd_reembed() == 1
        assert "makes 384-dimensional vectors but memories_vec has 768" in capsys.readouterr().err
        c = get_connection(n3memory.DB_PATH)
        assert get_vector(c, 1)[:2] == [1.0, 0.0]
        c.close()

    def test_reembed_replaces_vectors_with_the_configured_model(self, conn, monkeypatch, isolate, cfg, capsys):
        for i in range(3):
            add(conn, f"record {i}", basis(0))
        set_meta(conn, "embed_model", "old/model")
        n3memory._notify("embed_model_mismatch", "mismatch")
        conn.close()
        _set_config(isolate, embed_model="new/model")
        model = use_model(monkeypatch, FakeModel())
        assert n3memory.cmd_reembed() == 0
        c = get_connection(n3memory.DB_PATH)
        for rowid in (1, 2, 3):
            content = c.execute("SELECT content FROM memories WHERE rowid = ?", (rowid,)).fetchone()[0]
            assert get_vector(c, rowid) == pytest.approx(fake_embed("passage: " + content), abs=1e-6)
        assert get_meta(c, "embed_model") == "new/model"
        assert get_meta(c, "reembed_cursor") is None and get_meta(c, "reembed_model") is None
        c.close()
        assert "embed_model_mismatch" not in _notices()
        assert "Re-embedded 3 record(s) with new/model." in capsys.readouterr().out
        assert len(model.calls) == 4  # the probe + 3 records

    def test_reembed_resumes_from_cursor(self, conn, monkeypatch, cfg):
        for i in range(4):
            add(conn, f"record {i}", basis(0))
        set_meta(conn, "reembed_model", cfg["embed_model"])
        set_meta(conn, "reembed_cursor", 2)
        conn.close()
        model = use_model(monkeypatch, FakeModel())
        assert n3memory.cmd_reembed() == 0
        c = get_connection(n3memory.DB_PATH)
        assert get_vector(c, 1)[:2] == [1.0, 0.0] and get_vector(c, 2)[:2] == [1.0, 0.0]
        assert get_vector(c, 3)[:2] != [1.0, 0.0]
        c.close()
        assert len(model.calls) == 3


class TestFtsTextRules:
    def _old_rows(self, conn):
        add(conn, "[claude] 白樺の記憶の設定です")
        add(conn, "[user] ログの調べ方")
        for rowid, content in conn.execute("SELECT rowid, content FROM memories").fetchall():
            conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
            conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (rowid, content))
        conn.commit()
        delete_meta(conn, "fts_text_rules")

    def test_rebuild_old_rows(self, conn, cfg):
        self._old_rows(conn)
        assert search_fts(conn, "claude")
        assert n3memory.run_repair(conn, cfg) >= 2
        assert search_fts(conn, "claude") == []
        assert get_meta(conn, "fts_text_rules") == "2"
        assert n3memory.run_repair(conn, cfg) == 0

    def test_max_records_continues_from_cursor(self, conn, cfg):
        n3memory.run_repair(conn, cfg)  # one-time steps done, rules written
        for i in range(5):
            add(conn, f"[claude] row {i}")
        for rowid, content in conn.execute("SELECT rowid, content FROM memories").fetchall():
            conn.execute("UPDATE memories_fts SET content = ? WHERE rowid = ?", (content, rowid))
        conn.commit()
        delete_meta(conn, "fts_text_rules")
        n3memory.run_repair(conn, cfg, max_records=2)
        assert get_meta(conn, "fts_text_cursor") == "2"
        assert get_meta(conn, "fts_text_rules") is None
        n3memory.run_repair(conn, cfg, max_records=2)
        assert get_meta(conn, "fts_text_cursor") == "4"
        n3memory.run_repair(conn, cfg, max_records=2)
        n3memory.run_repair(conn, cfg, max_records=2)
        assert get_meta(conn, "fts_text_rules") == "2"
        assert get_meta(conn, "fts_text_cursor") is None
        assert search_fts(conn, "claude") == []
