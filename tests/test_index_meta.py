"""v1.8.0: the meta table, model-mismatch notice, --reembed (no model: embeddings are faked)."""
import os

import pytest

from n3memorycore import n3memory as n3m
from n3memorycore.core import database as db
from n3memorycore.core import processor as p

from .conftest import add, fake_embed

pytestmark = pytest.mark.usefixtures("use_fake_embed")


def test_init_db_creates_meta(conn):
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='meta'").fetchone()


def test_repair_writes_the_keys(conn, cfg):
    add(conn, cfg, "something to index")
    n3m._repair(conn, cfg)
    assert db.get_meta(conn, "fts_punct_cleaned") == "1" and db.get_meta(conn, "fts_bigram") == "1"
    assert db.get_meta(conn, "embed_model") == cfg["embed_model"] and db.get_meta(conn, "embed_dim") == "768"
    assert db.get_meta(conn, "embed_model_at")


def test_old_database_without_meta_keys_gets_its_bigram_rows(conn, cfg):
    add(conn, cfg, "記憶の設定", embed=False)
    conn.execute("DELETE FROM memories_fts_bi")
    db.delete_meta(conn, "fts_bigram")
    conn.commit()
    n3m._repair(conn, cfg)
    assert conn.execute("SELECT COUNT(*) FROM memories_fts_bi").fetchone()[0] == 1


def test_vec_model_txt_used_only_when_vectors_exist(conn, cfg):
    open(os.path.join(n3m.MEMORY_DIR, "vec_model.txt"), "w").write("old/model")
    n3m._repair(conn, cfg)  # no vectors yet -> configured model
    assert db.get_meta(conn, "embed_model") == cfg["embed_model"]
    db.delete_meta(conn, "embed_model")
    add(conn, cfg, "x")  # a vector now exists
    n3m._repair(conn, cfg)
    assert db.get_meta(conn, "embed_model") == "old/model"
    assert os.path.exists(os.path.join(n3m.MEMORY_DIR, "vec_model.txt"))  # never deleted


def test_mismatch_notice_appears_and_clears(conn, cfg):
    add(conn, cfg, "x")
    n3m._repair(conn, cfg)
    assert "embed_model_mismatch" not in n3m._notices_read()
    n3m._repair(conn, {**cfg, "embed_model": "intfloat/e5-base-v2"})
    msg = n3m._notices_read()["embed_model_mismatch"]["message"]
    assert "n3mc --reembed" in msg and cfg["embed_model"] in msg and "intfloat/e5-base-v2" in msg
    n3m._repair(conn, cfg)
    assert "embed_model_mismatch" not in n3m._notices_read()


def test_punct_cleanup_runs_once_and_counts_changes(conn, cfg):
    add(conn, cfg, "「括弧」付きの文章です")
    conn.execute("UPDATE memories_fts SET content='「括弧」付きの文章です'")
    conn.commit()
    assert n3m._repair(conn, cfg) == 1
    assert n3m._repair(conn, cfg) == 0


@pytest.fixture
def reembed_env(monkeypatch, conn, cfg):
    calls = []

    def fake(text, name=None):
        calls.append(name)
        return fake_embed(text)

    monkeypatch.setattr(n3m, "embed_passage", fake)
    monkeypatch.setattr(p, "get_model", lambda name=None: object())
    monkeypatch.setattr(n3m._processor, "get_model", lambda name=None: object())
    return calls


def _vec(conn, rowid):
    return conn.execute("SELECT embedding FROM memories_vec WHERE rowid=?", (rowid,)).fetchone()[0]


def test_reembed_replaces_every_vector_and_writes_meta(conn, cfg, reembed_env):
    for i in range(5):
        db.insert_memory(conn, f"i{i}", f"record {i}", "2026-01-01T00:00:00+00:00", cfg["owner_id"],
                         embedding=[0.0] * 767 + [1.0])
    n3m._repair(conn, {**cfg, "embed_model": "intfloat/e5-base-v2"})  # records the old model
    db.set_meta(conn, "embed_model", "old/model")
    conn.commit()
    n3m._notify("embed_model_mismatch", "x")
    assert n3m.cmd_reembed() == 0
    c2 = db.get_connection()
    assert db.deserialize_vector(_vec(c2, 3))[:3] == pytest.approx(fake_embed("record 2")[:3], abs=1e-6)
    assert db.get_meta(c2, "embed_model") == cfg["embed_model"] and db.get_meta(c2, "reembed_cursor") is None
    assert "embed_model_mismatch" not in n3m._notices_read()
    c2.close()


def test_reembed_resumes_from_the_cursor(conn, cfg, reembed_env, monkeypatch):
    for i in range(3):
        add(conn, cfg, f"record {i}")
    db.set_meta(conn, "reembed_model", cfg["embed_model"])
    db.set_meta(conn, "reembed_cursor", 2)
    conn.commit()
    seen = []
    monkeypatch.setattr(n3m, "embed_passage", lambda t, name=None: seen.append(t) or fake_embed(t))
    n3m.cmd_reembed()
    assert [s for s in seen if s.startswith("record")] == ["record 2"]


def test_reembed_refuses_a_different_dimension(conn, cfg, monkeypatch, capsys):
    add(conn, cfg, "x")
    before = _vec(conn, 1)
    monkeypatch.setattr(n3m._processor, "get_model", lambda name=None: object())
    monkeypatch.setattr(n3m, "embed_passage", lambda t, name=None: [0.1] * 1024)
    assert n3m.cmd_reembed() == 1
    assert "1024-dimensional" in capsys.readouterr().err and _vec(conn, 1) == before
