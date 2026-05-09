"""Layer 3: FastAPI endpoint tests via TestClient (spec §7)."""
import math
import uuid
import pytest

from fastapi.testclient import TestClient


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    """Isolated FastAPI app with a temp DB."""
    import os
    from n3memorycore import n3memory

    tmp = tmp_path_factory.mktemp('api_db')
    db_path = str(tmp / 'test.db')

    # Patch module-level globals so the app uses our temp DB/config
    original_db   = n3memory.DB_PATH
    original_cfg  = n3memory._srv_cfg
    original_sess = n3memory._srv_session

    cfg = {
        'owner_id':               str(uuid.uuid4()),
        'local_id':               str(uuid.uuid4()),
        'server_port':            18521,
        'dedup_threshold':        0.95,
        'half_life_days':         90,
        'bm25_min_threshold':     0.1,
        'search_result_limit':    20,
        'min_score':              0.0,
        'search_query_max_chars': 2000,
        'embed_model':            'intfloat/multilingual-e5-base',
    }

    n3memory.DB_PATH      = db_path
    n3memory._srv_cfg     = cfg
    n3memory._srv_session = str(uuid.uuid4())

    from n3memorycore.core.database import get_connection, init_db
    conn = get_connection(db_path)
    init_db(conn)
    conn.close()

    # Ensure model is loaded
    from n3memorycore.core.processor import get_model
    get_model(cfg.get('embed_model'))

    tc = TestClient(n3memory.app, raise_server_exceptions=True)
    yield tc

    n3memory.DB_PATH      = original_db
    n3memory._srv_cfg     = original_cfg
    n3memory._srv_session = original_sess


# ---------------------------------------------------------------------------
class TestHealth:

    def test_health_ok(self, client):
        resp = client.get('/health')
        assert resp.status_code == 200
        assert resp.json()['status'] == 'ok'


# ---------------------------------------------------------------------------
class TestBuffer:

    def test_save_content(self, client):
        resp = client.post('/buffer', json={'content': '保存テスト: テキスト内容'})
        assert resp.status_code == 200
        data = resp.json()
        assert data['status'] in ('ok', 'skipped')

    def test_empty_content_skipped(self, client):
        resp = client.post('/buffer', json={'content': ''})
        assert resp.status_code == 200
        assert resp.json()['status'] == 'skipped'

    def test_agent_name_saved(self, client):
        resp = client.post('/buffer', json={'content': 'agent test content xyz123', 'agent_name': 'test-agent'})
        assert resp.status_code == 200

    def test_exact_dedup(self, client):
        text = f'dedup-test-{uuid.uuid4()}'
        r1 = client.post('/buffer', json={'content': text})
        r2 = client.post('/buffer', json={'content': text})
        assert r1.json()['status'] == 'ok'
        assert r2.json()['status'] == 'skipped'

    def test_code_block_purified(self, client):
        text = 'Some text\n```python\nprint("secret")\n```\nend'
        resp = client.post('/buffer', json={'content': text})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
class TestSearch:

    def test_empty_db_search(self, client, tmp_path):
        import os
        from n3memorycore import n3memory as nm
        old_db = nm.DB_PATH
        fresh_db = str(tmp_path / 'empty.db')
        from n3memorycore.core.database import get_connection, init_db
        conn = get_connection(fresh_db)
        init_db(conn)
        conn.close()
        nm.DB_PATH = fresh_db
        resp = client.post('/search', json={'query': 'anything'})
        nm.DB_PATH = old_db
        assert resp.status_code == 200
        assert 'results' in resp.json()

    def test_save_then_search(self, client):
        unique = f'坂本龍馬テスト-{uuid.uuid4()}'
        client.post('/buffer', json={'content': unique})
        resp = client.post('/search', json={'query': unique[:10]})
        assert resp.status_code == 200
        assert 'results' in resp.json()

    def test_empty_query(self, client):
        resp = client.post('/search', json={'query': ''})
        assert resp.status_code == 200
        assert 'results' in resp.json()

    def test_score_returned(self, client):
        unique = f'scoring metric result verification {uuid.uuid4()}'
        client.post('/buffer', json={'content': unique})
        resp = client.post('/search', json={'query': unique[:20]})
        data = resp.json()
        if data.get('results'):
            assert 'score' in data['results'][0]


# ---------------------------------------------------------------------------
class TestRepair:

    def test_repair_runs(self, client):
        resp = client.post('/repair', json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data['status'] == 'ok'
        assert 'count' in data


# ---------------------------------------------------------------------------
class TestList:

    def test_list_returns_records(self, client):
        resp = client.get('/list')
        assert resp.status_code == 200
        data = resp.json()
        assert 'records' in data
        assert 'total' in data

    def test_list_after_save(self, client):
        unique = f'list endpoint roundtrip check {uuid.uuid4()}'
        client.post('/buffer', json={'content': unique})
        resp = client.get('/list')
        data = resp.json()
        contents = [r['content'] for r in data['records']]
        # The saved text may be purified, but should appear
        assert any(unique[:20] in c for c in contents)


# ---------------------------------------------------------------------------
class TestDelete:

    def test_delete_returns_403(self, client):
        resp = client.delete('/delete/some-uuid')
        assert resp.status_code == 403

    def test_delete_nonexistent_also_403(self, client):
        resp = client.delete('/delete/does-not-exist')
        assert resp.status_code == 403

    def test_delete_detail_is_pro(self, client):
        resp = client.delete('/delete/some-uuid')
        assert 'Pro' in resp.json().get('detail', '')


# ---------------------------------------------------------------------------
class TestGC:

    def test_gc_returns_403(self, client):
        resp = client.post('/gc', json={})
        assert resp.status_code == 403

    def test_gc_detail_is_pro(self, client):
        resp = client.post('/gc', json={})
        assert 'Pro' in resp.json().get('detail', '')


# ---------------------------------------------------------------------------
class TestImport:

    def test_import_returns_403(self, client):
        resp = client.post('/import', json=[])
        assert resp.status_code == 403

    def test_import_with_data_returns_403(self, client):
        resp = client.post('/import', json={'records': []})
        assert resp.status_code == 403

    def test_import_detail_is_pro(self, client):
        resp = client.post('/import', json={})
        assert 'Pro' in resp.json().get('detail', '')
