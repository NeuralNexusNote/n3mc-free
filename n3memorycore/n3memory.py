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
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from .paths import HOME_DIR, MEMORY_DIR, DB_PATH, PID_FILE, AUDIT_LOG, CONFIG_FILE, NOTICES_FILE, claude_paths
from .paths import TURN_ID_FILE, CONTEXT_FILE, CONTEXT_RETIRED_MARKER
from .paths import SERVER_LOG, SERVER_START_FAILED_MARKER
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


def _atomic_write_text(path: str, text: str) -> None:
    """spec §3.5 (v1.5.0): write a temporary file in the same folder, then
    os.replace it over the target — an interrupted write never leaves a
    truncated file."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.' + os.path.basename(path) + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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
        # spec §3 "Notices" (v1.5.0): atomic, like config.json
        _atomic_write_text(NOTICES_FILE, json.dumps(notices, ensure_ascii=False, indent=2))
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


# spec §3.5 "Allowed values" (v1.5.0): (kind, min, max, min_exclusive)
# kind 'int' = JSON integer only (2000.0 is rejected); 'num' = int or float.
_CONFIG_RULES: dict = {
    'server_port':              ('int', 1, 65535, False),
    'search_result_limit':      ('int', 1, None, False),
    'pair_top_n':               ('int', 1, None, False),
    'pair_max_chars':           ('int', 1, None, False),
    'search_query_max_chars':   ('int', 1, None, False),
    'version_min_chars':        ('int', 1, None, False),
    'half_life_days':           ('num', 0, None, True),
    'bm25_min_threshold':       ('num', 0, None, False),
    'min_score':                ('num', 0, 1, False),
    'vector_min_cos':           ('num', 0, 1, False),
    'relative_min_score_ratio': ('num', 0, 1, False),
    'version_sim_threshold':    ('num', 0, 1, False),
}


def _config_value_ok(key: str, value) -> bool:
    """spec §3.5 (v1.5.0): type and range check of one config.json value."""
    if key not in _DEFAULT_CONFIG:
        return True
    default = _DEFAULT_CONFIG[key]
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, (int, float)):
        if isinstance(value, bool):
            return False                      # booleans are never numbers
        rule = _CONFIG_RULES.get(key)
        if rule is None:
            return isinstance(value, (int, float))
        kind, lo, hi, lo_exclusive = rule
        if kind == 'int':
            if not isinstance(value, int):
                return False
        elif not isinstance(value, (int, float)):
            return False
        if value != value:                    # NaN
            return False
        if lo is not None and (value <= lo if lo_exclusive else value < lo):
            return False
        if hi is not None and value > hi:
            return False
        return True
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


def _read_config_file():
    """Parse config.json. A file that another process has just created may
    still be empty for a moment (exclusive creation, then write), so a
    very young file that does not parse is read again briefly."""
    for attempt in range(20):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("the top level is not a JSON object")
            return data
        except Exception:
            try:
                young = time.time() - os.path.getmtime(CONFIG_FILE) < 2.0
            except OSError:
                young = False
            if not young or attempt == 19:
                raise
            time.sleep(0.05)


def _create_config(cfg: dict) -> bool:
    """spec §3.5 (v1.5.0): create a missing config.json exclusively.
    False when another process created it first (read that file instead)."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    try:
        f = open(CONFIG_FILE, 'x', encoding='utf-8')
    except FileExistsError:
        return False
    with f:
        f.write(json.dumps(cfg, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    return True


def _load_config() -> dict:
    """spec §3.5: never overwrite a config.json the user mistyped; create a
    missing one exclusively; update an existing one atomically."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    cfg = dict(_DEFAULT_CONFIG)

    if not os.path.exists(CONFIG_FILE):
        _fill_ids(cfg)
        if _create_config(cfg):
            return cfg
        cfg = dict(_DEFAULT_CONFIG)   # another process won: use its file

    try:
        data = _read_config_file()
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
            "config.json: these items have the wrong type or are out of range "
            f"and are ignored (defaults are used): {', '.join(sorted(bad))}. "
            "Write numbers without quotes, e.g. \"vector_min_cos\": 0.83, and "
            "counts as whole numbers without a decimal point, e.g. 2000.",
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
    """spec §3.5 (v1.5.0): temporary file in the same folder + os.replace."""
    _atomic_write_text(CONFIG_FILE, json.dumps(cfg, indent=2, ensure_ascii=False))


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
        headers={'Content-Type': 'application/json',
                 'Host': f"127.0.0.1:{cfg['server_port']}"},   # spec §3 Host check
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _get(cfg: dict, path: str, timeout: int = 30) -> dict:
    import urllib.request
    url = f"http://127.0.0.1:{cfg['server_port']}{path}"
    req = urllib.request.Request(
        url, headers={'Host': f"127.0.0.1:{cfg['server_port']}"}, method='GET')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def _wait_for_server(cfg: dict, max_wait: int = 60, proc=None) -> bool:
    """Poll /health every 0.5 s. With `proc`, stop at once when the child
    process exits (spec §3 "Starting the server", step 2)."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _health_decision(_get_health(cfg)) == 'ok':
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


# Why this command runs without the server (None = it may use the server).
# _do_buffer then saves directly and _do_search prints the reason.
_NO_SERVER_REASON: Optional[str] = None

_STILL_STARTING = 'the memory server is still starting'
_START_FAILED = 'the memory server could not start'

# spec §3 (v1.5.0)
_START_FAILED_BACKOFF_SECONDS = 300
_SERVER_LOG_MAX_BYTES = 1024 * 1024
_PID_FILE_GRACE_SECONDS = 60


def _server_answered() -> bool:
    """spec §3: once our own server responds, clear the server notices and
    the start-failure marker."""
    global _NO_SERVER_REASON
    _NO_SERVER_REASON = None
    _clear_notice('stale_server')
    _clear_notice('port_conflict')
    _clear_notice('server_start_failed')
    try:
        os.remove(SERVER_START_FAILED_MARKER)
    except OSError:
        pass
    return True


def _start_failed_recently() -> bool:
    """spec §3 step 4: a start failed less than 5 minutes ago."""
    if not os.path.exists(SERVER_START_FAILED_MARKER):
        return False
    try:
        with open(SERVER_START_FAILED_MARKER, 'r', encoding='utf-8') as f:
            t = float(f.read().strip())
    except Exception:
        try:
            t = os.path.getmtime(SERVER_START_FAILED_MARKER)
        except OSError:
            return False
    return time.time() - t < _START_FAILED_BACKOFF_SECONDS


def _open_server_log():
    """spec §3 step 1: stderr of the server -> .memory/server.log (append),
    truncated first when it is larger than 1 MB."""
    os.makedirs(os.path.dirname(SERVER_LOG), exist_ok=True)
    try:
        if os.path.getsize(SERVER_LOG) > _SERVER_LOG_MAX_BYTES:
            open(SERVER_LOG, 'wb').close()
    except OSError:
        pass
    return open(SERVER_LOG, 'ab')


def _detach_kwargs() -> dict:
    """spec §3 Clean CLI (v1.5.0): keep the server alive after the hook exits."""
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP
                                 | subprocess.CREATE_NO_WINDOW}
    return {'start_new_session': True}


def _spawn_server():
    log = _open_server_log()
    try:
        return subprocess.Popen(
            [sys.executable, '-m', 'n3memorycore.n3memory', '--run-server'],
            stderr=log, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            **_detach_kwargs(),
        )
    finally:
        log.close()   # the child keeps its own handle


def _record_start_failure(code) -> None:
    _notify(
        'server_start_failed',
        f"The N3MC memory server could not start (exit code {code}). "
        f"See {SERVER_LOG} for the reason.",
    )
    try:
        _atomic_write_text(SERVER_START_FAILED_MARKER, str(time.time()))
    except Exception as e:
        print(f"Warning: could not write {SERVER_START_FAILED_MARKER}: {e}",
              file=sys.stderr)


def _start_server(cfg: dict) -> bool:
    """spec §3 "Starting the server (_start_server, v1.5.0)"."""
    global _NO_SERVER_REASON
    if _start_failed_recently():
        _NO_SERVER_REASON = _START_FAILED + f" (see {SERVER_LOG})"
        return False
    # spec §3: atomic exclusive-create of the PID file prevents two CLIs from
    # launching two servers at the same time.
    try:
        pid_fh = open(PID_FILE, 'x', encoding='utf-8')
    except FileExistsError:
        # Another process is starting the server right now - wait for it.
        if _wait_for_server(cfg, max_wait=60):
            return _server_answered()
        _NO_SERVER_REASON = _STILL_STARTING
        return False
    try:
        proc = _spawn_server()
        pid_fh.write(str(proc.pid))
    except Exception as e:
        pid_fh.close()
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
        # Never let a failed launch stop the hook: run without the server.
        print(f"Warning: could not launch the N3MC server: {e}", file=sys.stderr)
        _NO_SERVER_REASON = _START_FAILED + f" ({e})"
        return False
    pid_fh.close()
    if _wait_for_server(cfg, max_wait=60, proc=proc):
        return _server_answered()
    code = proc.poll()
    if code is not None:
        # Exited during startup: port held by another program, missing
        # dependency, DB cannot be opened, ...
        _record_start_failure(code)
        try:
            with open(PID_FILE, 'r', encoding='utf-8') as f:
                same = f.read().strip() == str(proc.pid)
            if same:
                os.remove(PID_FILE)
        except Exception:
            pass
        _NO_SERVER_REASON = _START_FAILED + f" (see {SERVER_LOG})"
        return False
    # Still alive after 60 s: the first start downloads the model. Leave it.
    _NO_SERVER_REASON = _STILL_STARTING
    return False


_LEGACY_FOREIGN_ROOT = '(unknown: the server did not report its folder)'


def _notify_port_conflict(cfg: dict, root: str) -> None:
    port = cfg.get('server_port', 18520)
    _notify(
        'port_conflict',
        f"Port {port} is used by the N3MC server of another folder ({root}). "
        "This folder saves directly to its own DB and skips memory search "
        f"until `server_port` in {CONFIG_FILE} is changed to a free port.",
    )


def _psutil_available() -> bool:
    try:
        import psutil  # noqa: F401
        return True
    except ImportError:
        return False


def _pid_file_process_starting(pid: Optional[int]) -> bool:
    """spec §3 "PID file handling": is the PID-file process this folder's
    server that is still starting? Without psutil (broken install), every
    PID-file process counts as starting for 60 s after the file's mtime."""
    if not _psutil_available():
        try:
            age = time.time() - os.path.getmtime(PID_FILE)
        except OSError:
            return False
        return age < _PID_FILE_GRACE_SECONDS
    return bool(pid) and _process_alive(pid) and _is_own_server_process(pid)


def ensure_server(cfg: dict) -> bool:
    """Return True when a server of this folder with the current code answers.

    False with _FOREIGN_SERVER_ROOT set: the port belongs to another folder's
    server (save directly, do not search). False with _NO_SERVER_REASON set:
    no server for this command (still starting / failed to start).
    """
    global _FOREIGN_SERVER_ROOT, _NO_SERVER_REASON
    _FOREIGN_SERVER_ROOT = None
    _NO_SERVER_REASON = None
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
        if _pid_file_process_starting(pid):
            # This folder's server is starting up: keep the PID file and wait.
            if _wait_for_server(cfg, max_wait=60):
                return _server_answered()
            # Still starting after 60 s: no second server, keep the PID file.
            _NO_SERVER_REASON = _STILL_STARTING
            return False
        # Dead / reused PID: delete without waiting.
        try:
            os.remove(PID_FILE)
        except Exception:
            pass

    return _start_server(cfg)


# ---------------------------------------------------------------------------
# Direct buffer fallback (no embedding — used when server is down/foreign)
# ---------------------------------------------------------------------------

def _quick_check_once(conn):
    """spec §3.5 (v1.5.0): PRAGMA quick_check at most once per process.
    Returns a fresh connection when the DB was set aside as corrupt."""
    global _QUICK_CHECK_DONE
    if _QUICK_CHECK_DONE:
        return conn
    _QUICK_CHECK_DONE = True
    result = conn.execute("PRAGMA quick_check").fetchone()
    if result and result[0] != 'ok':
        conn.close()
        _handle_corrupt_db()
        conn = get_connection(DB_PATH)
    return conn


# Set once quick_check has run in this process (spec §3.5, v1.5.0).
_QUICK_CHECK_DONE = False


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
        # spec §3.5: quick_check in the fallback path (lighter than
        # integrity_check), once per process
        conn = _quick_check_once(conn)
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


def _corrupt_backup_path() -> str:
    """n3memory.db.corrupt-<YYYYmmddHHMMSS>.bak (local time). A second
    corruption within the same second gets a -<n> suffix before .bak."""
    stamp = datetime.now().strftime('%Y%m%d%H%M%S')
    path = f"{DB_PATH}.corrupt-{stamp}.bak"
    n = 1
    while any(os.path.exists(path + sfx) for sfx in ('', '-wal', '-shm')):
        n += 1
        path = f"{DB_PATH}.corrupt-{stamp}-{n}.bak"
    return path


def _handle_corrupt_db() -> str:
    """spec §3.5 (v1.5.0): set the DB aside under a timestamped name and
    MOVE its -wal / -shm next to it (never delete them), then notify."""
    corrupt = _corrupt_backup_path()
    for src, dst in ((DB_PATH, corrupt),
                     (DB_PATH + '-wal', corrupt + '-wal'),
                     (DB_PATH + '-shm', corrupt + '-shm')):
        if os.path.exists(src):
            try:
                shutil.move(src, dst)
            except Exception as e:
                print(f"Warning: could not move {src} to {dst}: {e}", file=sys.stderr)
    print(
        f"Warning: DB corruption detected. Set aside as {corrupt} (with its "
        "-wal / -shm). A new empty DB will be created. Recovery: rename the "
        "sidecars back to <name>-wal / <name>-shm if needed, then "
        f"`sqlite3 {corrupt} .recover | sqlite3 recovered.db`.",
        file=sys.stderr,
    )
    _notify(
        'db_corrupt',
        f"The memory DB failed its integrity check and was set aside as {corrupt}. "
        "A new empty DB is in use. To recover records: "
        f"sqlite3 {corrupt} .recover | sqlite3 recovered.db",
    )
    return corrupt


# ---------------------------------------------------------------------------
# Time bounds for --since / --until (spec §3.6, v1.5.0)
# ---------------------------------------------------------------------------

# None = the machine's local time zone. Tests set a fixed tzinfo.
_LOCAL_TZ = None

_DATE_ONLY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_DATETIME_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)'
    r'(Z|z|[+-]\d{2}:?\d{2})?$'
)


class TimeBoundError(ValueError):
    pass


def _local_aware(dt: datetime) -> datetime:
    if _LOCAL_TZ is not None:
        return dt.replace(tzinfo=_LOCAL_TZ)
    return dt.astimezone()     # naive → the machine's local time (DST aware)


def _parse_time_bound(value: Optional[str], is_until: bool = False,
                      name: Optional[str] = None) -> Optional[str]:
    """spec §3.6 (v1.5.0): date or ISO datetime (T or space, optional
    offset/Z) → UTC string formatted like the stored timestamps.
    Offset-less values are local time; a date-only until is 23:59:59.999999.
    Raises TimeBoundError on unreadable input."""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    label = name or ('--until' if is_until else '--since')
    try:
        if _DATE_ONLY_RE.match(v):
            d = datetime.strptime(v, '%Y-%m-%d')
            if is_until:
                d = d.replace(hour=23, minute=59, second=59, microsecond=999999)
            dt = _local_aware(d)
        else:
            m = _DATETIME_RE.match(v)
            if not m:
                raise ValueError(v)
            date_part, time_part, off = m.groups()
            text = f"{date_part}T{time_part}"
            if off:
                if off in ('Z', 'z'):
                    off = '+00:00'
                elif ':' not in off:
                    off = off[:3] + ':' + off[3:]
                text += off
            dt = datetime.fromisoformat(text)
            dt = _local_aware(dt) if dt.tzinfo is None else dt
    except Exception:
        raise TimeBoundError(
            f"{label}: cannot read '{value}' (use 2026-11-13 or 2026-11-13T09:00)"
        ) from None
    return dt.astimezone(timezone.utc).isoformat()


def _parse_time_range(since: Optional[str], until: Optional[str]):
    """Both bounds; TimeBoundError when unreadable or since > until."""
    s = _parse_time_bound(since, is_until=False)
    u = _parse_time_bound(until, is_until=True)
    if s and u and datetime.fromisoformat(s) > datetime.fromisoformat(u):
        raise TimeBoundError('--since is later than --until')
    return s, u


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


def _check_host(host: Optional[str]) -> bool:
    """spec §3 (v1.5.0): only Host 127.0.0.1:<port> or localhost:<port>."""
    port = (_srv_cfg or {}).get('server_port') or _DEFAULT_CONFIG['server_port']
    h = (host or '').strip().lower()
    return h in (f'127.0.0.1:{port}', f'localhost:{port}')


# Looked up at request time so tests can replace the check (TestClient sends
# Host: testserver).
_host_ok = _check_host

# Global server state (populated in lifespan)
_srv_cfg: dict = {}
_srv_session: str = ""
_srv_code_version: str = ""

# spec §3.5 (v1.5.0): one /repair batch per --hook-submit turn
_HOOK_REPAIR_MAX_RECORDS = 200


def _repair_db(max_records: Optional[int] = None) -> int:
    """Body of /repair. max_records limits the records changed in this call
    (FTS punctuation cleaning and index repair); None = everything."""
    from .core.database import (
        get_connection, find_unindexed_memories, serialize_vector,
        strip_fts_punctuation,
    )
    from .core.processor import embed_passage

    budget = max_records if (isinstance(max_records, int) and max_records > 0) else None

    def left() -> Optional[int]:
        return None if budget is None else budget - repaired

    conn = get_connection(DB_PATH)
    repaired = 0
    try:
        # spec §3: FTS punct cleaning — one-time migration with marker
        marker = os.path.join(MEMORY_DIR, 'fts_punct_cleaned')
        if not os.path.exists(marker):
            last_rowid = -1
            cleaned_count = 0
            finished = False
            while True:
                if left() is not None and left() <= 0:
                    break
                # Batches of 200 without OFFSET: cleaned rows stay in
                # memories_fts, so page by rowid (keyset) instead.
                rows = conn.execute(
                    "SELECT rowid, content FROM memories_fts WHERE rowid > ? "
                    "ORDER BY rowid LIMIT 200",
                    (last_rowid,)
                ).fetchall()
                if not rows:
                    finished = True
                    break
                stopped = False
                for row in rows:
                    if left() is not None and left() <= 0:
                        stopped = True
                        break
                    last_rowid = row[0]
                    clean = strip_fts_punctuation(row[1])
                    if clean != row[1]:
                        conn.execute(
                            "UPDATE memories_fts SET content = ? WHERE rowid = ?",
                            (clean, row[0])
                        )
                        cleaned_count += 1
                        repaired += 1
                conn.commit()
                if stopped:
                    break
                if len(rows) < 200:
                    finished = True
                    break
            # The marker is written only when the migration has finished.
            if finished:
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
            remaining = left()
            if remaining is not None and remaining <= 0:
                break
            batch = 200 if remaining is None else min(200, remaining)
            rows = find_unindexed_memories(conn, limit=batch)  # OFFSET=0 always
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
            if len(rows) < batch:
                break
    finally:
        conn.close()

    if repaired > 0:
        print(f"Repaired {repaired} record(s).", file=sys.stderr)
    return repaired


try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
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

    class RepairRequest(BaseModel):
        max_records: Optional[int] = None

    class ShutdownRequest(BaseModel):
        root: str

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

    @app.middleware('http')
    async def _host_guard(request: Request, call_next):
        # spec §3 (v1.5.0): DNS-rebinding guard on every endpoint
        if not _host_ok(request.headers.get('host')):
            return JSONResponse({'detail': 'Host not allowed'}, status_code=403)
        return await call_next(request)

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

    # spec §3 (v1.5.0): endpoints that embed or touch the DB are plain `def`
    # (run in the thread pool, /health stays responsive); one SQLite
    # connection per request.
    @app.post('/buffer')
    def ep_buffer(req: BufferRequest):
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
    def ep_search(req: SearchRequest):
        from .core.processor import hybrid_search
        try:
            since, until = _parse_time_range(req.since, req.until)
        except TimeBoundError as e:
            raise HTTPException(status_code=400, detail=str(e))
        sess = req.session_id or _srv_session
        return hybrid_search(
            DB_PATH, req.query or '', _srv_cfg, sess,
            since=since, until=until,
        )

    @app.post('/repair')
    def ep_repair(req: Optional[RepairRequest] = None):
        max_records = req.max_records if req is not None else None
        return {'status': 'ok', 'count': _repair_db(max_records)}

    @app.get('/list')
    def ep_list():
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

    # spec §3: another folder's server owns the port → never write to its DB.
    # No server for this command (still starting / failed to start): save
    # directly (spec §3 "PID file handling", "Starting the server").
    if _FOREIGN_SERVER_ROOT is not None or _NO_SERVER_REASON is not None:
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

    if _NO_SERVER_REASON is not None:
        _emit(_status_md(f"_(memory search unavailable: {_NO_SERVER_REASON})_"))
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
    if _NO_SERVER_REASON is not None:
        print(f"N3MC: --repair skipped: {_NO_SERVER_REASON}.", file=sys.stderr)
        return
    try:
        result = _post(cfg, '/repair', {})   # no limit: process everything
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


def _behavior_notice_key(path: str) -> str:
    """spec §4 (v1.5.0): behavior_md_edited:<first 12 hex of sha256(abs path)>."""
    h = hashlib.sha256(os.path.abspath(path).encode('utf-8')).hexdigest()[:12]
    return f'behavior_md_edited:{h}'


def _ensure_behavior_md(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = _behavior_notice_key(path)
    # v1.4.0 used one key without the path; v1.5.0 never emits it.
    _clear_notice('behavior_md_edited')
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        return
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    first_line = text.lstrip('﻿').splitlines()[0].strip() if text else ''
    if first_line == _BEHAVIOR_MD_MARKER:
        # The user merged the template (or it is ours): nothing to report.
        _clear_notice(key)
        return
    if _behavior_md_hash(text) in _KNOWN_BEHAVIOR_MD_HASHES:
        _backup(path)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        _clear_notice(key)
        return
    _notify(
        key,
        f"The rules file {os.path.abspath(path)} has been edited by hand, so it "
        "was not updated. Merge the new template manually if needed.",
    )


_MEMORY_CONTEXT_IMPORT_RE = re.compile(r'^[ \t]*@[^\n]*memory_context\.md[ \t]*$')
_N3MC_COMMENT_PREFIXES = (
    '# N3MemoryCore — ',
    '# N3MemoryCore - ',
    '# Path resolves to ',
    '# If you have not yet run ',
)


def _cmp_path(path: str) -> str:
    """Normalize for comparison: forward and back slashes compare equal."""
    p = path.replace('\\', '/')
    return os.path.normcase(os.path.normpath(p))


def _is_own_context_import(line: str) -> bool:
    """spec §4 (v1.5.0): the @ line names this installation's own
    CONTEXT_FILE by an absolute path."""
    body = line.rstrip('\r\n')
    if not _MEMORY_CONTEXT_IMPORT_RE.match(body):
        return False
    target = body.strip()[1:].strip()
    target = os.path.expanduser(target)
    if not os.path.isabs(target):
        return False
    return _cmp_path(target) == _cmp_path(CONTEXT_FILE)


def _retire_memory_context(claude_md: str) -> None:
    """spec §4 (v1.5.0): remove the legacy @import of THIS installation's
    memory_context.md from <cwd>/.claude/CLAUDE.md (backup first, keep
    LF/CRLF) and delete the leftover global memory_context.md once."""
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
                if not _is_own_context_import(line):
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


# spec §4 --stop housekeeping (v1.5.0)
_TURN_ID_MAX_AGE_SECONDS = 30 * 86400
_AUDIT_LOG_MAX_BYTES = 50 * 1024 * 1024


def _housekeeping() -> None:
    """Delete turn_id_*.txt older than 30 days; rotate audit.log > 50 MB."""
    import glob
    now = time.time()
    for path in glob.glob(os.path.join(MEMORY_DIR, 'turn_id_*.txt')):
        try:
            if now - os.path.getmtime(path) > _TURN_ID_MAX_AGE_SECONDS:
                os.remove(path)
        except OSError as e:
            print(f"Warning: could not remove {path}: {e}", file=sys.stderr)
    try:
        if os.path.getsize(AUDIT_LOG) > _AUDIT_LOG_MAX_BYTES:
            os.replace(AUDIT_LOG, AUDIT_LOG + '.1')
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"Warning: could not rotate {AUDIT_LOG}: {e}", file=sys.stderr)


def cmd_stop(cfg: dict) -> None:
    """spec §4: behavior rules (versioned) + memory_context retirement +
    housekeeping. Writes nothing into CLAUDE.md. Does NOT stop the server.
    Never prints the physical-save warning (it does not write to the DB);
    each failing step prints one line to stderr."""
    cp = claude_paths()
    steps = (
        ('rules file', lambda: _ensure_behavior_md(cp['BEHAVIOR_MD'])),
        ('memory_context cleanup', lambda: _retire_memory_context(cp['CLAUDE_MD'])),
        ('housekeeping', _housekeeping),
    )
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            print(f"Warning: --stop {name} failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

_TRANSCRIPT_TAIL_BYTES = 5 * 1024 * 1024
# spec §4.5 D3 step 2: user-line texts that are not a real prompt.
_NON_PROMPT_PREFIXES = ('[Request interrupted by user', '<command-', '<local-command-')


class TranscriptUnreadable(Exception):
    pass


def _content_texts(content) -> Optional[list]:
    """Text parts of message.content: [str] for a string, the type=="text"
    parts for a list, None for anything else."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [p.get('text', '') for p in content
                if isinstance(p, dict) and p.get('type') == 'text'
                and isinstance(p.get('text', ''), str)]
    return None


def _read_previous_response(transcript_path, current_prompt: str) -> str:
    """spec §4.5 (decision D3, v1.5.0): the last assistant reply that
    precedes the current prompt, read from the session's JSONL transcript.

    Raises TranscriptUnreadable on any failure (the caller logs it)."""
    if not transcript_path or not isinstance(transcript_path, str):
        raise TranscriptUnreadable('no transcript_path')
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, 'rb') as f:
            start = max(0, size - _TRANSCRIPT_TAIL_BYTES)
            f.seek(start)
            data = f.read()
    except Exception as e:
        raise TranscriptUnreadable(f'{type(e).__name__}: {e}') from None
    text = data.decode('utf-8', errors='replace')
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]          # first line may be cut by the seek

    current = (current_prompt or '').strip()
    parsed_any = False
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        parsed_any = True
        if not isinstance(obj, dict) or obj.get('isSidechain') is True:
            continue
        typ = obj.get('type')
        if typ not in ('user', 'assistant'):
            continue
        msg = obj.get('message')
        content = msg.get('content') if isinstance(msg, dict) else None
        if typ == 'user':
            if obj.get('isMeta') is True:
                continue           # meta line, not a prompt
            parts = _content_texts(content)
            is_prompt = isinstance(content, str) or bool(
                isinstance(content, list) and any(
                    isinstance(p, dict) and p.get('type') == 'text' for p in content))
            if not is_prompt:
                continue           # tool_result-only line: same reply
            first = (parts[0] if parts else '').lstrip()
            if first.startswith(_NON_PROMPT_PREFIXES) or \
                    first.startswith(tuple(_SYSTEM_PROMPT_PREFIXES)):
                continue           # interruption marker / slash command / system input
            joined = '\n\n'.join(parts or []).strip()
            if joined == current or ' '.join(parts or []).strip() == current:
                continue           # the current prompt itself
            break                  # the previous real user prompt: no response
        # assistant: the LAST non-empty text part of the newest assistant line
        # that has one — the same block Stop receives as last_assistant_message.
        parts = _content_texts(content) if isinstance(content, list) else None
        texts = [p for p in (parts or []) if p and p.strip()]
        if texts:
            return texts[-1]
    if not parsed_any and any(l.strip() for l in lines):
        raise TranscriptUnreadable('no JSON lines')
    return ''


def _save_claude_text(text: str, cfg: dict, session_id: Optional[str],
                      turn_id: Optional[str]) -> None:
    """purify the whole text, then chunk and save as [claude] / [claude i/N]."""
    cleaned = purify_text(text)
    for chunk in add_chunk_prefixes(chunk_text(cleaned), 'claude'):
        _do_buffer(chunk, cfg, agent_name='claude-code',
                   session_id=session_id, turn_id=turn_id)


def cmd_hook_submit(cfg: dict) -> None:
    """spec §4.5: UserPromptSubmit hook — repair → buffer(claude) → search → buffer(user)."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    user_text     = _extract_text(data.get('prompt') or data.get('message') or '')
    cc_session_id = data.get('session_id', '') or ''
    if not isinstance(cc_session_id, str):
        cc_session_id = str(cc_session_id)

    # spec §4.5: system inputs (e.g. <task-notification>) — no search, no save,
    # no new turn_id; the reply to it belongs to the current exchange.
    if user_text.lstrip().startswith(_SYSTEM_PROMPT_PREFIXES):
        _append_audit({'hook': 'UserPromptSubmit', 'stage': 'skipped',
                       'reason': 'system_prompt'})
        _emit(_status_md("_No relevant memories found._"))
        return

    # Previous Claude response: a supplied last_assistant_message wins;
    # otherwise read it from the transcript (decision D3).
    last_claude = data.get('last_assistant_message')
    if not isinstance(last_claude, str) or not last_claude.strip():
        last_claude = ''
        try:
            last_claude = _read_previous_response(data.get('transcript_path'), user_text)
        except TranscriptUnreadable as e:
            _append_audit({'hook': 'UserPromptSubmit', 'stage': 'transcript_unreadable',
                           'reason': str(e)[:200]})
        except Exception as e:     # never let the transcript fail the hook
            _append_audit({'hook': 'UserPromptSubmit', 'stage': 'transcript_unreadable',
                           'reason': f'{type(e).__name__}: {e}'[:200]})

    server_ok = ensure_server(cfg)

    # Step 1: Repair unindexed records (own server only; one batch per turn)
    if server_ok:
        try:
            _post(cfg, '/repair', {'max_records': _HOOK_REPAIR_MAX_RECORDS})
        except Exception as e:
            logger.warning(f"Repair failed during hook: {e}")

    # Step 2: Save Claude's previous response under the previous turn_id
    if last_claude.strip():
        prev_tid = _read_turn_id(cc_session_id)
        _save_claude_text(last_claude, cfg, cc_session_id or None, prev_tid)

    # Step 3: Search memory for current query
    q = user_text[:cfg.get('search_query_max_chars', 2000)]
    _do_search(q, cfg, current_session=cc_session_id or None)

    # Step 4: a new turn_id for every typed prompt (also image-only, v1.5.0);
    # save the user message (purified as a whole, then chunked).
    new_tid = str(uuid.uuid4())
    _write_turn_id(new_tid, cc_session_id)
    if user_text.strip():
        cleaned = purify_text(user_text)
        for chunk in add_chunk_prefixes(chunk_text(cleaned), 'user'):
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
    if not isinstance(data, dict):
        data = {}

    last_claude   = data.get('last_assistant_message') or ''
    cc_session_id = data.get('session_id', '') or ''

    if not isinstance(last_claude, str) or not last_claude.strip():
        return

    tid = _read_turn_id(cc_session_id)
    if not tid:
        tid = str(uuid.uuid4())
        _write_turn_id(tid, cc_session_id)
    ensure_server(cfg)
    _save_claude_text(last_claude, cfg, cc_session_id or None, tid)


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

_HOOK_EVENTS = {  # event -> (entry-point name, module name)
    'UserPromptSubmit': ('n3mc-hook',      'n3memorycore.n3mc_hook'),
    'Stop':             ('n3mc-stop-hook', 'n3memorycore.n3mc_stop_hook'),
}

# spec §4 ② (v1.5.0): rules `--init` adds to permissions.allow
PERMISSION_RULES = (
    "Bash(python -m n3memorycore.n3memory --search *)",
    "Bash(python -m n3memorycore.n3memory --buffer *)",
    "Bash(python -m n3memorycore.n3memory --list*)",
    "Bash(python -m n3memorycore.n3memory --repair*)",
    "Bash(python -m n3memorycore.n3memory --stop*)",
    "Bash(python -m n3memorycore.n3memory --show-turn *)",
    "Bash(python -m n3memorycore.n3memory --recall-thread *)",
    "Bash(n3mc --search *)",
    "Bash(n3mc --buffer *)",
    "Bash(n3mc --list*)",
    "Bash(n3mc --repair*)",
    "Bash(n3mc --stop*)",
    "Bash(n3mc --show-turn *)",
    "Bash(n3mc --recall-thread *)",
)

_OTHER_N3MC_MARKERS = ('n3mc_hook', 'n3mc-hook', 'n3mc_stop_hook', 'n3mc-stop-hook')


def _hook_tokens(command: str) -> list:
    import shlex
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def is_own_hook(command: str, event: str) -> bool:
    """spec §4 ① Path B (decision D1): True only for a hook command that runs
    THIS package's hook — (a) the entry point n3mc-hook / n3mc-stop-hook
    (.exe ignored, any folder), (b) `-m n3memorycore.n3mc_hook`, or (c)
    n3mc_hook.py / n3mc_stop_hook.py inside this package's folder."""
    if event not in _HOOK_EVENTS or not isinstance(command, str):
        return False
    exe_name, module = _HOOK_EVENTS[event]
    toks = _hook_tokens(command)
    if not toks:
        return False
    first = toks[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
    if first.endswith('.exe'):
        first = first[:-4]
    if first == exe_name:
        return True
    for i, t in enumerate(toks[:-1]):
        if t == '-m' and toks[i + 1] == module:
            return True
    script = module.split('.')[-1] + '.py'
    pkg_dir = os.path.normcase(os.path.abspath(_PKG_DIR))
    for t in toks[1:]:
        tt = os.path.expanduser(t)
        if os.path.basename(tt.replace('\\', '/')).lower() == script:
            d = os.path.normcase(os.path.abspath(os.path.dirname(tt)))
            if d == pkg_dir:
                return True
    return False


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


def _settings_path() -> str:
    return os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')


def _merge_settings(settings: dict) -> list:
    """Merge our hooks and the permission rules into `settings` (in place).
    Returns the report lines about other N3MC hooks that were left alone."""
    report = []
    hooks = settings.setdefault('hooks', {})
    for event, (exe_name, _module) in _HOOK_EVENTS.items():
        kept = []
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise ValueError(f'hooks.{event} is not a list')
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            inner_all = entry.get('hooks', [])
            if not isinstance(inner_all, list):
                kept.append(entry)
                continue
            inner = [h for h in inner_all
                     if not (isinstance(h, dict) and is_own_hook(h.get('command', ''), event))]
            for h in inner:
                c = h.get('command', '') if isinstance(h, dict) else ''
                if isinstance(c, str) and any(m in c for m in _OTHER_N3MC_MARKERS):
                    report.append(f"Another N3MC hook is registered for {event} "
                                  f"and was left in place: {c}")
            if inner:
                kept.append({**entry, 'hooks': inner})
        kept.append({'hooks': [{'type': 'command',
                                'command': _resolve_hook_command(exe_name)}]})
        hooks[event] = kept

    allow = settings.setdefault('permissions', {}).setdefault('allow', [])
    for rule in PERMISSION_RULES:
        if rule not in allow:
            allow.append(rule)
    return report


def cmd_init() -> int:
    """Set up ~/.n3mc/ and register hooks + permission rules in
    ~/.claude/settings.json (spec §4 ①②, v1.5.0). Returns the exit status."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    print(f"Data directory: {HOME_DIR}")

    cfg = _load_config()
    print(f"Config: {CONFIG_FILE}")
    print(f"  owner_id = {cfg['owner_id']}")
    print(f"  port     = {cfg['server_port']}")

    settings_path = _settings_path()

    # 1. Read. An unreadable file is never touched.
    raw = None
    if os.path.exists(settings_path):
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                raw = f.read()
            settings = json.loads(raw)
        except Exception as e:
            print(f"{settings_path} is not valid JSON ({e}). Nothing was changed. "
                  "Fix the file, then run n3mc --init again.", file=sys.stderr)
            return 1
        perms = settings.get('permissions', {}) if isinstance(settings, dict) else None
        if (not isinstance(settings, dict)
                or not isinstance(settings.get('hooks', {}), dict)
                or not isinstance(perms, dict)
                or not isinstance(perms.get('allow', []), list)):
            print(f"{settings_path} has an unexpected structure. Nothing was changed.",
                  file=sys.stderr)
            return 1
        before = json.loads(raw)
    else:
        settings, before = {}, None

    # 2-3. Merge hooks and permission rules.
    try:
        report = _merge_settings(settings)
    except ValueError as e:
        print(f"{settings_path} has an unexpected structure ({e}). "
              "Nothing was changed.", file=sys.stderr)
        return 1
    for line in report:
        print(line)

    # 4. Write only when something changed: back up, then replace atomically.
    if settings != before:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        if raw is not None:
            stamp = datetime.now().strftime('%Y%m%d%H%M%S')
            bak = os.path.join(os.path.dirname(settings_path),
                               f'settings.json.bak-n3mc-{stamp}')
            shutil.copy2(settings_path, bak)
            print(f"Backup: {bak}")
        _atomic_write_text(settings_path,
                           json.dumps(settings, indent=2, ensure_ascii=False) + '\n')
        print(f"Hooks and permission rules registered in: {settings_path}")
    else:
        print(f"Already registered (no change): {settings_path}")
    for event, (exe_name, _m) in _HOOK_EVENTS.items():
        print(f"  {event:<16} -> {_resolve_hook_command(exe_name)}")
    print("Done. Restart Claude Code: saving will start from the next session.")
    return 0


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
    parser.add_argument('--since', default=None,
                        help='Filter from (2026-11-13 or 2026-11-13T09:00; local time unless an offset is given)')
    parser.add_argument('--until', default=None,
                        help='Filter until (a date means the end of that day)')
    parser.add_argument('--before', type=int, default=2, help='Turns before target (default 2)')
    parser.add_argument('--after',  type=int, default=2, help='Turns after target (default 2)')
    parser.add_argument('--format', dest='fmt', default='tsv',
                        choices=['tsv', 'jsonl'], help='Output format for --list/--recall-thread')

    args = parser.parse_args()

    if args.init:
        sys.exit(cmd_init())

    # spec §3.6 (v1.5.0): unreadable bounds → one stderr line, exit status 2
    try:
        since, until = _parse_time_range(args.since, args.until)
    except TimeBoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    cfg = _load_config()

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
