"""
N3MemoryCore test fixtures
"""
import os
import sys
import struct
import tempfile
import pytest

# Add core to path
_CORE_DIR = os.path.join(os.path.dirname(__file__), "..", "core")
sys.path.insert(0, _CORE_DIR)
_N3MC_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _N3MC_DIR)


@pytest.fixture
def tmp_db(tmp_path):
    """Provide an isolated in-memory or temp-file SQLite DB."""
    from database import get_connection, init_db
    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def dummy_vec():
    """Return a deterministic 768-dim unit vector."""
    import math
    v = [1.0 / math.sqrt(768)] * 768
    return v


@pytest.fixture
def base_config(tmp_path):
    """Minimal config for testing."""
    return {
        "owner_id": "test-owner-id",
        "local_id": "test-local-id",
        "server_port": 18521,
        "dedup_threshold": 0.95,
        "half_life_days": 90,
        "bm25_min_threshold": 0.1,
        "search_result_limit": 20,
        "context_char_limit": 3000,
        "min_score": 0.0,
        "search_query_max_chars": 2000,
    }


@pytest.fixture(scope="session")
def embedding_model():
    """Load the embedding model once per session."""
    from processor import get_model
    model = get_model()
    yield model
