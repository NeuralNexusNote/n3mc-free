import math
import os
import uuid
import pytest


_PATH_ATTRS = {
    'HOME_DIR':               '',
    'MEMORY_DIR':             '.memory',
    'DB_PATH':                os.path.join('.memory', 'n3memory.db'),
    'PID_FILE':               os.path.join('.memory', 'n3mc.pid'),
    'TURN_ID_FILE':           os.path.join('.memory', 'turn_id.txt'),
    'CONTEXT_FILE':           os.path.join('.memory', 'memory_context.md'),
    'CONTEXT_RETIRED_MARKER': os.path.join('.memory', '.memory_context_retired'),
    'NOTICES_FILE':           os.path.join('.memory', 'notices.json'),
    'AUDIT_LOG':              os.path.join('.memory', 'audit.log'),
    'CONFIG_FILE':            'config.json',
    'SERVER_LOG':             os.path.join('.memory', 'server.log'),
    'SERVER_START_FAILED_MARKER': os.path.join('.memory', 'server_start_failed'),
}

# Hook modules write audit.log themselves (spec §7, v1.5.0).
_HOOK_PATH_ATTRS = {
    'MEMORY_DIR': '.memory',
    'AUDIT_LOG':  os.path.join('.memory', 'audit.log'),
}


@pytest.fixture(autouse=True)
def _isolate_real_data(tmp_path, monkeypatch):
    """spec §7 (mandatory): never touch the real ~/.n3mc, the real
    ~/.claude/settings.json or a real server.

    Every data path of the n3memory module (and the hook modules' audit.log)
    points into a temp folder, `--init` writes a temporary settings.json,
    and HTTP / shutdown / process-start helpers are replaced by fakes that
    never reach the network. Tests that need a server answer monkeypatch
    these again. The Host check is relaxed for TestClient ("testserver");
    TestServerApi restores the real check.
    """
    from n3memorycore import n3memory, n3mc_hook, n3mc_stop_hook

    home = tmp_path / 'n3mc_home'
    (home / '.memory').mkdir(parents=True)
    for attr, rel in _PATH_ATTRS.items():
        if hasattr(n3memory, attr):
            monkeypatch.setattr(n3memory, attr, str(home / rel) if rel else str(home))
    for mod in (n3mc_hook, n3mc_stop_hook):
        for attr, rel in _HOOK_PATH_ATTRS.items():
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, str(home / rel))

    # --init: a temporary ~/.claude/settings.json (never the real one)
    user_home = tmp_path / 'user_home'
    (user_home / '.claude').mkdir(parents=True)
    if hasattr(n3memory, '_settings_path'):
        monkeypatch.setattr(n3memory, '_settings_path',
                            lambda: str(user_home / '.claude' / 'settings.json'))

    def _no_spawn(*args, **kwargs):
        raise AssertionError('tests must not start a real server process')
    if hasattr(n3memory, '_spawn_server'):
        monkeypatch.setattr(n3memory, '_spawn_server', _no_spawn)

    def _no_network(*args, **kwargs):
        raise ConnectionRefusedError('network disabled in tests')

    for attr in ('_post', '_get'):
        if hasattr(n3memory, attr):
            monkeypatch.setattr(n3memory, attr, _no_network)
    if hasattr(n3memory, '_request_shutdown'):
        monkeypatch.setattr(n3memory, '_request_shutdown', lambda *a, **k: False)
    if hasattr(n3memory, '_FOREIGN_SERVER_ROOT'):
        monkeypatch.setattr(n3memory, '_FOREIGN_SERVER_ROOT', None)
    for attr, val in (('_NO_SERVER_REASON', None), ('_QUICK_CHECK_DONE', False)):
        if hasattr(n3memory, attr):
            monkeypatch.setattr(n3memory, attr, val)
    if hasattr(n3memory, '_check_host'):
        real = n3memory._check_host
        monkeypatch.setattr(n3memory, '_host_ok',
                            lambda host: (host or '').lower() == 'testserver' or real(host))
    yield home


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
        'owner_id':               str(uuid.uuid4()),
        'local_id':               str(uuid.uuid4()),
        'server_port':            18521,
        'half_life_days':         90,
        'bm25_min_threshold':     0.1,
        'search_result_limit':    20,
        'min_score':              0.0,
        'search_query_max_chars': 2000,
    }


@pytest.fixture
def dummy_vec():
    return _dummy_vec


@pytest.fixture(scope='session')
def real_model():
    from n3memorycore.core.processor import get_model
    return get_model()
