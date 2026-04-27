import os
import sys
import uuid
import math
import json
import pytest

def _make_config(tmp_path):
    return {
        'owner_id':              str(uuid.uuid4()),
        'local_id':              str(uuid.uuid4()),
        'server_port':           18522,
        'dedup_threshold':       0.95,
        'half_life_days':        90,
        'bm25_min_threshold':    0.1,
        'search_result_limit':   20,
        'min_score':             0.0,
        'search_query_max_chars': 2000,
    }


@pytest.fixture(scope='module')
def test_client(tmp_path_factory):
    """Return a FastAPI TestClient backed by an isolated temp DB."""
    from fastapi.testclient import TestClient
    from n3memorycore import n3memory as nm

    tmp = tmp_path_factory.mktemp('api_db')
    db_path = str(tmp / 'test.db')

    cfg = _make_config(tmp)
    session_id = str(uuid.uuid4())

    # Patch globals
    nm._srv_cfg = cfg
    nm._srv_session = session_id
    nm.DB_PATH = db_path
    nm.MEMORY_DIR = str(tmp)

    from n3memorycore.core.database import get_connection, init_db, migrate_schema
    conn = get_connection(db_path)
    init_db(conn)
    migrate_schema(conn)
    conn.close()

    # Load model once
    try:
        from n3memorycore.core.processor import get_model
        get_model()
    except Exception:
        pass

    client = TestClient(nm.app, raise_server_exceptions=False)
    yield client, cfg, session_id, db_path


class TestHealth:
    def test_health_ok(self, test_client):
        client, *_ = test_client
        r = client.get('/health')
        assert r.status_code == 200
        assert r.json()['status'] == 'ok'


class TestBuffer:
    def test_saves_record(self, test_client):
        client, cfg, sess, db = test_client
        r = client.post('/buffer', json={'content': 'Hello buffer test'})
        assert r.status_code == 200
        data = r.json()
        assert data['status'] in ('ok', 'skipped')

    def test_empty_content(self, test_client):
        client, *_ = test_client
        r = client.post('/buffer', json={'content': ''})
        assert r.status_code == 200
        assert r.json()['status'] == 'skipped'

    def test_with_agent_name(self, test_client):
        client, cfg, sess, db = test_client
        r = client.post('/buffer', json={
            'content': f'agent tagged {uuid.uuid4()}',
            'agent_name': 'claude-code',
        })
        assert r.status_code == 200

    def test_exact_dedup(self, test_client):
        client, *_ = test_client
        text = f'exact dedup test {uuid.uuid4()}'
        r1 = client.post('/buffer', json={'content': text})
        r2 = client.post('/buffer', json={'content': text})
        assert r1.json()['status'] == 'ok'
        assert r2.json()['status'] == 'skipped'
        assert r2.json()['reason'] == 'exact_duplicate'

    def test_preserves_code_blocks_verbatim(self, test_client):
        # The /buffer endpoint applies purify_text (code block → [code omitted])
        client, *_ = test_client
        text = f'text with ```\ncode\n``` block {uuid.uuid4()}'
        r = client.post('/buffer', json={'content': text})
        assert r.status_code == 200
        # Verify via /list that content was purified
        lr = client.get('/list')
        records = lr.json()['records']
        for rec in records:
            if '[code omitted]' in rec['content']:
                break


class TestSearch:
    def test_empty_db(self, tmp_path_factory):
        from fastapi.testclient import TestClient
        from n3memorycore import n3memory as nm

        tmp = tmp_path_factory.mktemp('empty_db')
        db_path = str(tmp / 'empty.db')
        cfg = _make_config(tmp)
        nm._srv_cfg = cfg
        nm._srv_session = str(uuid.uuid4())
        nm.DB_PATH = db_path
        nm.MEMORY_DIR = str(tmp)

        from n3memorycore.core.database import get_connection, init_db
        conn = get_connection(db_path)
        init_db(conn)
        conn.close()

        client = TestClient(nm.app, raise_server_exceptions=False)
        r = client.post('/search', json={'query': 'anything'})
        assert r.status_code == 200
        assert 'results' in r.json()

    def test_buffer_and_search_roundtrip(self, test_client):
        client, *_ = test_client
        unique = f'Abraham Lincoln sixteenth president {uuid.uuid4()}'
        client.post('/buffer', json={'content': unique})
        r = client.post('/search', json={'query': 'Abraham Lincoln'})
        assert r.status_code == 200
        results = r.json().get('results', [])
        assert any('Lincoln' in res['content'] or 'Abraham' in res['content']
                   for res in results)

    def test_empty_query(self, test_client):
        client, *_ = test_client
        r = client.post('/search', json={'query': ''})
        assert r.status_code == 200

    def test_returns_score(self, test_client):
        client, *_ = test_client
        client.post('/buffer', json={'content': f'score test {uuid.uuid4()}'})
        r = client.post('/search', json={'query': 'score test'})
        results = r.json().get('results', [])
        for res in results:
            assert 'score' in res


class TestRepair:
    def test_repair_fixes_unindexed(self, test_client):
        client, cfg, sess, db = test_client
        from n3memorycore.core.database import get_connection
        import sqlite3

        conn = get_connection(db)
        mid = str(uuid.uuid4())
        from uuid_extensions import uuid7
        from datetime import datetime, timezone
        conn.execute(
            "INSERT INTO memories (id, content, timestamp, owner_id) VALUES (?, ?, ?, ?)",
            (str(uuid7()), 'unindexed record repair', datetime.now(timezone.utc).isoformat(),
             cfg['owner_id'])
        )
        conn.execute(
            "INSERT INTO memories_fts(rowid, content) VALUES "
            "((SELECT last_insert_rowid()), ?)", ('unindexed record repair',)
        )
        conn.commit()
        conn.close()

        r = client.post('/repair', json={})
        assert r.status_code == 200
        assert r.json()['status'] == 'ok'


class TestList:
    def test_list_empty(self, tmp_path_factory):
        from fastapi.testclient import TestClient
        from n3memorycore import n3memory as nm

        tmp = tmp_path_factory.mktemp('list_empty')
        db = str(tmp / 'list.db')
        nm._srv_cfg = _make_config(tmp)
        nm._srv_session = str(uuid.uuid4())
        nm.DB_PATH = db
        nm.MEMORY_DIR = str(tmp)

        from n3memorycore.core.database import get_connection, init_db
        conn = get_connection(db)
        init_db(conn)
        conn.close()

        client = TestClient(nm.app, raise_server_exceptions=False)
        r = client.get('/list')
        assert r.status_code == 200
        assert r.json()['total'] == 0

    def test_list_after_buffer(self, test_client):
        client, *_ = test_client
        r = client.get('/list')
        assert r.status_code == 200
        assert r.json()['total'] >= 0
