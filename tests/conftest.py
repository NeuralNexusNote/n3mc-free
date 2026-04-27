import math
import uuid
import pytest


def _dummy_vec(val: float = 0.5, dim: int = 768) -> list:
    raw = [val] * dim
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


@pytest.fixture
def tmp_db(tmp_path):
    """Return path to a fresh, initialised DB."""
    from n3memorycore.core.database import get_connection, init_db
    db_path = str(tmp_path / 'test.db')
    conn = get_connection(db_path)
    init_db(conn)
    conn.close()
    return db_path


@pytest.fixture
def cfg(tmp_path):
    return {
        'owner_id':              str(uuid.uuid4()),
        'local_id':              str(uuid.uuid4()),
        'server_port':           18521,
        'dedup_threshold':       0.95,
        'half_life_days':        90,
        'bm25_min_threshold':    0.1,
        'search_result_limit':   20,
        'min_score':             0.0,
        'search_query_max_chars': 2000,
    }


@pytest.fixture
def dummy_vec():
    return _dummy_vec


@pytest.fixture(scope='session')
def real_model():
    from n3memorycore.core.processor import get_model
    return get_model()
