import sys
import os

# spec §3: reconfigure all streams to UTF-8 before any I/O (Windows cp932 guard)
for _s_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _s_name, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from .paths import HOME_DIR, MEMORY_DIR, DB_PATH, PID_FILE, AUDIT_LOG, CONFIG_FILE, NOTICES_FILE, claude_paths
from .paths import TURN_ID_FILE, CONTEXT_FILE, CONTEXT_RETIRED_MARKER
from .core.database import (
    get_connection, init_db, migrate_schema, insert_memory,
    check_exact_duplicate, search_vector, find_unindexed_memories,
    strip_fts_punctuation, serialize_vector,
)
from .core.processor import (
    embed_passage, cosine_sim_from_l2, hybrid_search,
    chunk_text, add_chunk_prefixes, purify_text, render_memory_context,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_CODE_FILES = (
    os.path.join(_PKG_DIR, 'n3memory.py'),
    os.path.join(_PKG_DIR, 'core', 'database.py'),
    os.path.join(_PKG_DIR, 'core', 'processor.py'),
)

# Set by ensure_server() when the port answers with another folder's server
# (spec §3 "When the port belongs to another folder's server"). None = not foreign.
_FOREIGN_SERVER_ROOT: Optional[str] = None

# spec §4.5: inputs that are not typed by the user — no search, no save,
# no new turn_id.
_SYSTEM_PROMPT_PREFIXES = ('<task-notification>',)

_HEADING = "# Recalled Memory Context"

# spec §3 Session ID fallback for direct (non-HTTP) writes from this CLI
# process: N3MC_SESSION_ID, else a per-process UUIDv4.
_CLI_SESSION_ID = os.environ.get('N3MC_SESSION_ID') or str(uuid.uuid4())

_FATAL_SAVE_MSG = "⚠️ Physical save failed. Current memories may be lost."


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


# ---------------------------------------------------------------------------
# Audit log + notices (spec §3 "Notices")
# ---------------------------------------------------------------------------

def _append_audit(record: dict) -> None:
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        rec = {'ts': datetime.now(timezone.utc).isoformat()}
        rec.update(record)
        with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f"Warning: audit log write failed: {e}", file=sys.stderr)


def _load_notices() -> dict:
    try:
        with open(NOTICES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_notices(notices: dict) -> None:
    try:
        os.makedirs(os.path.dirname(NOTICES_FILE), exist_ok=True)
        if not notices:
            if os.path.exists(NOTICES_FILE):
                os.remove(NOTICES_FILE)
            return
        with open(NOTICES_FILE, 'w', encoding='utf-8') as f:
            json.dump(notices, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Warning: notices write failed: {e}", file=sys.stderr)


def _notify(key: str, message: str) -> None:
    """A notice that needs a human: stderr + audit.log (once per text) +
    notices.json (shown at the top of search output once a day)."""
    print(f"N3MC: {message}", file=sys.stderr)
    notices = _load_notices()
    prev = notices.get(key)
    if prev and prev.get('message') == message:
        return
    _append_audit({'hook': 'notice', 'key': key, 'message': message})
    notices[key] = {'message': message, 'last_shown': None}
    _save_notices(notices)


def _clear_notice(key: str) -> None:
    notices = _load_notices()
    if key in notices:
        del notices[key]
        _save_notices(notices)


def _take_notices() -> list:
    """Notices due for display (not shown in the last 24 h)."""
    notices = _load_notices()
    if not notices:
        return []
    now = time.time()
    due = []
    for v in notices.values():
        last = v.get('last_shown')
        if not last or now - float(last) >= 86400:
            due.append(v.get('message', ''))
            v['last_shown'] = now
    if due:
        _save_notices(notices)
    return due


def _status_md(body: str) -> str:
    """Heading + due notices + body (used for every non-result output).

    Without notices this is exactly "# Recalled Memory Context\\n\\n<body>\\n"
    (spec §5.4 degraded-state placeholders)."""
    parts = [_HEADING, ""]
    for n in _take_notices():
        parts += [f"> ⚠️ N3MC notice: {n}", ""]
    parts.append(body)
    return "\n".join(parts) + "\n"


def _emit(text: str) -> None:
    """Write search output to stdout as-is (stdout only; no file)."""
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Config (spec §3 identifiers, §3.5)
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict = {
    "owner_id":                 None,
    "local_id":                 None,
    "bind_host":                "127.0.0.1",  # keep 127.0.0.1: the API has no auth
    "server_port":              18520,
    "embed_model":              "intfloat/multilingual-e5-base",
    "half_life_days":           90,
    "bm25_min_threshold":       0.1,
    "search_result_limit":      20,
    "min_score":                0.2,
    "search_query_max_chars":   2000,
    "vector_min_cos":           0.85,   # measured with multilingual-e5-base
    "relative_min_score_ratio": 0.6,
    "pair_top_n":               3,
    "pair_max_chars":           3000,
    "version_order_enabled":    True,
    "version_sim_threshold":    0.98,   # measured with multilingual-e5-base
    "version_min_chars":        100,
}


def _config_value_ok(key: str, value) -> bool:
    if key not in _DEFAULT_CONFIG:
        return True
    default = _DEFAULT_CONFIG[key]
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    return True


def _recover_ids_from_db(cfg: dict) -> None:
    """spec §3.5: recover owner_id / local_id from the most frequent DB value."""
    try:
        import sqlite3
        if not os.path.exists(DB_PATH):
            return
        conn = sqlite3.connect(DB_PATH)
        try:
            for col in ('owner_id', 'local_id'):
                if cfg.get(col):
                    continue
                row = conn.execute(
                    f"SELECT {col} FROM memories WHERE {col} IS NOT NULL "
                    f"GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    cfg[col] = row[0]
        finally:
            conn.close()
    except Exception:
        pass


def _fill_ids(cfg: dict) -> None:
    _recover_ids_from_db(cfg)
    for col in ('owner_id', 'local_id'):
        if not cfg.get(col):
            cfg[col] = str(uuid.uuid4())


def _load_config() -> dict:
    """spec §3.5 (v1.4.0): never overwrite a config.json the user mistyped."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    cfg = dict(_DEFAULT_CONFIG)

    if not os.path.exists(CONFIG_FILE):
        _fill_ids(cfg)
        _save_config(cfg)
        return cfg

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("the top level is not a JSON object")
    except Exception as e:
        _notify(
            'config_unreadable',
            f"config.json cannot be read ({e}). N3MC runs with default "
            f"settings until it is fixed; the file was NOT modified: {CONFIG_FILE}",
        )
        _fill_ids(cfg)   # this run only — not saved
        return cfg
    _clear_notice('config_unreadable')

    bad = []
    for k, v in data.items():
        if _config_value_ok(k, v):
            cfg[k] = v
        else:
            bad.append(k)
    if bad:
        _notify(
            'config_type',
            "config.json: these items have the wrong type and are ignored "
            f"(defaults are used): {', '.join(sorted(bad))}. "
            "Write numbers without quotes, e.g. \"vector_min_cos\": 0.83.",
        )
    else:
        _clear_notice('config_type')

    for col in ('owner_id', 'local_id'):
        if not cfg.get(col):
            cfg[col] = None
    if not cfg.get('owner_id') or not cfg.get('local_id'):
        _fill_ids(cfg)

    missing = [k for k in _DEFAULT_CONFIG
               if k not in data or (data.get(k) is None and cfg.get(k) is not None)]
    if missing and not bad:
        merged = dict(data)
        for k in missing:
            merged[k] = cfg[k]
        _save_config(merged)
    return cfg


def _save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _code_version() -> str:
    """spec §3: hash of the server code + config.json content (key order and
    whitespace do not matter)."""
    h = hashlib.sha256()
    for path in _CODE_FILES:
        try:
            with open(path, 'rb') as f:
                h.update(f.read())
        except Exception:
            h.update(path.encode('utf-8'))
    try:
        with open(CONFIG_FILE, 'rb') as f:
            raw = f.read()
    except Exception:
        raw = b''
    try:
        obj = json.loads(raw.decode('utf-8'))
        h.update(json.dumps(obj, sort_keys=True).encode('utf-8'))
    except Exception:
        h.update(raw)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(cfg: dict, path: str, payload: dict, timeout: int = 30) -> dict:
    import urllib.request
    url = f"http://127.0.0.1:{cfg['server_port']}{path}"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _get(cfg: dict, path: str, timeout: int = 30) -> dict:
    import urllib.request
    url = f"http://127.0.0.1:{cfg['server_port']}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _get_health(cfg: dict) -> Optional[dict]:
    try:
        r = _get(cfg, '/health', timeout=5)
        return r if isinstance(r, dict) and r.get('status') == 'ok' else None
    except Exception:
        return None


def _health_check(cfg: dict) -> bool:
    return _get_health(cfg) is not None


def _health_decision(health: Optional[dict]) -> str:
    """spec §3: 'none' | 'ok' | 'stale' | 'foreign' | 'legacy'."""
    if not health:
        return 'none'
    root = health.get('root')
    if not root:
        return 'legacy'
    if _norm(root) != _norm(HOME_DIR):
        return 'foreign'
    if health.get('code_version') != _code_version():
        return 'stale'
    return 'ok'


# ---------------------------------------------------------------------------
# Server management (spec §3 "Server check and replacement (ensure_server)")
# ---------------------------------------------------------------------------

def _process_alive(pid: int) -> bool:
    try:
        if sys.platform == 'win32':
            import ctypes
            SYNCHRONIZE = 0x100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            ret = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
            ctypes.windll.kernel32.CloseHandle(handle)
            return ret == 0x102  # WAIT_TIMEOUT → still alive
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def _is_own_server_process(pid: int) -> bool:
    """True only if psutil confirms pid is `n3memorycore.n3memory --run-server`
    whose data folder (N3MC_HOME) is ours."""
    try:
        import psutil
    except ImportError:
        return False
    if not pid:
        return False
    try:
        p = psutil.Process(int(pid))
        cmdline = ' '.join(p.cmdline())
        # spec §3: command line contains both markers
        if 'n3memorycore.n3memory' not in cmdline or '--run-server' not in cmdline:
            return False
        env = p.environ()
        home = env.get('N3MC_HOME')
        if home:
            their_home = home
        else:
            user_home = env.get('USERPROFILE') or env.get('HOME') or os.path.expanduser('~')
            their_home = os.path.join(user_home, '.n3mc')
        return _norm(their_home) == _norm(HOME_DIR)
    except Exception:
        # psutil missing / access denied / process gone → cannot verify
        return False


def _server_pid(cfg: dict, health: Optional[dict]) -> Optional[int]:
    """PID from /health's `pid`, else the process listening on the port."""
    pid = (health or {}).get('pid')
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        return pid
    return _listener_pid(cfg.get('server_port', 18520))


def _listener_pid(port: int) -> Optional[int]:
    try:
        import psutil
        for c in psutil.net_connections(kind='tcp'):
            if c.laddr and c.laddr.port == port and c.status == 'LISTEN' and c.pid:
                return c.pid
    except Exception:
        pass
    return None


def _request_shutdown(cfg: dict) -> bool:
    try:
        r = _post(cfg, '/shutdown', {'root': HOME_DIR}, timeout=5)
        return isinstance(r, dict) and r.get('status') == 'ok'
    except Exception:
        return False


def _wait_port_free(cfg: dict, max_wait: float = 15) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if not _health_check(cfg):
            return True
        time.sleep(0.3)
    return False


def _stop_stale_server(cfg: dict, health: dict) -> bool:
    """Ask /shutdown first; for servers without it (v1.3.x) terminate only a
    process psutil confirms as ours. Never use the PID file's number."""
    if _request_shutdown(cfg):
        return _wait_port_free(cfg)

    pid = _server_pid(cfg, health)
    if pid and _is_own_server_process(pid):
        try:
            import psutil
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        except Exception:
            pass
        # Remove the PID file only if it names the process we just stopped.
        try:
            with open(PID_FILE, 'r', encoding='utf-8') as f:
                same = f.read().strip() == str(pid)
            if same:
                os.remove(PID_FILE)
        except Exception:
            pass
        return _wait_port_free(cfg)

    _notify('stale_server', _STALE_SERVER_MSG)
    return False


_STALE_SERVER_MSG = (
    "Cannot stop the old server. Another N3MC folder may be using the same "
    "`server_port`. Check `server_port` in each folder's `config.json` and "
    "restart manually."
)


def _wait_for_server(cfg: dict, max_wait: int = 60) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _health_decision(_get_health(cfg)) == 'ok':
            return True
        time.sleep(0.5)
    return False


def _server_answered() -> bool:
    """spec §3: once our own server responds, clear the server notices."""
    _clear_notice('stale_server')
    _clear_notice('port_conflict')
    return True


def _start_server(cfg: dict) -> bool:
    # spec §3: atomic exclusive-create of the PID file prevents two CLIs from
    # launching two servers at the same time.
    try:
        pid_fh = open(PID_FILE, 'x', encoding='utf-8')
    except FileExistsError:
        # Another process is starting the server right now — wait for it.
        if _wait_for_server(cfg, max_wait=60):
            return _server_answered()
        print("Warning: N3MC server did not start in time.", file=sys.stderr)
        return False
    try:
        # spec §3 Clean CLI: stderr/stdout → DEVNULL to silence model load warnings
        proc = subprocess.Popen(
            [sys.executable, '-m', 'n3memorycore.n3memory', '--run-server'],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        pid_fh.write(str(proc.pid))
    except Exception:
        pid_fh.close()
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
        raise
    pid_fh.close()
    if not _wait_for_server(cfg, max_wait=60):
        print("Warning: N3MC server did not start in time.", file=sys.stderr)
        return False
    return _server_answered()


_LEGACY_FOREIGN_ROOT = '(unknown: the server did not report its folder)'


def _notify_port_conflict(cfg: dict, root: str) -> None:
    port = cfg.get('server_port', 18520)
    _notify(
        'port_conflict',
        f"Port {port} is used by the N3MC server of another folder ({root}). "
        "This folder saves directly to its own DB and skips memory search "
        f"until `server_port` in {CONFIG_FILE} is changed to a free port.",
    )


def ensure_server(cfg: dict) -> bool:
    """Return True when a server of this folder with the current code answers.

    False with _FOREIGN_SERVER_ROOT set: the port belongs to another folder's
    server (save directly, do not search). False otherwise: start failure.
    """
    global _FOREIGN_SERVER_ROOT
    _FOREIGN_SERVER_ROOT = None
    os.makedirs(MEMORY_DIR, exist_ok=True)

    health = _get_health(cfg)
    decision = _health_decision(health)
    if decision == 'ok':
        return _server_answered()
    if decision == 'foreign':
        # Another folder's server: do not stop it, do not use it.
        _FOREIGN_SERVER_ROOT = health.get('root')
        _notify_port_conflict(cfg, _FOREIGN_SERVER_ROOT)
        return False
    if decision == 'legacy':
        # No `root` (v1.3.x or older): stop it only if psutil confirms it is
        # ours; otherwise treat it as another folder's server.
        if not _is_own_server_process(_server_pid(cfg, health)):
            _FOREIGN_SERVER_ROOT = _LEGACY_FOREIGN_ROOT
            _notify_port_conflict(cfg, _LEGACY_FOREIGN_ROOT)
            return False
        if not _stop_stale_server(cfg, health):
            _FOREIGN_SERVER_ROOT = _LEGACY_FOREIGN_ROOT
            return False
    elif decision == 'stale':
        # Same root, different code_version: stop it and restart.
        if not _stop_stale_server(cfg, health):
            # Could not stop it (notice `stale_server` emitted). It serves our
            # own DB, so keep using it until a human restarts it.
            return True

    # No server answers. spec §3 "PID file handling"
    if os.path.exists(PID_FILE):
        pid = None
        try:
            with open(PID_FILE, 'r', encoding='utf-8') as f:
                pid = int(f.read().strip())
        except Exception:
            pid = None
        if pid and _process_alive(pid) and _is_own_server_process(pid):
            # This folder's server is starting up: keep the PID file and wait.
            if _wait_for_server(cfg, max_wait=60):
                return _server_answered()
            print("Warning: N3MC server did not start in time.", file=sys.stderr)
            return False
        # Dead / reused / unverifiable PID: delete without waiting.
        try:
            os.remove(PID_FILE)
        except Exception:
            pass

    return _start_server(cfg)


# ---------------------------------------------------------------------------
# Direct buffer fallback (no embedding — used when server is down/foreign)
# ---------------------------------------------------------------------------

def _buffer_direct(
    content: str,
    cfg: dict,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> None:
    from .core.database import count_memories
    from uuid_extensions import uuid7

    content = purify_text(content) if content else content
    if not content or not content.strip():
        return

    conn = get_connection(DB_PATH)
    try:
        # spec §3.5: use quick_check in fallback path (lighter than integrity_check)
        result = conn.execute("PRAGMA quick_check").fetchone()
        if result and result[0] != 'ok':
            conn.close()
            _handle_corrupt_db()
            conn = get_connection(DB_PATH)
        init_db(conn)
        migrate_schema(conn)
        # spec §5.4: exact text only (same turn_id when given) — same rule as /buffer
        if check_exact_duplicate(conn, content, turn_id=turn_id):
            return

        before = count_memories(conn)
        ts = datetime.now(timezone.utc).isoformat()
        insert_memory(
            conn, str(uuid7()), content, ts,
            owner_id=cfg['owner_id'],
            local_id=cfg.get('local_id'),
            agent_name=agent_name,
            session_id=session_id or _CLI_SESSION_ID,
            turn_id=turn_id,
        )
        if count_memories(conn) == before:
            raise RuntimeError("record count did not change after INSERT")
    finally:
        conn.close()


def _handle_corrupt_db() -> None:
    corrupt = DB_PATH + '.corrupt.bak'
    # spec §3.5: rename DB and remove WAL sidecar files
    for path in (DB_PATH, DB_PATH + '-wal', DB_PATH + '-shm'):
        if os.path.exists(path):
            try:
                if path == DB_PATH:
                    shutil.move(path, corrupt)
                else:
                    os.remove(path)
            except Exception:
                pass
    print(
        f"Warning: DB corruption detected. Renamed to {corrupt}. "
        "A new empty DB will be created. Recovery: restore a backup of "
        "n3memory.db (together with its config.json) into "
        f"{MEMORY_DIR}, or try `sqlite3 {corrupt} .recover` to salvage records.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

def _exit_server_process() -> None:
    try:
        with open(PID_FILE, 'r') as f:
            if f.read().strip() == str(os.getpid()):
                f.close()
                os.remove(PID_FILE)
    except Exception:
        pass
    os._exit(0)


try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel

    class BufferRequest(BaseModel):
        content: str
        agent_name: Optional[str] = None
        local_id: Optional[str] = None
        session_id: Optional[str] = None
        turn_id: Optional[str] = None

    class SearchRequest(BaseModel):
        query: str
        session_id: Optional[str] = None
        since: Optional[str] = None
        until: Optional[str] = None

    class ShutdownRequest(BaseModel):
        root: str

    # Global server state (populated in lifespan)
    _srv_cfg: dict = {}
    _srv_session: str = ""
    _srv_code_version: str = ""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _srv_cfg, _srv_session, _srv_code_version
        _srv_cfg = _load_config()
        _srv_code_version = _code_version()  # after _load_config may rewrite it
        _srv_session = os.environ.get('N3MC_SESSION_ID') or str(uuid.uuid4())
        os.makedirs(MEMORY_DIR, exist_ok=True)

        from .core.database import get_connection, init_db, migrate_schema

        conn = get_connection(DB_PATH)
        try:
            # spec §3.5: integrity_check on startup (once, not in fallback path)
            ok = conn.execute("PRAGMA integrity_check").fetchone()
            if ok and ok[0] != 'ok':
                conn.close()
                _handle_corrupt_db()
                conn = get_connection(DB_PATH)
            init_db(conn)
            migrate_schema(conn)
        finally:
            conn.close()

        # Preload embedding model (covers first-time download wait)
        try:
            from .core.processor import get_model
            get_model(_srv_cfg.get('embed_model'))
        except Exception as e:
            logger.warning(f"Model preload failed: {e}")

        yield

    app = FastAPI(lifespan=lifespan)

    @app.get('/health')
    async def ep_health():
        return {
            'status':       'ok',
            'root':         os.path.abspath(HOME_DIR),
            'code_version': _srv_code_version or _code_version(),
            'pid':          os.getpid(),
        }

    @app.post('/shutdown')
    async def ep_shutdown(req: ShutdownRequest):
        if _norm(req.root) != _norm(HOME_DIR):
            raise HTTPException(status_code=403, detail='root mismatch')
        threading.Timer(0.3, _exit_server_process).start()
        return {'status': 'ok'}

    @app.post('/buffer')
    async def ep_buffer(req: BufferRequest):
        # Looked up at call time so tests can patch processor.embed_passage.
        from .core.database import (
            get_connection, check_exact_duplicate, insert_memory, count_memories,
        )
        from .core.processor import embed_passage, purify_text
        from uuid_extensions import uuid7

        content = purify_text(req.content) if req.content else ''
        if not content.strip():
            return {'status': 'skipped', 'count': 0, 'reason': 'empty'}

        local = req.local_id   or _srv_cfg.get('local_id')
        sess  = req.session_id or _srv_session
        tid   = req.turn_id
        agent = req.agent_name

        conn = get_connection(DB_PATH)
        try:
            # spec §5.4 (v1.4.0): exact text only (same turn when turn_id is
            # given); checked before computing the embedding. No similarity dedup.
            if check_exact_duplicate(conn, content, turn_id=tid):
                return {'status': 'skipped', 'count': 0, 'reason': 'exact_duplicate'}

            vec = None
            try:
                vec = embed_passage(content)
            except Exception as e:
                logger.warning(f"Embed error: {e}")

            before = count_memories(conn)
            ts = datetime.now(timezone.utc).isoformat()
            insert_memory(
                conn, str(uuid7()), content, ts,
                owner_id=_srv_cfg['owner_id'],
                embedding=vec,
                local_id=local,
                agent_name=agent,
                session_id=sess,
                turn_id=tid,
            )
            if count_memories(conn) == before:
                # spec §5.4: record count unchanged after INSERT → fatal
                return {'status': 'error',
                        'message': 'record count did not change after INSERT'}
            return {'status': 'ok', 'count': 1}
        finally:
            conn.close()

    @app.post('/search')
    async def ep_search(req: SearchRequest):
        from .core.processor import hybrid_search
        sess = req.session_id or _srv_session
        return hybrid_search(
            DB_PATH, req.query or '', _srv_cfg, sess,
            since=req.since, until=req.until,
        )

    @app.post('/repair')
    async def ep_repair():
        from .core.database import (
            get_connection, find_unindexed_memories, serialize_vector,
            strip_fts_punctuation,
        )
        from .core.processor import embed_passage

        conn = get_connection(DB_PATH)
        repaired = 0
        try:
            # spec §3: FTS punct cleaning — one-time migration with marker
            marker = os.path.join(MEMORY_DIR, 'fts_punct_cleaned')
            if not os.path.exists(marker):
                last_rowid = -1
                cleaned_count = 0
                while True:
                    # Batches of 200 without OFFSET: cleaned rows stay in
                    # memories_fts, so page by rowid (keyset) instead.
                    rows = conn.execute(
                        "SELECT rowid, content FROM memories_fts WHERE rowid > ? "
                        "ORDER BY rowid LIMIT 200",
                        (last_rowid,)
                    ).fetchall()
                    if not rows:
                        break
                    last_rowid = rows[-1][0]
                    for row in rows:
                        clean = strip_fts_punctuation(row[1])
                        if clean != row[1]:
                            conn.execute(
                                "UPDATE memories_fts SET content = ? WHERE rowid = ?",
                                (clean, row[0])
                            )
                            cleaned_count += 1
                    conn.commit()
                    if len(rows) < 200:
                        break
                with open(marker, 'w', encoding='utf-8') as f:
                    f.write('done')
                if cleaned_count > 0:
                    print(f"FTS punctuation cleaned: {cleaned_count} record(s).", file=sys.stderr)

            # spec §5: vec_model.txt marker — warn if embed model changed
            current_model = (_srv_cfg or {}).get('embed_model', 'intfloat/multilingual-e5-base')
            vec_marker = os.path.join(MEMORY_DIR, 'vec_model.txt')
            if not os.path.exists(vec_marker):
                with open(vec_marker, 'w', encoding='utf-8') as f:
                    f.write(current_model)
            else:
                with open(vec_marker, 'r', encoding='utf-8') as f:
                    recorded = f.read().strip()
                if recorded and recorded != current_model:
                    print(
                        f"Warning: embed_model in config.json ({current_model}) "
                        f"differs from the model used to build the vector index ({recorded}). "
                        f"Search quality will degrade until vectors are re-embedded. "
                        f"To rebuild: delete '{vec_marker}', then run `n3mc --repair`.",
                        file=sys.stderr,
                    )

            # spec §3.5: batch 200 at a time; OFFSET always 0 (repaired records
            # disappear from WHERE clause automatically)
            while True:
                rows = find_unindexed_memories(conn, limit=200)  # OFFSET=0 always
                if not rows:
                    break
                batch_repaired = 0
                for row in rows:
                    rowid    = row['rowid']
                    content  = row['content']
                    has_vec  = row['vec_rowid'] is not None
                    has_fts  = row['fts_rowid'] is not None

                    if not has_vec:
                        try:
                            vec = embed_passage(content)
                            conn.execute(
                                "INSERT OR IGNORE INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                (rowid, serialize_vector(vec))
                            )
                            batch_repaired += 1
                        except Exception as e:
                            logger.warning(f"Repair embed rowid={rowid}: {e}")

                    if not has_fts:
                        stripped = strip_fts_punctuation(content)
                        conn.execute(
                            "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                            (rowid, stripped)
                        )
                        batch_repaired += 1

                    repaired += 1

                conn.commit()
                # spec §3.5: exit if no progress (e.g. embed model unavailable)
                if batch_repaired == 0:
                    break
                if len(rows) < 200:
                    break

        finally:
            conn.close()

        if repaired > 0:
            print(f"Repaired {repaired} record(s).", file=sys.stderr)
        return {'status': 'ok', 'count': repaired}

    @app.get('/list')
    async def ep_list():
        from .core.database import get_connection, get_all_memories
        conn = get_connection(DB_PATH)
        try:
            rows = get_all_memories(conn)
            records = [
                {
                    'id':         r['id'],
                    'content':    r['content'],
                    'timestamp':  r['timestamp'],
                    'agent_name': r['agent_name'],
                }
                for r in rows
            ]
            return {'records': records, 'total': len(records)}
        finally:
            conn.close()

    # spec §3: Pro-only endpoints — stub returns 403 in Free edition
    @app.delete('/delete/{memory_id}')
    async def ep_delete(memory_id: str):
        raise HTTPException(status_code=403, detail='Pro feature')

    @app.post('/gc')
    async def ep_gc():
        raise HTTPException(status_code=403, detail='Pro feature')

    @app.post('/import')
    async def ep_import():
        raise HTTPException(status_code=403, detail='Pro feature')

except ImportError:
    app = None  # type: ignore


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _turn_id_file(cc_session_id: str = '') -> str:
    """Resolve turn_id file path (session-specific or legacy)."""
    if cc_session_id:
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', cc_session_id)[:64]
        return os.path.join(MEMORY_DIR, f'turn_id_{safe_id}.txt')
    return TURN_ID_FILE


def _read_turn_id(cc_session_id: str = '') -> Optional[str]:
    # spec §2: turn_id_<session>.txt per Claude Code session_id; turn_id.txt
    # only when there is no session_id (never another session's file).
    f = _turn_id_file(cc_session_id)
    if os.path.exists(f):
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                tid = fh.read().strip()
            return tid or None
        except Exception:
            pass
    return None


def _write_turn_id(tid: str, cc_session_id: str = '') -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(_turn_id_file(cc_session_id), 'w', encoding='utf-8') as f:
        f.write(tid)


def _extract_text(prompt) -> str:
    if isinstance(prompt, str):
        try:
            parts = json.loads(prompt)
        except Exception:
            return prompt
        if isinstance(parts, list):
            return ' '.join(
                p.get('text', '') for p in parts
                if isinstance(p, dict) and p.get('type') == 'text'
            )
        return prompt
    if isinstance(prompt, list):
        return ' '.join(
            p.get('text', '') for p in prompt
            if isinstance(p, dict) and p.get('type') == 'text'
        )
    return str(prompt) if prompt else ''


def _normalize_timestamp(date_str: str, end_of_day: bool = False) -> str:
    if not date_str:
        return date_str
    if 'T' in date_str or ' ' in date_str:
        return date_str
    suffix = 'T23:59:59' if end_of_day else 'T00:00:00'
    return date_str + suffix


def _do_buffer(
    text: str, cfg: dict,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> None:
    def _direct():
        try:
            _buffer_direct(text, cfg, agent_name=agent_name,
                           session_id=session_id, turn_id=turn_id)
        except Exception as e2:
            # spec §5.4: fatal failure warning (DB write failed)
            print(f"{_FATAL_SAVE_MSG} ({e2})", file=sys.stderr)

    # spec §3: another folder's server owns the port → never write to its DB
    if _FOREIGN_SERVER_ROOT is not None:
        _direct()
        return

    payload = {
        'content':    text,
        'agent_name': agent_name,
        'session_id': session_id,
        'turn_id':    turn_id,
    }
    try:
        result = _post(cfg, '/buffer', payload)
    except Exception as e:
        print(f"Warning: HTTP buffer failed ({e}), using direct write.", file=sys.stderr)
        _direct()
        return
    if isinstance(result, dict) and result.get('status') == 'error':
        print(f"Warning: /buffer failed ({result.get('message')}), using direct write.",
              file=sys.stderr)
        _direct()


def _do_search(
    query: str, cfg: dict,
    since: Optional[str] = None,
    until: Optional[str] = None,
    current_session: Optional[str] = None,
) -> None:
    """spec §5.4 (v1.4.0): print the result to stdout only (no file), on
    every invocation, including degraded states."""
    if not query or not query.strip():
        _emit(_status_md("_No relevant memories found._"))
        return

    if _FOREIGN_SERVER_ROOT is not None:
        _emit(_status_md(
            "_(memory search unavailable: another N3MC folder's server is "
            f"using port {cfg.get('server_port')})_"
        ))
        return

    payload: dict = {'query': query}
    if since:
        payload['since'] = since
    if until:
        payload['until'] = until
    if current_session:
        payload['session_id'] = current_session

    try:
        result = _post(cfg, '/search', payload)
        if not isinstance(result, dict):
            raise RuntimeError('invalid /search response')
        if result.get('status') == 'error':
            raise RuntimeError(result.get('message') or '/search error')
    except Exception as e:
        _emit(_status_md(f"_(memory search unavailable: {e})_"))
        return

    try:
        md = render_memory_context(result, query, notices=_take_notices())
    except Exception as e:
        md = _status_md(f"_(memory search unavailable: rendering failed: {e})_")
    _emit(md)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_buffer(text: str, cfg: dict, agent_name: Optional[str] = None) -> None:
    ensure_server(cfg)
    _do_buffer(text, cfg, agent_name=agent_name)


def cmd_search(
    query: str, cfg: dict,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> None:
    # A failed start falls through: _do_search then prints
    # "_(memory search unavailable: <reason>)_" (spec §5.4).
    ensure_server(cfg)
    _do_search(query, cfg, since=since, until=until)


def cmd_repair(cfg: dict) -> None:
    if not ensure_server(cfg) and _FOREIGN_SERVER_ROOT is not None:
        print(
            f"N3MC: --repair skipped: port {cfg.get('server_port')} is used by "
            "another N3MC folder's server.",
            file=sys.stderr,
        )
        return
    try:
        result = _post(cfg, '/repair', {})
        count = result.get('count', 0)
        if count > 0:
            print(f"Repaired {count} record(s).", file=sys.stderr)
    except Exception as e:
        print(f"Repair failed: {e}", file=sys.stderr)


def _open_db_readonly_ready():
    from .core.database import get_connection, init_db, migrate_schema
    conn = get_connection(DB_PATH)
    init_db(conn)
    migrate_schema(conn)
    return conn


def cmd_list(
    cfg: dict,
    since: Optional[str] = None,
    until: Optional[str] = None,
    fmt: str = 'tsv',
) -> None:
    from .core.database import get_all_memories
    os.makedirs(MEMORY_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH):
        print("Total: 0 records")
        return

    conn = _open_db_readonly_ready()
    try:
        rows = get_all_memories(conn, since=since, until=until)
        if fmt == 'jsonl':
            for r in rows:
                print(json.dumps({
                    'id':         r['id'],
                    'timestamp':  r['timestamp'],
                    'agent_name': r['agent_name'],
                    'content':    r['content'],
                }, ensure_ascii=False))
        else:
            for r in rows:
                # spec §6 test 2: [UUIDv7]\t[timestamp]\t[agent_name]\t[content[:80]]
                content_80 = r['content'][:80].replace('\n', ' ').replace('\r', ' ')
                agent = r['agent_name'] or '-'
                print(f"{r['id']}\t{r['timestamp']}\t{agent}\t{content_80}")
        print(f"Total: {len(rows)} records")
    finally:
        conn.close()


def cmd_recall_thread(
    turn_id: str,
    cfg: dict,
    before: int = 2,
    after: int = 2,
    fmt: str = 'tsv',
) -> None:
    """spec §3.6: return turn_id's records plus N surrounding turns."""
    from .core.database import get_thread_context
    os.makedirs(MEMORY_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH):
        print(f"No DB found at {DB_PATH}", file=sys.stderr)
        return

    conn = _open_db_readonly_ready()
    try:
        rows = get_thread_context(conn, turn_id, before=before, after=after)
        if fmt == 'jsonl':
            for r in rows:
                print(json.dumps({
                    'id':         r['id'],
                    'timestamp':  r['timestamp'],
                    'agent_name': r['agent_name'],
                    'turn_id':    r['turn_id'],
                    'content':    r['content'],
                }, ensure_ascii=False))
        else:
            for r in rows:
                content_80 = r['content'][:80].replace('\n', ' ').replace('\r', ' ')
                agent = r['agent_name'] or '-'
                print(f"{r['id']}\t{r['timestamp']}\t{agent}\t{content_80}")
        print(f"Total: {len(rows)} records")
    finally:
        conn.close()


def cmd_show_turn(turn_id: str, cfg: dict) -> None:
    """spec §4.5: full text of one exchange (no truncation)."""
    from .core.database import get_memories_by_turn_id
    from .core.processor import _fmt_saved
    if not os.path.exists(DB_PATH):
        print("Total: 0 records")
        return
    conn = _open_db_readonly_ready()
    try:
        rows = get_memories_by_turn_id(conn, turn_id)
        for r in rows:
            print(f"--- {r['id']} saved={_fmt_saved(r['timestamp'])} ---")
            print(r['content'])
        print(f"Total: {len(rows)} records")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# --stop: behavior rules + memory_context retirement (spec §4)
# ---------------------------------------------------------------------------

_BEHAVIOR_MD_MARKER = '<!-- n3mc-behavior v2 -->'

_BEHAVIOR_MD_CONTENT = _BEHAVIOR_MD_MARKER + """
# N3MemoryCore Behavioral Guidelines

These guidelines apply to every Claude Code session in this project. They are
loaded automatically from `.claude/rules/` at session start.

## Fully Automatic Saving
All conversations are saved by hooks (UserPromptSubmit + Stop). Do NOT call
`--buffer` manually. Every non-empty user message and Claude response is
recorded character-for-character; there is no length filter and no skip-pattern
filter. Make NO acknowledgement when a save succeeds — silence is correct.

## Recalled Memory Context
Every turn, the UserPromptSubmit hook adds a `# Recalled Memory Context` block
to your context. Read `## Top matches` first; use
`## Previous matching Q-A exchanges` as supporting context. When several
versions of the same content appear, the one with the newest `saved` time is
the current one. If the block contains a `> ⚠️ N3MC notice:` line, tell the
user what it says.

## Active RAG
When prior context would help, run a search proactively. **Always invoke via
`python -m n3memorycore.n3memory --search "<keywords>"`**, NOT the bare `n3mc`
console script. Claude Code spawns a bash subprocess whose `PATH` does not
reliably include the Python `Scripts/` directory where the `n3mc` entry-point
lives, so `n3mc --search ...` works in interactive shells but fails in
production with `command not found`. `python -m` resolves through the Python
interpreter (which is on `PATH`) and is portable across venvs and platforms.
Both invocation forms are auto-allowed via `permissions.allow`.

## Cut Exchanges
A Q-A exchange may end with a line like
`_(N more chunk(s) of this exchange omitted. Full text: ...)_`. If the omitted
part is needed for your answer, run the command shown there
(`python -m n3memorycore.n3memory --show-turn <turn_id>`) and read the full
text before answering.

## Recall Acknowledgment
When recalled memory actually shapes your reply (you are recalling something
saved earlier), open the reply with a short acknowledgment in the user's
language — e.g. "Pulling this from earlier memory in this session." or
「前回の回答がメモリに保存されています。」 If no relevant memory was found,
or it did not influence your answer, do NOT announce anything.

## Fatal-Failure Warning
If a save fails physically, surface the warning prominently:
> ⚠️ Physical save failed. Current memories may be lost.
"""

# Templates shipped before the current one (sha256 of LF-normalized text,
# first 16 hex). Add the previous template's hash whenever the template changes.
_KNOWN_BEHAVIOR_MD_HASHES = frozenset({
    'ade0a59b94f59f9a',  # v1.2.x – v1.3.x
    '34ed39efe5a3833d',  # v1.2.0 scaffolding
})


def _behavior_md_hash(text: str) -> str:
    t = text.replace('\r\n', '\n').replace('\r', '\n')
    return hashlib.sha256(t.encode('utf-8')).hexdigest()[:16]


def _backup(path: str) -> str:
    dst = f"{path}.bak-n3mc-{datetime.now():%Y%m%d%H%M%S}"
    shutil.copy2(path, dst)
    return dst


def _ensure_behavior_md(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        return
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    first_line = text.lstrip('﻿').splitlines()[0].strip() if text else ''
    if first_line == _BEHAVIOR_MD_MARKER:
        return
    if _behavior_md_hash(text) in _KNOWN_BEHAVIOR_MD_HASHES:
        _backup(path)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        _clear_notice('behavior_md_edited')
        return
    _notify(
        'behavior_md_edited',
        "The rules file has been edited by hand, so it was not updated. "
        "Merge manually if needed.",
    )


_MEMORY_CONTEXT_IMPORT_RE = re.compile(r'^[ \t]*@[^\n]*memory_context\.md[ \t]*$')
_N3MC_COMMENT_PREFIXES = (
    '# N3MemoryCore — ',
    '# N3MemoryCore - ',
    '# Path resolves to ',
    '# If you have not yet run ',
)


def _retire_memory_context(claude_md: str) -> None:
    """spec §4 (v1.4.0): remove the legacy @…memory_context.md import from
    <cwd>/.claude/CLAUDE.md (backup first, keep LF/CRLF) and delete the
    leftover global memory_context.md once."""
    if os.path.exists(claude_md):
        try:
            with open(claude_md, 'rb') as f:
                text = f.read().decode('utf-8')
        except Exception:
            text = None
        if text is not None:
            # Lines keep their own endings (LF / CRLF), so the file is written
            # back with its original line endings.
            lines = text.splitlines(keepends=True)
            drop = set()
            for i, line in enumerate(lines):
                if not _MEMORY_CONTEXT_IMPORT_RE.match(line.rstrip('\r\n')):
                    continue
                drop.add(i)
                # The N3MC comment lines just before it (blank lines between
                # the comment block and the @import go with them).
                j = i - 1
                first_comment = None
                while j >= 0 and j not in drop:
                    body = lines[j].rstrip('\r\n')
                    if body.startswith(_N3MC_COMMENT_PREFIXES):
                        first_comment = j
                    elif body.strip():
                        break
                    j -= 1
                if first_comment is not None:
                    drop.update(range(first_comment, i))
            if drop:
                new_text = ''.join(l for k, l in enumerate(lines) if k not in drop)
                _backup(claude_md)
                with open(claude_md, 'wb') as f:
                    f.write(new_text.encode('utf-8'))

    # Leftover global memory_context.md: delete once, then mark as retired.
    if not os.path.exists(CONTEXT_RETIRED_MARKER) and os.path.exists(CONTEXT_FILE):
        try:
            os.remove(CONTEXT_FILE)
            os.makedirs(os.path.dirname(CONTEXT_RETIRED_MARKER), exist_ok=True)
            with open(CONTEXT_RETIRED_MARKER, 'w', encoding='utf-8') as f:
                f.write(datetime.now(timezone.utc).isoformat())
        except Exception as e:
            print(f"Warning: could not retire memory_context.md: {e}", file=sys.stderr)


def cmd_stop(cfg: dict) -> None:
    """spec §4: behavior rules (versioned) + memory_context retirement.
    Writes nothing into CLAUDE.md. Does NOT stop the server."""
    cp = claude_paths()
    _ensure_behavior_md(cp['BEHAVIOR_MD'])
    _retire_memory_context(cp['CLAUDE_MD'])


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def cmd_hook_submit(cfg: dict) -> None:
    """spec §4.5: UserPromptSubmit hook — repair → buffer(claude) → search → buffer(user)."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    user_text     = _extract_text(data.get('message') or data.get('prompt') or '')
    last_claude   = data.get('last_assistant_message') or ''
    cc_session_id = data.get('session_id', '')

    # spec §4.5: system inputs (e.g. <task-notification>) — no search, no save,
    # no new turn_id; the reply to it belongs to the current exchange.
    if user_text.lstrip().startswith(_SYSTEM_PROMPT_PREFIXES):
        _append_audit({'hook': 'UserPromptSubmit', 'stage': 'skipped',
                       'reason': 'system_prompt'})
        _emit(_status_md("_No relevant memories found._"))
        return

    server_ok = ensure_server(cfg)

    # Step 1: Repair unindexed records (own server only)
    if server_ok:
        try:
            _post(cfg, '/repair', {})
        except Exception as e:
            logger.warning(f"Repair failed during hook: {e}")

    # Step 2: Save Claude's previous response
    if last_claude.strip():
        from .core.processor import chunk_text, add_chunk_prefixes, purify_text
        cleaned  = purify_text(last_claude)
        chunks   = chunk_text(cleaned)
        prev_tid = _read_turn_id(cc_session_id)
        for chunk in add_chunk_prefixes(chunks, 'claude'):
            _do_buffer(chunk, cfg, agent_name='claude-code',
                       session_id=cc_session_id or None, turn_id=prev_tid)

    # Step 3: Search memory for current query
    q = user_text[:cfg.get('search_query_max_chars', 2000)]
    _do_search(q, cfg, current_session=cc_session_id or None)

    # Step 4: Save user message (spec §5: no length filter, no skip patterns)
    if user_text.strip():
        from .core.processor import chunk_text, add_chunk_prefixes
        new_tid = str(uuid.uuid4())
        _write_turn_id(new_tid, cc_session_id)
        chunks = chunk_text(user_text)
        for chunk in add_chunk_prefixes(chunks, 'user'):
            _do_buffer(chunk, cfg, agent_name='claude-code',
                       session_id=cc_session_id or None, turn_id=new_tid)


def cmd_save_claude_turn(cfg: dict) -> None:
    """spec §4.5: Stop hook helper — save last_assistant_message via chunking.
    The turn_id file is NOT cleared (the next UserPromptSubmit overwrites it);
    a second Stop in the same turn is stopped by the same-turn exact check."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    last_claude   = data.get('last_assistant_message') or ''
    cc_session_id = data.get('session_id', '')

    if not last_claude.strip():
        return

    from .core.processor import chunk_text, add_chunk_prefixes, purify_text
    cleaned = purify_text(last_claude)
    chunks  = chunk_text(cleaned)

    tid = _read_turn_id(cc_session_id)
    if not tid:
        tid = str(uuid.uuid4())
        _write_turn_id(tid, cc_session_id)
    ensure_server(cfg)
    for chunk in add_chunk_prefixes(chunks, 'claude'):
        _do_buffer(chunk, cfg, agent_name='claude-code',
                   session_id=cc_session_id or None, turn_id=tid)


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

def run_server(cfg: dict) -> None:
    import uvicorn
    host = cfg.get('bind_host') or '127.0.0.1'
    if host != '127.0.0.1':
        print(
            f"WARNING: bind_host is {host}. The N3MC API (search, list, "
            "/shutdown, ...) has no authentication and becomes reachable from "
            "the network. Keep the default 127.0.0.1 unless you know what you "
            "are doing.",
            file=sys.stderr,
        )
    uvicorn.run(
        app, host=host, port=cfg.get('server_port', 18520),
        log_level='error',
    )


# ---------------------------------------------------------------------------
# n3mc --init  —  register hooks in ~/.claude/settings.json
# ---------------------------------------------------------------------------

def _resolve_hook_command(script_name: str) -> str:
    """Resolve entry-point script path with forward slashes (bash-safe)."""
    import pathlib
    exe = shutil.which(script_name)
    if exe:
        return pathlib.Path(exe).as_posix()
    module = {
        'n3mc-hook':      'n3memorycore.n3mc_hook',
        'n3mc-stop-hook': 'n3memorycore.n3mc_stop_hook',
    }[script_name]
    py = pathlib.Path(sys.executable).as_posix()
    return f'"{py}" -m {module}'


def cmd_init() -> None:
    """Set up ~/.n3mc/ and register hooks in ~/.claude/settings.json."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    print(f"Data directory: {HOME_DIR}")

    cfg = _load_config()
    print(f"Config: {CONFIG_FILE}")
    print(f"  owner_id = {cfg['owner_id']}")
    print(f"  port     = {cfg['server_port']}")

    settings_path = os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)

    if os.path.exists(settings_path):
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
    else:
        settings = {}

    hooks = settings.setdefault('hooks', {})
    hook_cmd = _resolve_hook_command('n3mc-hook')
    stop_cmd = _resolve_hook_command('n3mc-stop-hook')

    def _replace_hook(event: str, markers: tuple, command: str) -> None:
        # spec §4: markers cover both hyphen (exe) and underscore (module) forms
        existing = hooks.get(event, [])
        new_entries = []
        for entry in existing:
            inner = entry.get('hooks', [])
            keep = [
                h for h in inner
                if not any(m in h.get('command', '') for m in markers)
            ]
            if keep:
                new_entry = dict(entry)
                new_entry['hooks'] = keep
                new_entries.append(new_entry)
        new_entries.append({
            'hooks': [{'type': 'command', 'command': command}],
        })
        hooks[event] = new_entries

    _replace_hook('UserPromptSubmit', ('n3mc-hook', 'n3mc_hook'),           hook_cmd)
    _replace_hook('Stop',             ('n3mc-stop-hook', 'n3mc_stop_hook'), stop_cmd)

    with open(settings_path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

    print(f"Hooks registered in: {settings_path}")
    print(f"  UserPromptSubmit -> {hook_cmd}")
    print(f"  Stop             -> {stop_cmd}")
    print("Done. Restart Claude Code: saving will start from the next session.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description='N3MemoryCore CLI')
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument('--init',   action='store_true',
                     help='Set up ~/.n3mc/ and register Claude Code hooks')
    grp.add_argument('--buffer', metavar='TEXT', nargs='?', const='-')
    grp.add_argument('--search', metavar='QUERY')
    grp.add_argument('--repair', action='store_true')
    grp.add_argument('--list',   action='store_true')
    grp.add_argument('--recall-thread', metavar='TURN_ID', dest='recall_thread')
    grp.add_argument('--show-turn', metavar='TURN_ID', dest='show_turn',
                     help='Print the full text of one exchange')
    grp.add_argument('--stop',   action='store_true')
    grp.add_argument('--hook-submit',      action='store_true', dest='hook_submit')
    grp.add_argument('--save-claude-turn', action='store_true', dest='save_claude_turn')
    grp.add_argument('--run-server',       action='store_true', dest='run_server')

    parser.add_argument('--agent-id', dest='agent_id', default=None)
    parser.add_argument('--since', default=None, help='Filter from date (YYYY-MM-DD or ISO)')
    parser.add_argument('--until', default=None, help='Filter until date (YYYY-MM-DD or ISO)')
    parser.add_argument('--before', type=int, default=2, help='Turns before target (default 2)')
    parser.add_argument('--after',  type=int, default=2, help='Turns after target (default 2)')
    parser.add_argument('--format', dest='fmt', default='tsv',
                        choices=['tsv', 'jsonl'], help='Output format for --list/--recall-thread')

    args = parser.parse_args()

    if args.init:
        cmd_init()
        return

    cfg = _load_config()

    since = _normalize_timestamp(args.since) if args.since else None
    until = _normalize_timestamp(args.until, end_of_day=True) if args.until else None

    if args.run_server:
        run_server(cfg)
    elif args.buffer is not None:
        text = sys.stdin.read() if args.buffer == '-' else args.buffer
        if text.strip():
            cmd_buffer(text, cfg, agent_name=args.agent_id)
    elif args.search is not None:
        cmd_search(args.search, cfg, since=since, until=until)
    elif args.repair:
        cmd_repair(cfg)
    elif args.list:
        cmd_list(cfg, since=since, until=until, fmt=args.fmt)
    elif args.recall_thread:
        cmd_recall_thread(
            args.recall_thread, cfg,
            before=args.before, after=args.after, fmt=args.fmt,
        )
    elif args.show_turn:
        cmd_show_turn(args.show_turn, cfg)
    elif args.stop:
        cmd_stop(cfg)
    elif args.hook_submit:
        cmd_hook_submit(cfg)
    elif args.save_claude_turn:
        cmd_save_claude_turn(cfg)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
