"""v1.8.0 (spec §3.5 "Index metadata"): the meta table, the model-mismatch
notice and `--reembed` (design review of 2026-11-17, proposal 3).

No embedding model: vectors and the model are fakes. Real data and real
servers are never touched (conftest._isolate_real_data).
"""
import os

import numpy as np
import pytest

from n3memorycore import n3memory
from n3memorycore.core import database as db
from n3memorycore.core import processor as proc

from tests.test_regressions import DIM, add, unit


class FakeModel:
    def __init__(self, vec, dim=None):
        self.vec = np.asarray(vec, dtype=np.float32)
        if dim is not None:
            self.vec = np.ones(dim, dtype=np.float32) / np.sqrt(dim)
        self.seen = []

    def encode(self, texts, normalize_embeddings=True, **kw):
        if isinstance(texts, str):
            self.seen.append(texts)
            return self.vec.copy()
        self.seen.extend(texts)
        return np.vstack([self.vec for _ in texts])


def fresh_db():
    os.makedirs(n3memory.MEMORY_DIR, exist_ok=True)
    conn = db.get_connection(n3memory.DB_PATH)
    try:
        db.init_db(conn)
    finally:
        conn.close()
    return n3memory.DB_PATH


def meta(key):
    conn = db.get_connection(n3memory.DB_PATH)
    try:
        return db.meta_get(conn, key)
    finally:
        conn.close()


def notices():
    return n3memory._load_notices()


@pytest.fixture
def model_cfg(monkeypatch):
    def set_model(name):
        monkeypatch.setattr(n3memory, '_srv_cfg', {'embed_model': name}, raising=False)
    set_model('model-a')
    return set_model


class TestMetaTable:
    def test_init_db_creates_meta(self, tmp_path):
        conn = db.get_connection(str(tmp_path / 'x.db'))
        try:
            db.init_db(conn)
            db.meta_set(conn, 'k', 'v')
            conn.commit()
            assert db.meta_get(conn, 'k') == 'v'
            assert db.vec_table_dim(conn) == DIM
        finally:
            conn.close()

    def test_repair_writes_keys_not_files(self, model_cfg):
        path = fresh_db()
        add(path, 'a record, with punctuation!', emb=unit(1))
        n3memory._repair_db()
        assert meta('fts_punct_cleaned') == '1' and meta('fts_bigram') == '1'
        assert meta('embed_model') == 'model-a' and meta('embed_dim') == str(DIM)
        assert meta('embed_model_at')
        for name in ('fts_punct_cleaned', 'fts_bigram_v1', 'vec_model.txt'):
            assert not os.path.exists(os.path.join(n3memory.MEMORY_DIR, name))

    def test_restored_db_next_to_old_markers_gets_bigram_index(self, model_cfg):
        path = fresh_db()
        add(path, '記憶の保存先', emb=unit(1))
        add(path, 'PR を作った', emb=unit(2))
        conn = db.get_connection(path)
        try:   # an older backup: no bigram rows, no meta keys
            conn.execute("DELETE FROM memories_fts_bi")
            conn.commit()
        finally:
            conn.close()
        for name in ('fts_bigram_v1', 'fts_punct_cleaned'):     # the newer DB's v1.7.0 markers
            with open(os.path.join(n3memory.MEMORY_DIR, name), 'w', encoding='utf-8') as f:
                f.write('done')
        n3memory._repair_db()
        conn = db.get_connection(path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM memories_fts_bi").fetchone()[0] == 2
        finally:
            conn.close()


class TestEmbedModelRecord:
    def _legacy(self, name):
        with open(os.path.join(n3memory.MEMORY_DIR, 'vec_model.txt'), 'w', encoding='utf-8') as f:
            f.write(name)

    def test_empty_db_takes_the_configured_model(self, model_cfg):
        fresh_db()
        self._legacy('old-model')
        n3memory._repair_db()
        assert meta('embed_model') == 'model-a'
        assert 'embed_model_mismatch' not in notices()

    def test_vectors_take_the_legacy_file_and_mismatch_is_a_notice(self, model_cfg):
        path = fresh_db()
        add(path, 'made with the old model', emb=unit(1))
        self._legacy('old-model')
        n3memory._repair_db()
        assert meta('embed_model') == 'old-model'
        msg = notices()['embed_model_mismatch']['message']
        assert 'old-model' in msg and 'model-a' in msg and 'n3mc --reembed' in msg
        model_cfg('old-model')                     # set back: the notice goes away
        n3memory._repair_db()
        assert 'embed_model_mismatch' not in notices()

    def test_vectors_without_legacy_file_take_the_configured_model(self, model_cfg):
        path = fresh_db()
        add(path, 'moved from another PC', emb=unit(1))
        n3memory._repair_db()
        assert meta('embed_model') == 'model-a'


class TestReembed:
    def _setup(self, model_cfg, n=3):
        path = fresh_db()
        ids = [add(path, f'record {i}', emb=unit(i + 1)) for i in range(n)]
        self_vec = unit(1)
        model_cfg('old-model')
        n3memory._repair_db()                       # meta.embed_model = old-model
        model_cfg('new-model')
        n3memory._repair_db()                       # mismatch notice
        assert 'embed_model_mismatch' in notices()
        return path, ids, self_vec

    def _vectors(self, path):
        conn = db.get_connection(path)
        try:
            rowids = [r[0] for r in conn.execute("SELECT rowid FROM memories ORDER BY rowid")]
            return [db.get_vector(conn, r) for r in rowids], rowids
        finally:
            conn.close()

    def test_reembed_replaces_every_vector(self, model_cfg, monkeypatch):
        path, ids, _ = self._setup(model_cfg)
        fake = FakeModel(unit(7))
        monkeypatch.setattr(proc, 'get_model', lambda name=None: fake)
        assert n3memory.cmd_reembed({'embed_model': 'new-model'}) == 0
        vecs, _ = self._vectors(path)
        assert all(abs(v[7] - 1.0) < 1e-6 for v in vecs) and len(vecs) == 3
        assert meta('embed_model') == 'new-model'
        assert meta('reembed_model') is None and meta('reembed_cursor') is None
        assert 'embed_model_mismatch' not in notices()

    def test_reembed_resumes_from_the_cursor(self, model_cfg, monkeypatch):
        path, ids, _ = self._setup(model_cfg, n=4)
        _, rowids = self._vectors(path)
        conn = db.get_connection(path)
        try:
            db.meta_set(conn, 'reembed_model', 'new-model')
            db.meta_set(conn, 'reembed_cursor', str(rowids[1]))
            conn.commit()
        finally:
            conn.close()
        fake = FakeModel(unit(7))
        monkeypatch.setattr(proc, 'get_model', lambda name=None: fake)
        assert n3memory.cmd_reembed({'embed_model': 'new-model'}) == 0
        assert [t for t in fake.seen if t != 'passage: probe'] == ['passage: record 2', 'passage: record 3']
        vecs, _ = self._vectors(path)
        assert abs(vecs[0][1] - 1.0) < 1e-6          # before the cursor: untouched
        assert abs(vecs[3][7] - 1.0) < 1e-6

    def test_another_dimension_is_refused(self, model_cfg, monkeypatch, capsys):
        path, ids, _ = self._setup(model_cfg)
        before, _ = self._vectors(path)
        monkeypatch.setattr(proc, 'get_model', lambda name=None: FakeModel(None, dim=384))
        assert n3memory.cmd_reembed({'embed_model': 'small-model'}) == 1
        assert '384-dimensional' in capsys.readouterr().err
        after, _ = self._vectors(path)
        assert before == after and meta('embed_model') == 'old-model'
        assert meta('reembed_model') is None
