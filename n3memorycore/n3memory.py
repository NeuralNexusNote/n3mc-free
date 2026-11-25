"""N3MemoryCore main CLI module and the resident FastAPI server.

Commands: --init / --buffer / --search / --list / --recall-thread / --show-turn / --stop / --repair /
--reembed / --hook-submit / --save-claude-turn / --run-server.
"""
import os as _os
import sys as _sys

if __name__ == "__main__":
    # Safe import path. Started as `python -m <package>.<module>`, Python has put the current folder
    # first on sys.path, so a file there named like a module imported below would run inside this
    # program. Drop that entry, unless this package itself lives in the current folder (a source
    # checkout run in place). Nothing else may be imported before this block.
    try:
        _cwd = _os.path.normcase(_os.path.abspath(_os.getcwd()))
        _pkg_parent = _os.path.normcase(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        if _sys.path and _os.path.normcase(_os.path.abspath(_sys.path[0])) == _cwd and _pkg_parent != _cwd:
            del _sys.path[0]
    except Exception:  # noqa: BLE001
        pass

import argparse
import datetime as _datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import n3memorycore
from .paths import (HOME_DIR, MEMORY_DIR, DB_PATH, PID_FILE, AUDIT_LOG, CONFIG_FILE, NOTICES_FILE,
                    claude_paths, SERVER_LOG, SERVER_START_FAILED, TURN_ID_FILE, python_cmd)
from .core.database import (
    get_connection, init_db, migrate_schema, insert_memory, insert_memory_once,
    check_exact_duplicate, search_vector, find_unindexed_memories,
    strip_fts_punctuation, serialize_vector,
    fts_index_text, bigram_text, get_meta, set_meta, delete_meta, vec_table_dim, count_memories,
    get_all_memories, get_memories_by_turn_id, FTS_TEXT_RULES, _FTS_PUNCT_RE,
)
from .core.processor import (
    embed_passage, cosine_sim_from_l2, hybrid_search,
    chunk_text, add_chunk_prefixes, purify_text, render_memory_context,
    render_placeholder, sanitize_surrogates, set_embed_model, _fmt_saved, NO_RESULTS,
)
from .core import processor as _processor

try:
    from uuid_extensions import uuid7
except ImportError:  # pragma: no cover - the dependency is required
    uuid7 = None

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:  # pragma: no cover
    psutil = None
    _PSUTIL_OK = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "owner_id": None,
    "local_id": None,
    "bind_host": "127.0.0.1",
    "server_port": 18520,
    "embed_model": "intfloat/multilingual-e5-base",
    "half_life_days": 90,
    "bm25_min_threshold": 0.1,
    "search_result_limit": 20,
    "min_score": 0.2,
    "search_query_max_chars": 2000,
    "vector_min_cos": 0.85,
    "relative_min_score_ratio": 0.6,
    "min_results": 3,
    "pair_top_n": 3,
    "pair_max_chars": 3000,
    "version_order_enabled": True,
    "version_sim_threshold": 0.98,
    "version_min_chars": 100,
    "reply_sim_rerank_enabled": False,
    "reply_sim_alpha": 0.3,
    "reply_sim_floor": 0.80,
    "reply_sim_span": 0.15,
}

# key -> (kind, low, low_inclusive, high)   kind: "int" (JSON integer only) or "num" (int or float)
_CONFIG_RANGES = {
    "server_port": ("int", 1, True, 65535),
    "search_result_limit": ("int", 1, True, 1365),
    "pair_top_n": ("int", 1, True, None),
    "pair_max_chars": ("int", 1, True, None),
    "search_query_max_chars": ("int", 1, True, None),
    "version_min_chars": ("int", 1, True, None),
    "min_results": ("int", 0, True, None),
    "half_life_days": ("num", 0, False, None),
    "bm25_min_threshold": ("num", 0, True, None),
    "min_score": ("num", 0, True, 1),
    "vector_min_cos": ("num", 0, True, 1),
    "relative_min_score_ratio": ("num", 0, True, 1),
    "version_sim_threshold": ("num", 0, True, 1),
    "reply_sim_alpha": ("num", 0, True, 1),
    "reply_sim_floor": ("num", 0, True, 1),
    "reply_sim_span": ("num", 0, False, None),
}

AGENT_NAME = "claude-code"
_SYSTEM_PROMPT_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")
_NOT_A_PROMPT_PREFIXES = ("[Request interrupted by user", "<command-", "<local-command-")

HTTP_TIMEOUT = 30
HEALTH_TIMEOUT = 5
SERVER_WAIT_SECONDS = 60
POLL_INTERVAL = 0.5
PORT_FREE_WAIT = 15
PORT_FREE_INTERVAL = 0.3
START_FAILED_COOLDOWN = 300
EMPTY_PID_GRACE = 60
SERVER_LOG_MAX = 1024 * 1024
AUDIT_ROTATE_BYTES = 50 * 1024 * 1024
AUDIT_RAW_MAX = 4096
TURN_FILE_MAX_AGE = 30 * 86400
NOTICE_INTERVAL = 86400
TRANSCRIPT_TAIL = 5 * 1024 * 1024
REPAIR_BATCH = 200
HOOK_REPAIR_MAX = 200
HOOK_TIMEOUT_SECONDS = 120
LEGACY_ROOT = "(unknown: the server did not report its folder)"
MIN_TS = "0001-01-01T00:00:00+00:00"
MAX_TS = "9999-12-31T23:59:59.999999+00:00"

_sleep = time.sleep

# Module state
_CFG = None
_CONFIG_RAW = None
_FOREIGN_SERVER_ROOT = None
_NO_SERVER_REASON = None
_QUICK_CHECK_DONE = False
_CLI_SESSION_ID = str(uuid.uuid4())


def _mem(name):
    return os.path.join(MEMORY_DIR, name)


def _ensure_dirs():
    os.makedirs(MEMORY_DIR, exist_ok=True)


def _now_ts():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _new_id():
    return str(uuid7()) if uuid7 is not None else str(uuid.uuid4())


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _err(msg):
    try:
        print(msg, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# audit.log and notices
# ---------------------------------------------------------------------------

def _audit(record):
    try:
        _ensure_dirs()
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: audit.log could not be written ({type(e).__name__}: {e})")


def _atomic_write_bytes(path, data: bytes):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_notices():
    try:
        with open(NOTICES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_notices(d):
    _atomic_write_bytes(NOTICES_FILE, (json.dumps(d, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _notify(key, message):
    """A notice a person has to act on: stderr, audit.log (once) and notices.json."""
    _err(f"N3MC notice: {message}")
    try:
        d = _read_notices()
        cur = d.get(key)
        if isinstance(cur, dict) and cur.get("message") == message:
            return
        d[key] = {"message": message, "last_shown": None}
        _write_notices(d)
        _audit({"ts": _now_ts(), "hook": "notice", "key": key, "message": message})
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: notices.json could not be written ({type(e).__name__}: {e})")


def _clear_notice(key):
    try:
        d = _read_notices()
        if key in d:
            del d[key]
            _write_notices(d)
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: notices.json could not be written ({type(e).__name__}: {e})")


def _take_notices():
    """The notices to show now (each key at most once per 24 hours)."""
    try:
        d = _read_notices()
        now = time.time()
        out, changed = [], False
        for key, v in d.items():
            if not isinstance(v, dict):
                continue
            last = v.get("last_shown")
            if isinstance(last, bool) or not isinstance(last, (int, float)):
                last = None
            if last is None or now - last >= NOTICE_INTERVAL:
                out.append(str(v.get("message", "")))
                v["last_shown"] = now
                changed = True
        if changed:
            _write_notices(d)
        return out
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: notices could not be read ({type(e).__name__}: {e})")
        return []


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------

def _config_value_ok(key, value) -> bool:
    if key not in DEFAULT_CONFIG:
        return True
    default = DEFAULT_CONFIG[key]
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(value, bool):
        return False
    rng = _CONFIG_RANGES.get(key)
    if rng is None:
        return isinstance(value, type(default))
    kind, low, low_inc, high = rng
    if kind == "int":
        if not isinstance(value, int):
            return False
    else:
        if not isinstance(value, (int, float)):
            return False
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return False
    if low is not None:
        if low_inc and value < low:
            return False
        if not low_inc and value <= low:
            return False
    if high is not None and value > high:
        return False
    return True


def _read_config_file():
    """('missing', None, None) | ('ok', raw, obj) | ('bad', raw, reason)."""
    for attempt in range(21):
        try:
            with open(CONFIG_FILE, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return "missing", None, None
        except OSError as e:
            return "bad", None, f"{type(e).__name__}: {e}"
        reason = None
        obj = None
        try:
            obj = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(obj, dict):
                reason = "the top level is not a JSON object"
        except (ValueError, UnicodeDecodeError) as e:
            reason = f"{type(e).__name__}: {e}"
        if reason is None:
            return "ok", raw, obj
        try:
            fresh = time.time() - os.path.getmtime(CONFIG_FILE) <= 2
        except OSError:
            fresh = False
        if fresh and attempt < 20:
            time.sleep(0.05)
            continue
        return "bad", raw, reason
    return "bad", None, "unreadable"


def _recover_ids_from_db():
    owner = local = None
    if not os.path.exists(DB_PATH):
        return None, None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            r = conn.execute("SELECT owner_id FROM memories WHERE owner_id IS NOT NULL "
                             "GROUP BY owner_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
            owner = r[0] if r else None
            r = conn.execute("SELECT local_id FROM memories WHERE local_id IS NOT NULL "
                             "GROUP BY local_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
            local = r[0] if r else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None, None
    return owner, local


def _dump_config(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _acquire_config_lock(timeout=10.0):
    lock = _mem("config.lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 10:
                    os.remove(lock)
                    continue
            except OSError:
                pass
        except OSError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _release_config_lock():
    try:
        os.remove(_mem("config.lock"))
    except OSError:
        pass


def _valid_id(v):
    return isinstance(v, str) and v.strip() != ""


def _fill_missing(obj):
    """Fill IDs (under the lock, re-reading the file) and missing default keys. Returns (obj, raw)."""
    need_ids = not (_valid_id(obj.get("owner_id")) and _valid_id(obj.get("local_id")))
    locked = False
    if need_ids:
        locked = _acquire_config_lock()
    try:
        if need_ids:
            state, raw, again = _read_config_file()
            if state == "ok":
                obj = again
        new = dict(obj)
        if not (_valid_id(new.get("owner_id")) and _valid_id(new.get("local_id"))):
            db_owner, db_local = _recover_ids_from_db()
            if not _valid_id(new.get("owner_id")):
                new["owner_id"] = db_owner or str(uuid.uuid4())
            if not _valid_id(new.get("local_id")):
                new["local_id"] = db_local or str(uuid.uuid4())
        for k, v in DEFAULT_CONFIG.items():
            if k not in new:
                new[k] = v
        data = _dump_config(new)
        _atomic_write_bytes(CONFIG_FILE, data)
        return new, data
    finally:
        if locked:
            _release_config_lock()


def _load_config():
    """Read config.json (never overwrite a file that cannot be read) and return the settings."""
    global _CFG, _CONFIG_RAW
    _ensure_dirs()
    for _ in range(3):
        state, raw, obj = _read_config_file()
        if state != "missing":
            break
        cfg = dict(DEFAULT_CONFIG)
        owner, local = _recover_ids_from_db()
        cfg["owner_id"] = owner or str(uuid.uuid4())
        cfg["local_id"] = local or str(uuid.uuid4())
        data = _dump_config(cfg)
        try:
            with open(CONFIG_FILE, "xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            continue  # another process created it first: read that file
        _CONFIG_RAW = data
        _CFG = cfg
        _clear_notice("config_unreadable")
        _clear_notice("config_type")
        set_embed_model(cfg["embed_model"])
        return cfg
    if state == "bad":
        cfg = dict(DEFAULT_CONFIG)
        owner, local = _recover_ids_from_db()
        cfg["owner_id"] = owner or str(uuid.uuid4())
        cfg["local_id"] = local or str(uuid.uuid4())
        _notify("config_unreadable", f"{CONFIG_FILE} cannot be read ({obj}). N3MC runs with default settings "
                                     "and did not change the file. Fix the file; the notice disappears once it can be read.")
        _CONFIG_RAW = raw
        _CFG = cfg
        set_embed_model(cfg["embed_model"])
        return cfg
    if state == "missing":  # pragma: no cover - created and removed repeatedly
        raise RuntimeError(f"{CONFIG_FILE} could not be created")
    missing_keys = [k for k in DEFAULT_CONFIG if k not in obj]
    if missing_keys or not (_valid_id(obj.get("owner_id")) and _valid_id(obj.get("local_id"))):
        try:
            obj, raw = _fill_missing(obj)
        except OSError as e:
            _err(f"N3MC: {CONFIG_FILE} could not be updated ({type(e).__name__}: {e})")
            if not _valid_id(obj.get("owner_id")) or not _valid_id(obj.get("local_id")):
                db_owner, db_local = _recover_ids_from_db()
                obj = dict(obj)
                obj.setdefault("owner_id", db_owner or str(uuid.uuid4()))
                obj.setdefault("local_id", db_local or str(uuid.uuid4()))
    cfg = dict(DEFAULT_CONFIG)
    bad = []
    for k, v in obj.items():
        if k in DEFAULT_CONFIG and DEFAULT_CONFIG[k] is not None and not _config_value_ok(k, v):
            bad.append(k)
            continue
        cfg[k] = v
    _clear_notice("config_unreadable")
    if bad:
        _notify("config_type", f"{CONFIG_FILE}: these settings have a wrong type or a value out of range and "
                               f"were ignored (the defaults are used): {', '.join(sorted(bad))}. "
                               "Write numbers without quotes.")
    else:
        _clear_notice("config_type")
    _CONFIG_RAW = raw
    _CFG = cfg
    set_embed_model(cfg["embed_model"])
    return cfg


def compute_code_version(config_bytes, pkg_dir=None) -> str:
    """SHA-256 (first 16 hex digits) of every .py file of the package and of config.json."""
    pkg = pkg_dir or os.path.dirname(os.path.abspath(n3memorycore.__file__))
    files = []
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, pkg).replace(os.sep, "/")
                files.append((rel, full))
    h = hashlib.sha256()
    for rel, full in sorted(files):
        h.update(rel.encode("utf-8") + b"\0")
        with open(full, "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    if config_bytes is not None:
        try:
            canon = json.dumps(json.loads(config_bytes.decode("utf-8-sig")), sort_keys=True).encode("utf-8")
        except (ValueError, UnicodeDecodeError):
            canon = config_bytes
        h.update(b"config\0" + canon)
    return h.hexdigest()[:16]


def _client_code_version():
    return compute_code_version(_CONFIG_RAW)


def _current_port(cfg=None):
    cfg = cfg or _CFG
    return int(cfg["server_port"]) if cfg else DEFAULT_CONFIG["server_port"]


# ---------------------------------------------------------------------------
# HTTP client (never through a proxy, never with an Origin header)
# ---------------------------------------------------------------------------

def _request(method, path, body=None, timeout=HTTP_TIMEOUT, port=None):
    port = port or _current_port()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    return json.loads(text) if text else None


def _post(path, body=None, timeout=HTTP_TIMEOUT, port=None):
    return _request("POST", path, body if body is not None else {}, timeout=timeout, port=port)


def _get(path, timeout=HEALTH_TIMEOUT, port=None):
    return _request("GET", path, None, timeout=timeout, port=port)


def _health(port=None):
    try:
        h = _get("/health", timeout=HEALTH_TIMEOUT, port=port)
        return h if isinstance(h, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _request_shutdown(port=None) -> bool:
    r = _post("/shutdown", {"root": HOME_DIR}, timeout=HEALTH_TIMEOUT, port=port)
    return isinstance(r, dict) and r.get("status") == "ok"


# ---------------------------------------------------------------------------
# Server check, replacement and start
# ---------------------------------------------------------------------------

def _health_decision(health) -> str:
    if not health:
        return "none"
    root = health.get("root")
    if not root:
        return "legacy"
    if not _same_path(root, HOME_DIR):
        return "foreign"
    if health.get("code_version") != _client_code_version():
        return "stale"
    return "ok"


def _process_home(env):
    h = env.get("N3MC_HOME")
    if h:
        return os.path.abspath(os.path.expanduser(h))
    home = env.get("USERPROFILE") if os.name == "nt" else env.get("HOME")
    home = home or env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, ".n3mc")


def _pid_on_port(port):
    if not _PSUTIL_OK:
        return None
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return c.pid
    except Exception:  # noqa: BLE001
        return None
    return None


def _is_own_server_process(pid=None, port=None) -> bool:
    """True only when the process is `-m n3memorycore.n3memory --run-server` of this data folder."""
    if not _PSUTIL_OK:
        return False
    if not pid and port:
        pid = _pid_on_port(port)
    if not pid:
        return False
    try:
        p = psutil.Process(int(pid))
        cmd = p.cmdline()
        env = p.environ()
    except Exception:  # noqa: BLE001
        return False
    if "n3memorycore.n3memory" not in cmd or "--run-server" not in cmd:
        return False
    return _same_path(_process_home(env), HOME_DIR)


def _listening_ports(pid):
    if not _PSUTIL_OK or not pid:
        return set()
    try:
        p = psutil.Process(int(pid))
        conns = p.net_connections(kind="inet") if hasattr(p, "net_connections") else p.connections(kind="inet")
        return {c.laddr.port for c in conns if c.status == psutil.CONN_LISTEN and c.laddr}
    except Exception:  # noqa: BLE001
        return set()


def _read_pid_file():
    """(exists, pid or None, age in seconds)."""
    try:
        st = os.stat(PID_FILE)
    except OSError:
        return False, None, 0.0
    age = time.time() - st.st_mtime
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            text = f.read().strip()
        pid = int(text) if text else None
    except (OSError, ValueError):
        pid = None
    return True, pid, age


def _remove_pid_file_if(pid):
    exists, cur, _ = _read_pid_file()
    if exists and cur == pid:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


def _wait_port_free(port):
    deadline = time.monotonic() + PORT_FREE_WAIT
    while time.monotonic() < deadline:
        if _health(port) is None:
            return True
        _sleep(PORT_FREE_INTERVAL)
    return _health(port) is None


def _wait_pid_exit(pid, seconds=PORT_FREE_WAIT):
    if not _PSUTIL_OK or not pid:
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if not psutil.pid_exists(int(pid)):
                return
        except Exception:  # noqa: BLE001
            return
        _sleep(PORT_FREE_INTERVAL)


def _stop_stale_server(health, port=None) -> bool:
    """Ask the server to stop (/shutdown). Only a server without /shutdown is terminated, after psutil
    confirms it is ours. Never by the number in the PID file alone."""
    pid = health.get("pid") if isinstance(health, dict) else None
    try:
        if _request_shutdown(port):
            _wait_port_free(port)
            if pid:
                _remove_pid_file_if(pid)
            return True
        return False
    except urllib.error.HTTPError as e:
        if e.code not in (404, 405):
            return False
    except Exception:  # noqa: BLE001
        return False
    if not _is_own_server_process(pid, port):
        return False
    if not pid:
        pid = _pid_on_port(port)
    try:
        p = psutil.Process(int(pid))
        p.terminate()
        try:
            p.wait(5)
        except Exception:  # noqa: BLE001
            p.kill()
    except Exception:  # noqa: BLE001
        return False
    _remove_pid_file_if(pid)
    return True


def _server_cwd():
    """The data folder; for a source checkout run in place, that folder (the child finds the package there)."""
    cwd = os.path.abspath(os.getcwd())
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _same_path(pkg_parent, cwd):
        return cwd
    return os.path.abspath(HOME_DIR)


def _spawn(cmd, **kwargs):
    return subprocess.Popen(cmd, **kwargs)


def _set_foreign(root, port):
    global _FOREIGN_SERVER_ROOT
    _FOREIGN_SERVER_ROOT = root
    _notify("port_conflict",
            f"Port {port} is used by the N3MC server of another folder ({root}). Memories of this folder "
            f"({HOME_DIR}) are saved directly and not searched until the port is free. Give each N3MC "
            "folder its own server_port in its config.json.")


def _server_responded():
    for key in ("stale_server", "port_conflict", "server_start_failed"):
        _clear_notice(key)
    try:
        os.remove(SERVER_START_FAILED)
    except OSError:
        pass


def _pid_status(pid, age):
    if pid is None:
        return "starting" if age < EMPTY_PID_GRACE else "dead"
    if not _PSUTIL_OK:
        return "starting" if age < EMPTY_PID_GRACE else "dead"
    return "starting" if _is_own_server_process(pid) else "dead"


def _wait_for_starting(cfg) -> bool:
    global _NO_SERVER_REASON
    port = _current_port(cfg)
    steps = int(SERVER_WAIT_SECONDS / POLL_INTERVAL)
    for _ in range(steps):
        h = _health(port)
        d = _health_decision(h)
        if d in ("ok", "stale"):
            _server_responded()
            return True
        if d == "foreign":
            _set_foreign(h["root"], port)
            return False
        if d == "legacy":
            _set_foreign(LEGACY_ROOT, port)
            return False
        _sleep(POLL_INTERVAL)
    _NO_SERVER_REASON = "the memory server is still starting"
    return False


def _move_from_other_port(pid, port) -> bool:
    moved = False
    for p in sorted(_listening_ports(pid) - {port}):
        h = _health(p)
        if not h or not h.get("root") or not _same_path(h["root"], HOME_DIR):
            continue
        try:
            _request_shutdown(p)
        except Exception:  # noqa: BLE001
            continue
        _wait_pid_exit(pid)
        _remove_pid_file_if(pid)
        moved = True
    return moved


def _no_health_path(cfg) -> bool:
    port = _current_port(cfg)
    exists, pid, age = _read_pid_file()
    if not exists:
        return _start_server(cfg)
    if _pid_status(pid, age) == "dead":
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return _start_server(cfg)
    if pid and _PSUTIL_OK and _move_from_other_port(pid, port):
        return _start_server(cfg)
    return _wait_for_starting(cfg)


def _truncate_server_log():
    try:
        if os.path.getsize(SERVER_LOG) > SERVER_LOG_MAX:
            with open(SERVER_LOG, "wb"):
                pass
    except OSError:
        pass


def _reserve_pid_file() -> bool:
    try:
        fd = os.open(PID_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _start_server(cfg) -> bool:
    global _NO_SERVER_REASON
    port = _current_port(cfg)
    try:
        if time.time() - os.path.getmtime(SERVER_START_FAILED) < START_FAILED_COOLDOWN:
            _NO_SERVER_REASON = "the memory server failed to start a few minutes ago"
            return False
    except OSError:
        pass
    _ensure_dirs()
    if not _reserve_pid_file():
        exists, pid, age = _read_pid_file()
        if exists and _pid_status(pid, age) == "dead":
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            if not _reserve_pid_file():
                return _wait_for_starting(cfg)
        else:
            return _wait_for_starting(cfg)
    _truncate_server_log()
    env = os.environ.copy()
    env["N3MC_HOME"] = os.path.abspath(HOME_DIR)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    log = None
    try:
        log = open(SERVER_LOG, "ab")
        proc = _spawn(
            [*python_cmd(), "-m", "n3memorycore.n3memory", "--run-server"],
            stderr=log, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            cwd=_server_cwd(), env=env, **kwargs,
        )
    except Exception as e:  # noqa: BLE001
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        _err(f"N3MC: the memory server could not be started ({type(e).__name__}: {e})")
        _NO_SERVER_REASON = f"the memory server could not be started ({type(e).__name__})"
        return False
    finally:
        if log is not None:
            try:
                log.close()
            except Exception:  # noqa: BLE001
                pass
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except OSError:
        pass
    steps = int(SERVER_WAIT_SECONDS / POLL_INTERVAL)
    for _ in range(steps):
        rc = proc.poll()
        if rc is not None:
            _notify("server_start_failed", f"The N3MC memory server could not start (exit code {rc}). "
                                           f"See {SERVER_LOG} for the reason.")
            try:
                with open(SERVER_START_FAILED, "w", encoding="utf-8") as f:
                    f.write(_now_ts())
            except OSError:
                pass
            _remove_pid_file_if(proc.pid)
            _NO_SERVER_REASON = f"the memory server could not start (exit code {rc})"
            return False
        h = _health(port)
        d = _health_decision(h)
        if d in ("ok", "stale"):
            _server_responded()
            return True
        if d == "foreign":
            _set_foreign(h["root"], port)
            return False
        if d == "legacy":
            _set_foreign(LEGACY_ROOT, port)
            return False
        _sleep(POLL_INTERVAL)
    _NO_SERVER_REASON = "the memory server is still starting"
    return False


def ensure_server(cfg=None) -> bool:
    """True when this folder's server can be used; False means: go on without the server."""
    global _FOREIGN_SERVER_ROOT, _NO_SERVER_REASON
    cfg = cfg or _CFG or _load_config()
    _FOREIGN_SERVER_ROOT = None
    _NO_SERVER_REASON = None
    port = _current_port(cfg)
    h = _health(port)
    d = _health_decision(h)
    if d == "ok":
        _server_responded()
        return True
    if d == "stale":
        if _stop_stale_server(h, port):
            return _start_server(cfg)
        _notify("stale_server", "An old N3MC memory server could not be stopped. Another N3MC folder may use "
                                f"the same server_port ({port}). Check server_port in the config.json of each "
                                "folder and restart the server by hand.")
        return True  # our own old server: same DB, keep using it
    if d == "foreign":
        _set_foreign(h["root"], port)
        return False
    if d == "legacy":
        if _is_own_server_process(h.get("pid"), port) and _stop_stale_server(h, port):
            return _start_server(cfg)
        _set_foreign(LEGACY_ROOT, port)
        return False
    return _no_health_path(cfg)


# ---------------------------------------------------------------------------
# DB with corruption handling
# ---------------------------------------------------------------------------

def _is_corruption_error(e) -> bool:
    if not isinstance(e, sqlite3.DatabaseError) or isinstance(e, sqlite3.OperationalError):
        return False
    msg = str(e).lower()
    return "file is not a database" in msg or "malformed" in msg


_SIDECARS = ("-wal", "-shm")


def _make_holds():
    holds = []
    for suf in _SIDECARS:
        side = DB_PATH + suf
        hold = side + ".n3mc-hold"
        try:
            if os.path.exists(hold):
                os.remove(hold)
            if os.path.exists(side):
                try:
                    os.link(side, hold)
                except OSError:
                    shutil.copy2(side, hold)
                holds.append(hold)
        except OSError:
            pass
    return holds


def _drop_holds():
    for suf in _SIDECARS:
        try:
            os.remove(DB_PATH + suf + ".n3mc-hold")
        except OSError:
            pass


def _quarantine_db():
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    base = f"{DB_PATH}.corrupt-{stamp}"
    backup = base + ".bak"
    n = 2
    while os.path.exists(backup):
        backup = f"{base}-{n}.bak"
        n += 1
    os.replace(DB_PATH, backup)
    for suf in _SIDECARS:
        side = DB_PATH + suf
        hold = side + ".n3mc-hold"
        target = backup + suf
        try:
            if os.path.exists(side):
                os.replace(side, target)
            elif os.path.exists(hold):
                os.replace(hold, target)
        except OSError:
            pass
    _drop_holds()
    _err(f"N3MC: the memory DB failed its integrity check and was set aside as {backup}. A new empty DB "
         f"is in use. To recover records: sqlite3 {backup} .recover | sqlite3 recovered.db "
         "(rename the -wal / -shm files next to it first).")
    _notify("db_corrupt", f"The memory DB failed its integrity check and was set aside as {backup}. A new empty "
                          f"DB is in use. To recover records: sqlite3 {backup} .recover | sqlite3 recovered.db")
    return backup


def _connect_checked(pragma=None):
    """Open DB_PATH, run PRAGMA integrity_check / quick_check, set a corrupt DB aside, return the connection."""
    _ensure_dirs()
    _make_holds()
    try:
        try:
            conn = get_connection(DB_PATH)
            if pragma:
                rows = conn.execute(f"PRAGMA {pragma}").fetchall()
                if not rows or str(rows[0][0]).lower() != "ok":
                    conn.close()
                    _quarantine_db()
                    conn = get_connection(DB_PATH)
        except sqlite3.DatabaseError as e:
            if not _is_corruption_error(e):
                raise
            try:
                conn.close()  # noqa: F821 - may not exist
            except Exception:  # noqa: BLE001
                pass
            _quarantine_db()
            conn = get_connection(DB_PATH)
        init_db(conn)
        return conn
    finally:
        _drop_holds()


def _open_db():
    conn = get_connection(DB_PATH)
    init_db(conn)
    return conn


# ---------------------------------------------------------------------------
# Repair, meta and --reembed
# ---------------------------------------------------------------------------

def _check_model_meta(conn, cfg):
    model = cfg["embed_model"]
    recorded = get_meta(conn, "embed_model")
    if recorded is None:
        has_vec = conn.execute("SELECT 1 FROM memories_vec LIMIT 1").fetchone() is not None
        recorded = model
        if has_vec:
            try:
                with open(_mem("vec_model.txt"), "r", encoding="utf-8") as f:
                    txt = f.read().strip()
                if txt:
                    recorded = txt
            except OSError:
                pass
        set_meta(conn, "embed_model", recorded, commit=False)
        set_meta(conn, "embed_dim", vec_table_dim(conn), commit=False)
        set_meta(conn, "embed_model_at", _now_ts(), commit=False)
        conn.commit()
    elif get_meta(conn, "embed_dim") is None:
        set_meta(conn, "embed_dim", vec_table_dim(conn))
    if recorded != model:
        n = count_memories(conn)
        _notify("embed_model_mismatch",
                f"The stored vectors were made with {recorded}, but config.json sets embed_model to {model}. "
                f"Search quality is degraded until they are re-made: run n3mc --reembed (it re-embeds all {n} "
                f"records with {model}), or set embed_model back to {recorded}.")
    else:
        _clear_notice("embed_model_mismatch")


def _fts_rows(conn, rowid):
    a = conn.execute("SELECT content FROM memories_fts WHERE rowid = ?", (rowid,)).fetchone()
    b = conn.execute("SELECT content FROM memories_fts_bi WHERE rowid = ?", (rowid,)).fetchone()
    return (a[0] if a else None), (b[0] if b else None)


def run_repair(conn, cfg, max_records=None) -> int:
    """One-time FTS migrations and repair of records missing from memories_vec / memories_fts.
    Returns the number of records actually changed."""
    count = 0
    budget = max_records if max_records else None

    def take():
        return REPAIR_BATCH if budget is None else min(REPAIR_BATCH, budget)

    _check_model_meta(conn, cfg)

    # 1. FTS punctuation cleaning (keyset pagination, resumable)
    if get_meta(conn, "fts_punct_cleaned") is None:
        cursor = int(get_meta(conn, "fts_punct_cursor") or 0)
        while take() > 0:
            rows = conn.execute("SELECT rowid, content FROM memories WHERE rowid > ? ORDER BY rowid LIMIT ?",
                                (cursor, take())).fetchall()
            if not rows:
                set_meta(conn, "fts_punct_cleaned", "1", commit=False)
                delete_meta(conn, "fts_punct_cursor", commit=False)
                conn.commit()
                break
            for rowid, content in rows:
                cur = conn.execute("SELECT content FROM memories_fts WHERE rowid = ?", (rowid,)).fetchone()
                if cur is not None and _FTS_PUNCT_RE.search(cur[0] or ""):
                    conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
                    conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                                 (rowid, strip_fts_punctuation(fts_index_text(content))))
                    count += 1
            cursor = rows[-1][0]
            set_meta(conn, "fts_punct_cursor", cursor, commit=False)
            conn.commit()
            if budget is not None:
                budget -= len(rows)

    # 2. bigram index for rows saved before the table existed (not limited by max_records)
    if get_meta(conn, "fts_bigram") is None:
        while True:
            rows = conn.execute("SELECT rowid, content FROM memories WHERE rowid NOT IN "
                                "(SELECT rowid FROM memories_fts_bi) ORDER BY rowid LIMIT ?",
                                (REPAIR_BATCH,)).fetchall()
            if not rows:
                break
            for rowid, content in rows:
                conn.execute("INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
                             (rowid, bigram_text(fts_index_text(content))))
                count += 1
            conn.commit()
        set_meta(conn, "fts_bigram", "1")

    # 3. rebuild the FTS text when the rules changed
    if get_meta(conn, "fts_text_rules") != FTS_TEXT_RULES:
        cursor = int(get_meta(conn, "fts_text_cursor") or 0)
        while take() > 0:
            rows = conn.execute("SELECT rowid, content FROM memories WHERE rowid > ? ORDER BY rowid LIMIT ?",
                                (cursor, take())).fetchall()
            if not rows:
                set_meta(conn, "fts_text_rules", FTS_TEXT_RULES, commit=False)
                delete_meta(conn, "fts_text_cursor", commit=False)
                conn.commit()
                break
            for rowid, content in rows:
                idx = fts_index_text(content)
                want_tri, want_bi = strip_fts_punctuation(idx), bigram_text(idx)
                have_tri, have_bi = _fts_rows(conn, rowid)
                if have_tri != want_tri or have_bi != want_bi:
                    conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
                    conn.execute("DELETE FROM memories_fts_bi WHERE rowid = ?", (rowid,))
                    conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (rowid, want_tri))
                    conn.execute("INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)", (rowid, want_bi))
                    count += 1
            cursor = rows[-1][0]
            set_meta(conn, "fts_text_cursor", cursor, commit=False)
            conn.commit()
            if budget is not None:
                budget -= len(rows)

    # 4. records missing from memories_vec or memories_fts (OFFSET 0, batches of 200)
    while take() > 0:
        rows = find_unindexed_memories(conn, take())
        if not rows:
            break
        if conn.in_transaction:
            conn.commit()
        embeddings = {}
        for r in rows:  # outside any write transaction
            if not r["has_vec"]:
                try:
                    embeddings[r["rowid"]] = embed_passage(r["content"])
                except Exception:  # noqa: BLE001
                    embeddings[r["rowid"]] = None
        changed = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            for r in rows:
                cur = conn.execute("SELECT id, content FROM memories WHERE rowid = ?", (r["rowid"],)).fetchone()
                if not cur or (cur[0] != r["id"] and cur[1] != r["content"]):
                    continue
                rowid, content = r["rowid"], cur[1]
                touched = False
                idx = fts_index_text(content)
                if conn.execute("SELECT 1 FROM memories_fts WHERE rowid = ?", (rowid,)).fetchone() is None:
                    conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                                 (rowid, strip_fts_punctuation(idx)))
                    touched = True
                if conn.execute("SELECT 1 FROM memories_fts_bi WHERE rowid = ?", (rowid,)).fetchone() is None:
                    conn.execute("INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
                                 (rowid, bigram_text(idx)))
                    touched = True
                emb = embeddings.get(rowid)
                if emb is not None and conn.execute("SELECT 1 FROM memories_vec WHERE rowid = ?",
                                                    (rowid,)).fetchone() is None:
                    conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                 (rowid, serialize_vector(emb)))
                    touched = True
                if touched:
                    changed += 1
            conn.commit()
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            _err(f"N3MC: repair stopped ({type(e).__name__}: {e})")
            break
        count += changed
        if budget is not None:
            budget -= len(rows)
        if changed == 0:
            break
    return count


def cmd_reembed() -> int:
    cfg = _load_config()
    model = cfg["embed_model"]
    port = _current_port(cfg)
    h = _health(port)
    if h and h.get("root") and _same_path(h["root"], HOME_DIR):
        try:
            if _request_shutdown(port):
                _wait_port_free(port)
        except Exception as e:  # noqa: BLE001
            _err(f"N3MC: the memory server could not be stopped ({type(e).__name__}: {e})")
    set_embed_model(model)
    conn = _connect_checked(None)
    try:
        dim = vec_table_dim(conn)
        try:
            probe = embed_passage("dimension check", name=model)
        except Exception as e:  # noqa: BLE001
            _err(f"N3MC: --reembed: the model {model} could not be loaded ({type(e).__name__}: {e})")
            return 1
        if len(probe) != dim:
            _err(f"N3MC: --reembed: {model} makes {len(probe)}-dimensional vectors but memories_vec has {dim}; "
                 "a different dimension needs the manual procedure in §3 \"Embeddings\".")
            return 1
        if get_meta(conn, "reembed_model") == model:
            cursor = int(get_meta(conn, "reembed_cursor") or 0)
        else:
            cursor = 0
            set_meta(conn, "reembed_model", model, commit=False)
            delete_meta(conn, "reembed_cursor", commit=False)
            conn.commit()
        total = count_memories(conn)
        done = conn.execute("SELECT COUNT(*) FROM memories WHERE rowid <= ?", (cursor,)).fetchone()[0]
        while True:
            rows = conn.execute("SELECT rowid, content FROM memories WHERE rowid > ? ORDER BY rowid LIMIT ?",
                                (cursor, REPAIR_BATCH)).fetchall()
            if not rows:
                break
            vecs = [embed_passage(content, name=model) for _, content in rows]
            if conn.in_transaction:
                conn.commit()
            try:
                for (rowid, _), vec in zip(rows, vecs):
                    conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid,))
                    conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                 (rowid, serialize_vector(vec)))
                cursor = rows[-1][0]
                set_meta(conn, "reembed_cursor", cursor, commit=False)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            done += len(rows)
            _err(f"Re-embedded {done} / {total}")
        set_meta(conn, "embed_model", model, commit=False)
        set_meta(conn, "embed_dim", dim, commit=False)
        set_meta(conn, "embed_model_at", _now_ts(), commit=False)
        delete_meta(conn, "reembed_model", commit=False)
        delete_meta(conn, "reembed_cursor", commit=False)
        conn.commit()
        _clear_notice("embed_model_mismatch")
        print(f"Re-embedded {total} record(s) with {model}.")
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Time bounds (--since / --until)
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?"
                      r"(Z|[+-]\d{2}:?\d{2})?$")


def _astimezone_utc(dt):
    return dt.astimezone(timezone.utc)


def _parse_time_bound(value, is_until=False):
    """A date or ISO 8601 date-time (local time unless it has an offset) -> UTC string as stored."""
    s = (value or "").strip()
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(value)
    date_only = m.group(4) is None
    try:
        if date_only:
            if m.group(8):
                raise ValueError(value)
            d = _datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            t = _datetime.time(23, 59, 59, 999999) if is_until else _datetime.time(0, 0, 0)
            dt = datetime.combine(d, t)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        raise ValueError(value)
    try:
        if dt.tzinfo is None:
            try:
                utc = _astimezone_utc(dt)
            except (OSError, OverflowError, ValueError):
                utc = dt.replace(tzinfo=timezone.utc)  # the local zone cannot be applied: read as UTC
        else:
            utc = dt.astimezone(timezone.utc)
    except (OverflowError, ValueError, OSError):
        return MIN_TS if dt.year <= 1 else MAX_TS
    return utc.isoformat(timespec="microseconds")


def _parse_bounds(since, until):
    """(since, until) as UTC strings; raises ValueError with the message to print."""
    s = u = None
    if since:
        try:
            s = _parse_time_bound(since, False)
        except ValueError:
            raise ValueError(f"--since: cannot read '{since}' (use 2026-11-13 or 2026-11-13T09:00)")
    if until:
        try:
            u = _parse_time_bound(until, True)
        except ValueError:
            raise ValueError(f"--until: cannot read '{until}' (use 2026-11-13 or 2026-11-13T09:00)")
    if s and u and s > u:
        raise ValueError("--since is later than --until")
    return s, u


# ---------------------------------------------------------------------------
# FastAPI server
# ---------------------------------------------------------------------------

_SERVER_STATE = {"cfg": None, "code_version": None, "port": None, "session_id": str(uuid.uuid4()),
                 "uvicorn": None}


def _server_cfg():
    if _SERVER_STATE["cfg"] is None:
        cfg = _load_config()
        _SERVER_STATE["cfg"] = cfg
        _SERVER_STATE["code_version"] = compute_code_version(_CONFIG_RAW)
        _SERVER_STATE["port"] = int(cfg["server_port"])
    return _SERVER_STATE["cfg"]


def _server_port():
    if _SERVER_STATE["port"] is None:
        _server_cfg()
    return _SERVER_STATE["port"]


@asynccontextmanager
async def lifespan(app_: FastAPI):
    cfg = _server_cfg()
    set_embed_model(cfg["embed_model"])
    try:
        _connect_checked("integrity_check").close()
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: the DB check failed ({type(e).__name__}: {e})")
    try:
        _processor.get_model()
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: the embedding model is not available; saving without vectors ({type(e).__name__}: {e})")
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def _local_only(request: Request, call_next):
    if "origin" in request.headers:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    port = _server_port()
    host = request.headers.get("host", "")
    if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)


@app.exception_handler(Exception)
async def _on_error(request: Request, exc: Exception):
    return JSONResponse({"status": "error", "message": f"{type(exc).__name__}: {exc}"}, status_code=500)


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


@app.get("/health")
def api_health():
    _server_cfg()
    return {"status": "ok", "root": os.path.abspath(HOME_DIR), "code_version": _SERVER_STATE["code_version"],
            "pid": os.getpid()}


@app.post("/buffer")
def api_buffer(req: BufferRequest):
    cfg = _server_cfg()
    content = purify_text(sanitize_surrogates(req.content or ""))
    if not content.strip():
        return {"status": "skipped", "count": 0, "reason": "empty"}
    conn = _open_db()
    try:
        if check_exact_duplicate(conn, content, turn_id=req.turn_id):
            _clear_notice("save_failed")
            return {"status": "skipped", "count": 0, "reason": "exact_duplicate"}
        try:
            emb = embed_passage(content)
        except Exception as e:  # noqa: BLE001
            _err(f"N3MC: saved without a vector ({type(e).__name__}: {e}); --repair adds it later")
            emb = None
        before = count_memories(conn)
        rowid = insert_memory_once(
            conn, _new_id(), content, _now_ts(), cfg["owner_id"], turn_id=req.turn_id,
            local_id=req.local_id or cfg["local_id"], agent_name=req.agent_name,
            session_id=req.session_id or _SERVER_STATE["session_id"], embedding=emb)
        if rowid is None:
            _clear_notice("save_failed")
            return {"status": "skipped", "count": 0, "reason": "exact_duplicate"}
        if count_memories(conn) <= before:
            return {"status": "error", "message": "record count did not change after INSERT"}
        _clear_notice("save_failed")
        return {"status": "ok", "count": 1}
    finally:
        conn.close()


@app.post("/search")
def api_search(req: SearchRequest):
    cfg = _server_cfg()
    try:
        since, until = _parse_bounds(req.since, req.until)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    query = sanitize_surrogates(req.query or "")[: int(cfg["search_query_max_chars"])]
    conn = _open_db()
    try:
        return hybrid_search(conn, query, cfg, session_id=req.session_id or _SERVER_STATE["session_id"],
                             since=since, until=until)
    finally:
        conn.close()


@app.post("/repair")
def api_repair(req: Optional[RepairRequest] = None):
    cfg = _server_cfg()
    conn = _open_db()
    try:
        n = run_repair(conn, cfg, max_records=(req.max_records if req else None))
    finally:
        conn.close()
    return {"status": "ok", "count": n}


@app.get("/list")
def api_list():
    conn = _open_db()
    try:
        rows = get_all_memories(conn)
    finally:
        conn.close()
    return {"records": [{"id": r["id"], "content": r["content"], "timestamp": r["timestamp"],
                         "agent_name": r["agent_name"]} for r in rows], "total": len(rows)}


def _exit_server():
    srv = _SERVER_STATE.get("uvicorn")
    _remove_pid_file_if(os.getpid())
    if srv is not None:
        srv.should_exit = True


@app.post("/shutdown")
def api_shutdown(req: ShutdownRequest):
    if not _same_path(req.root, HOME_DIR):
        raise HTTPException(status_code=403, detail="root mismatch")
    threading.Timer(0.3, _exit_server).start()
    return {"status": "ok"}


@app.delete("/delete/{memory_id}")
def api_delete(memory_id: str):
    return JSONResponse({"detail": "Pro feature"}, status_code=403)


@app.post("/gc")
def api_gc():
    return JSONResponse({"detail": "Pro feature"}, status_code=403)


@app.post("/import")
def api_import():
    return JSONResponse({"detail": "Pro feature"}, status_code=403)


def run_server():
    cfg = _load_config()
    _SERVER_STATE["cfg"] = cfg
    _SERVER_STATE["code_version"] = compute_code_version(_CONFIG_RAW)
    _SERVER_STATE["port"] = int(cfg["server_port"])
    if cfg["bind_host"] != "127.0.0.1":
        _err(f"N3MC: WARNING: bind_host is {cfg['bind_host']}. The server API has no authentication; "
             "use the default 127.0.0.1.")
    _ensure_dirs()
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        _err(f"N3MC: the PID file could not be written ({e})")
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=cfg["bind_host"], port=int(cfg["server_port"]),
                                           log_level="error"))
    _SERVER_STATE["uvicorn"] = server
    try:
        server.run()
    finally:
        _remove_pid_file_if(os.getpid())
    return 0


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

FATAL_WARNING = "⚠️ Physical save failed. Current memories may be lost."


def _buffer_direct(content, cfg, agent_name=None, session_id=None, turn_id=None, local_id=None):
    """Save without the server (no vector; --repair adds it later). Returns 'ok', 'skipped' or 'error'."""
    global _QUICK_CHECK_DONE
    try:
        content = purify_text(content)
        if not content.strip():
            return "skipped"
        pragma = None if _QUICK_CHECK_DONE else "quick_check"
        _QUICK_CHECK_DONE = True
        conn = _connect_checked(pragma)
        try:
            before = count_memories(conn)
            rowid = insert_memory_once(conn, _new_id(), content, _now_ts(), cfg["owner_id"], turn_id=turn_id,
                                       local_id=local_id or cfg["local_id"], agent_name=agent_name,
                                       session_id=session_id or _CLI_SESSION_ID, embedding=None)
            if rowid is None:
                _clear_notice("save_failed")
                return "skipped"
            if count_memories(conn) <= before:
                raise RuntimeError("record count did not change after INSERT")
        finally:
            conn.close()
        _clear_notice("save_failed")
        return "ok"
    except Exception as e:  # noqa: BLE001
        _err(f"{FATAL_WARNING} ({type(e).__name__}: {e})")
        _notify("save_failed", f"A record could not be saved ({type(e).__name__}: {e}). Current memories may "
                               "be lost; if it happens again, check that no other program holds the memory database.")
        return "error"


def _save_record(content, cfg, server_ok, agent_name=None, session_id=None, turn_id=None):
    if server_ok and _FOREIGN_SERVER_ROOT is None:
        try:
            resp = _post("/buffer", {"content": content, "agent_name": agent_name, "session_id": session_id,
                                     "turn_id": turn_id})
            if isinstance(resp, dict) and resp.get("status") in ("ok", "skipped"):
                _clear_notice("save_failed")
                return resp.get("status")
        except Exception:  # noqa: BLE001
            pass
    return _buffer_direct(content, cfg, agent_name=agent_name, session_id=session_id, turn_id=turn_id)


def _save_chunks(text, role, cfg, server_ok, session_id, turn_id):
    purified = purify_text(text)
    results = []
    for chunk in add_chunk_prefixes(chunk_text(purified, max_chars=400, overlap=40), role) if purified.strip() else []:
        results.append(_save_record(chunk, cfg, server_ok, agent_name=AGENT_NAME, session_id=session_id,
                                    turn_id=turn_id))
    return results


# ---------------------------------------------------------------------------
# Hook helpers
# ---------------------------------------------------------------------------

def _strip_images(payload):
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts = []
        for p in payload:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "\n".join(parts)
    return ""


def _extract_text(prompt):
    if prompt is None:
        return ""
    if isinstance(prompt, list):
        return _strip_images(prompt)
    if not isinstance(prompt, str):
        return ""
    s = prompt.strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
        except ValueError:
            return prompt
        if (isinstance(arr, list) and arr
                and all(isinstance(x, dict) and isinstance(x.get("type"), str) for x in arr)):
            return _strip_images(arr)
    return prompt


def _user_prompt_text(text):
    """The user's own text, or None for a system input to skip (§4.5 'skip system inputs')."""
    if text is None:
        return None
    s = text.strip()
    if s.startswith("<system-reminder>"):
        while s.startswith("<system-reminder>"):
            end = s.find("</system-reminder>")
            if end < 0:
                return None
            s = s[end + len("</system-reminder>"):].lstrip()
        rest = s.strip()
        if not rest or rest.startswith(_SYSTEM_PROMPT_PREFIXES):
            return None
        return rest
    if s.startswith(_SYSTEM_PROMPT_PREFIXES):
        return None
    return text


def _turn_id_file(session_id):
    if session_id:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(session_id))[:64]
        return _mem(f"turn_id_{safe}.txt")
    return TURN_ID_FILE


def _read_turn_id(session_id):
    try:
        with open(_turn_id_file(session_id), "r", encoding="utf-8") as f:
            t = f.read().strip()
        return t or None
    except OSError:
        return None


def _write_turn_id(session_id, turn_id):
    try:
        _ensure_dirs()
        with open(_turn_id_file(session_id), "w", encoding="utf-8") as f:
            f.write(turn_id)
    except OSError as e:
        _err(f"N3MC: the turn_id file could not be written ({e})")


def _transcript_user_text(msg):
    """Text of a transcript user line if it is a real prompt, else None."""
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        texts = [p for p in content if isinstance(p, str)]
        texts += [p.get("text") for p in content
                  if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)]
        if not texts:
            return None
        text = "\n".join(texts)
    else:
        return None
    s = text.strip()
    if s.startswith(_NOT_A_PROMPT_PREFIXES):
        return None
    if s.startswith(_SYSTEM_PROMPT_PREFIXES):
        return _user_prompt_text(s)
    return text


def _read_prev_reply(transcript_path, current_prompt):
    """(reply text or None, failure reason or None) from the session transcript (JSONL)."""
    if not transcript_path:
        return None, "no transcript_path"
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            seeked = size > TRANSCRIPT_TAIL
            if seeked:
                f.seek(size - TRANSCRIPT_TAIL)
            data = f.read()
    except OSError as e:
        return None, f"{type(e).__name__}"
    try:
        lines = data.decode("utf-8", errors="replace").splitlines()
        if seeked and lines:
            lines = lines[1:]
        cur = (current_prompt or "").strip()
        first = True
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("isSidechain") is True:
                continue
            typ = obj.get("type")
            if typ not in ("user", "assistant"):
                continue
            msg = obj.get("message")
            if typ == "user":
                if obj.get("isMeta") is True:
                    continue
                text = _transcript_user_text(msg)
                if text is None:
                    first = False
                    continue
                if first and text.strip() == cur:
                    first = False
                    continue
                return None, None  # an earlier real prompt: there is no previous reply
            first = False
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                texts = [content] if content.strip() else []
            elif isinstance(content, list):
                texts = [p.get("text") for p in content if isinstance(p, dict) and p.get("type") == "text"
                         and isinstance(p.get("text"), str) and p.get("text").strip()]
            else:
                texts = []
            if texts:
                return texts[-1], None
        return None, None
    except Exception as e:  # noqa: BLE001
        return None, f"unexpected structure ({type(e).__name__})"


def _search_output(query, cfg, server_ok, session_id=None, since=None, until=None):
    port = _current_port(cfg)
    if _FOREIGN_SERVER_ROOT is not None:
        return render_placeholder(f"_(memory search unavailable: another N3MC folder's server is using port {port})_",
                                  _take_notices())
    if not server_ok:
        reason = _NO_SERVER_REASON or "the memory server is not running"
        return render_placeholder(f"_(memory search unavailable: {reason})_", _take_notices())
    try:
        data = _post("/search", {"query": query, "session_id": session_id, "since": since, "until": until})
    except urllib.error.HTTPError as e:
        return render_placeholder(f"_(memory search unavailable: HTTP {e.code})_", _take_notices())
    except Exception as e:  # noqa: BLE001
        return render_placeholder(f"_(memory search unavailable: {type(e).__name__})_", _take_notices())
    return render_memory_context(query, data, _take_notices())


def _read_stdin_json():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return raw, sanitize_surrogates(payload)


def cmd_hook_submit() -> int:
    """UserPromptSubmit: repair -> save the previous reply -> search -> save the user message."""
    _, payload = _read_stdin_json()
    session_id = payload.get("session_id") or None
    prompt = payload.get("prompt")
    if prompt is None:
        prompt = payload.get("message")
    raw_text = sanitize_surrogates(_extract_text(prompt))
    text = _user_prompt_text(raw_text) if raw_text.strip() else raw_text
    if text is None:
        _audit({"ts": _now_ts(), "hook": "UserPromptSubmit", "stage": "skipped", "reason": "system_prompt"})
        sys.stdout.write(render_placeholder(NO_RESULTS))
        return 0
    prev_turn = _read_turn_id(session_id)
    cfg = _load_config()
    server_ok = ensure_server(cfg)
    if server_ok and _FOREIGN_SERVER_ROOT is None:
        try:
            r = _post("/repair", {"max_records": HOOK_REPAIR_MAX})
            if isinstance(r, dict) and r.get("count"):
                _err(f"N3MC: Repaired {r['count']} record(s)")
        except Exception as e:  # noqa: BLE001
            _err(f"N3MC: repair failed ({type(e).__name__})")
    # Step 2: the previous Claude reply (normally already saved by the Stop hook)
    prev = payload.get("last_assistant_message")
    if prev is not None:
        prev = sanitize_surrogates(_strip_images(prev))
    else:
        prev, reason = _read_prev_reply(payload.get("transcript_path"), text)
        if reason:
            _audit({"ts": _now_ts(), "hook": "UserPromptSubmit", "stage": "transcript_unreadable",
                    "reason": reason})
        prev = sanitize_surrogates(prev) if prev else prev
    if prev and prev.strip():
        _save_chunks(prev, "claude", cfg, server_ok, session_id, prev_turn or str(uuid.uuid4()))
    # Step 3: search (an exception here must not stop step 4)
    query = text[: int(cfg["search_query_max_chars"])]
    try:
        if query.strip():
            out = _search_output(query, cfg, server_ok, session_id=session_id)
        else:
            out = render_placeholder(NO_RESULTS, _take_notices())
    except Exception as e:  # noqa: BLE001
        out = render_placeholder(f"_(memory search unavailable: {type(e).__name__})_")
    try:
        sys.stdout.write(out)
        sys.stdout.flush()
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: the search result could not be written ({type(e).__name__})")
    # Step 4: a new turn and the user message
    new_turn = str(uuid.uuid4())
    _write_turn_id(session_id, new_turn)
    if text.strip():
        _save_chunks(text, "user", cfg, server_ok, session_id, new_turn)
    return 0


def cmd_save_claude_turn() -> int:
    _, payload = _read_stdin_json()
    session_id = payload.get("session_id") or None
    turn_id = _read_turn_id(session_id)  # before the config and ensure_server
    msg = payload.get("last_assistant_message")
    text = sanitize_surrogates(_strip_images(msg)) if msg is not None else ""
    if not text or not text.strip():
        return 0
    turn_id = turn_id or str(uuid.uuid4())
    cfg = _load_config()
    server_ok = ensure_server(cfg)
    results = _save_chunks(text, "claude", cfg, server_ok, session_id, turn_id)
    return 1 if "error" in results else 0


# ---------------------------------------------------------------------------
# --stop: rules file, retiring memory_context.md, housekeeping
# ---------------------------------------------------------------------------

BEHAVIOR_MARKER = "<!-- n3mc-behavior v3 -->"
_KNOWN_BEHAVIOR_MD_HASHES = {"ade0a59b94f59f9a", "34ed39efe5a3833d", "5af3c015c06c62c1"}
_BEHAVIOR_MD_CONTENT = """<!-- n3mc-behavior v3 -->
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

## Weak Matches
Records under `## Possibly related (weak matches — may be unrelated)` did not
pass the relevance floors and are often unrelated. Use one only when it is
actually relevant to the question; if it is not, ignore it and do not mention
it.

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


def _content_hash16(text):
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _stamp():
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _ensure_behavior_md(project):
    _clear_notice("behavior_md_edited")  # the v1.4.0 key without the path hash
    path = os.path.abspath(claude_paths(project)["behavior_md"])
    key = "behavior_md_edited:" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        return
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        text = f.read()
    first = text.lstrip("\ufeff").splitlines()[0].strip() if text.strip() else ""
    if first == BEHAVIOR_MARKER:
        _clear_notice(key)
        return
    if _content_hash16(text) in _KNOWN_BEHAVIOR_MD_HASHES:
        shutil.copy2(path, f"{path}.bak-n3mc-{_stamp()}")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        _clear_notice(key)
        return
    _notify(key, f"The rules file {path} has been edited by hand, so it was not updated. "
                 "Merge the new template manually if needed.")


_IMPORT_LINE_RE = re.compile(r"^[ \t]*@[^\n]*memory_context\.md[ \t]*$")
_N3MC_COMMENT_PREFIXES = ("# N3MemoryCore", "# Path resolves to", "# If you have not yet run")


def _norm_import_path(p):
    return os.path.normcase(os.path.normpath(p.replace("\\", "/")))


def _retire_memory_context(project):
    context_file = _mem("memory_context.md")
    claude_md = claude_paths(project)["claude_md"]
    if os.path.exists(claude_md):
        with open(claude_md, "rb") as f:
            data = f.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            _err(f"N3MC: {claude_md} is not UTF-8 text; it was left unchanged")
            text = None
        if text is not None:
            bom = text.startswith("\ufeff")
            if bom:
                text = text[1:]
            nl = "\r\n" if "\r\n" in text else "\n"
            lines = text.split(nl)
            target = _norm_import_path(context_file)
            remove = set()
            for i, line in enumerate(lines):
                if not _IMPORT_LINE_RE.match(line.rstrip("\r")):
                    continue
                p = os.path.expanduser(line.strip()[1:].strip())
                if not os.path.isabs(p.replace("\\", "/")) and not os.path.isabs(p):
                    continue
                if _norm_import_path(p) != target:
                    continue
                remove.add(i)
                j = i - 1
                while j >= 0 and lines[j].strip() == "":
                    j -= 1
                k = j
                while k >= 0 and lines[k].startswith(_N3MC_COMMENT_PREFIXES):
                    k -= 1
                if k < j:  # a comment block precedes the import: remove it and the blank lines
                    remove.update(range(k + 1, i))
            if remove:
                shutil.copy2(claude_md, f"{claude_md}.bak-n3mc-{_stamp()}")
                kept = [l for i, l in enumerate(lines) if i not in remove]
                out = ("\ufeff" if bom else "") + nl.join(kept)
                with open(claude_md, "wb") as f:
                    f.write(out.encode("utf-8"))
    marker = _mem(".memory_context_retired")
    if not os.path.exists(marker) and os.path.exists(context_file):
        os.remove(context_file)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(_now_ts())


def _housekeeping():
    now = time.time()
    try:
        names = os.listdir(MEMORY_DIR)
    except OSError:
        names = []
    for name in names:
        if name.startswith("turn_id_") and name.endswith(".txt"):
            p = os.path.join(MEMORY_DIR, name)
            try:
                if now - os.path.getmtime(p) > TURN_FILE_MAX_AGE:
                    os.remove(p)
            except OSError:
                pass
    try:
        if os.path.getsize(AUDIT_LOG) > AUDIT_ROTATE_BYTES:
            os.replace(AUDIT_LOG, AUDIT_LOG + ".1")
    except OSError:
        pass


def _project_dir():
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    return os.path.abspath(os.getcwd())


def _is_home(path):
    return (os.path.normcase(os.path.realpath(path))
            == os.path.normcase(os.path.realpath(os.path.expanduser("~"))))


def cmd_stop() -> int:
    project = _project_dir()
    if not _is_home(project):
        try:
            _ensure_behavior_md(project)
        except Exception as e:  # noqa: BLE001
            _err(f"N3MC: the rules file could not be prepared ({type(e).__name__}: {e})")
        try:
            _retire_memory_context(project)
        except Exception as e:  # noqa: BLE001
            _err(f"N3MC: memory_context clean-up failed ({type(e).__name__}: {e})")
    try:
        _housekeeping()
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: housekeeping failed ({type(e).__name__}: {e})")
    return 0


# ---------------------------------------------------------------------------
# --init: hooks and permission rules in ~/.claude/settings.json
# ---------------------------------------------------------------------------

PERMISSION_RULES = [
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
]

OURS = {  # event -> (entry point, module)
    "UserPromptSubmit": ("n3mc-hook", "n3memorycore.n3mc_hook"),
    "Stop": ("n3mc-stop-hook", "n3memorycore.n3mc_stop_hook"),
}


def _settings_path():
    return pathlib.Path(os.path.expanduser(os.path.join("~", ".claude", "settings.json")))


def _pkg_dir():
    return os.path.normcase(os.path.abspath(os.path.dirname(n3memorycore.__file__)))


def _tokens(command: str) -> list:
    # Replace "\" with "/" first: posix shlex drops an unquoted "\" as an escape, so
    # C:\Tools\n3mc-hook.exe would become C:Toolsn3mc-hook.exe. Tokens are compared, never run.
    command = command.replace("\\", "/")
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def is_own_hook(command: str, event: str) -> bool:
    """True only for a command that runs this package's hook."""
    exe_name, module = OURS[event]
    toks = _tokens(command)
    if not toks:
        return False
    first = os.path.basename(toks[0]).lower()
    if first.endswith(".exe"):
        first = first[:-4]
    if first == exe_name:
        return True
    for i, t in enumerate(toks[:-1]):
        if t == "-m" and toks[i + 1] == module:
            return True
    script = module.split(".")[-1] + ".py"
    for t in toks[1:]:
        if os.path.basename(t).lower() == script:
            d = os.path.normcase(os.path.abspath(os.path.dirname(os.path.expanduser(t))))
            if d == _pkg_dir():
                return True
    return False


def shell_path(p) -> str:
    s = pathlib.Path(p).as_posix()
    return f'"{s}"' if " " in s else s


def _scripts_folders():
    folders = [sysconfig.get_path("scripts")]
    try:
        folders.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
    except (KeyError, ValueError, AttributeError):
        pass
    return folders


def resolve_cmd(script_name: str) -> str:
    for folder in _scripts_folders():
        if not folder:
            continue
        for name in (script_name + ".exe", script_name):
            cand = pathlib.Path(folder) / name
            if cand.is_file():
                return shell_path(cand)
    exe = shutil.which(script_name)
    if exe:
        return shell_path(exe)
    module = {"n3mc-hook": "n3memorycore.n3mc_hook", "n3mc-stop-hook": "n3memorycore.n3mc_stop_hook"}[script_name]
    return f"{shell_path(sys.executable)} -m {module}"


def register_hooks() -> bool:
    """Merge the two hook entries and the permission rules into ~/.claude/settings.json.
    Returns True when the file was written. Exits with status 1 for a file it must not touch."""
    settings_file = _settings_path()
    if settings_file.exists():
        try:
            raw = settings_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            sys.exit(f"{settings_file} is not UTF-8 text. Nothing was changed. "
                     "Save it as UTF-8, then run n3mc --init again.")
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit(f"{settings_file} is not valid JSON ({e}). Nothing was changed. "
                     "Fix the file, then run n3mc --init again.")
        perms = settings.get("permissions", {}) if isinstance(settings, dict) else None
        if (not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict)
                or not isinstance(perms, dict) or not isinstance(perms.get("allow", []), list)):
            sys.exit(f"{settings_file} has an unexpected structure. Nothing was changed.")
        before = json.loads(raw)
    else:
        raw, settings, before = None, {}, None

    hooks = settings.setdefault("hooks", {})
    for event, (exe_name, _module) in OURS.items():
        kept = []
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            inner = [h for h in entry.get("hooks", [])
                     if not (isinstance(h, dict) and isinstance(h.get("command"), str)
                             and is_own_hook(h["command"], event))]
            for h in inner:
                c = h.get("command") if isinstance(h, dict) else None
                if isinstance(c, str) and any(m in c for m in ("n3mc_hook", "n3mc-hook", "n3mc_stop_hook",
                                                               "n3mc-stop-hook")):
                    print(f"Another N3MC hook is registered for {event} and was left in place: {c}")
            if inner:
                kept.append({**entry, "hooks": inner})
        kept.append({"hooks": [{"type": "command", "command": resolve_cmd(exe_name),
                                "timeout": HOOK_TIMEOUT_SECONDS}]})
        hooks[event] = kept

    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    for rule in PERMISSION_RULES:
        if rule not in allow:
            allow.append(rule)

    if settings != before:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            shutil.copy2(settings_file, settings_file.with_name(f"settings.json.bak-n3mc-{_stamp()}"))
        fd, tmp = tempfile.mkstemp(dir=settings_file.parent, prefix=".settings.json.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, settings_file)
        return True
    return False


def cmd_init() -> int:
    _ensure_dirs()
    cfg = _load_config()
    try:
        conn = _connect_checked(None)
        conn.close()
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: the DB could not be created ({type(e).__name__}: {e})")
    register_hooks()
    print(f"N3MemoryCore data folder: {HOME_DIR} (server_port {cfg['server_port']})")
    print("Saving will start from the next session.")
    return 0


# ---------------------------------------------------------------------------
# Reading commands (the DB directly, no server)
# ---------------------------------------------------------------------------

def _list_line(r):
    snippet = re.sub(r"\r\n|\r|\n", " ", (r["content"] or "")[:80]).replace("\t", " ")
    return f"{r['id']}\t{r['timestamp']}\t{r['agent_name'] or '-'}\t{snippet}"


def _open_existing_db():
    if not os.path.exists(DB_PATH):
        _err(f"No DB found at {DB_PATH}")
        return None
    return _connect_checked(None)


def cmd_list(since=None, until=None, fmt="tsv") -> int:
    conn = _open_existing_db()
    if conn is None:
        return 1
    try:
        rows = get_all_memories(conn, since, until)
    finally:
        conn.close()
    for r in rows:
        if fmt == "jsonl":
            print(json.dumps({"id": r["id"], "timestamp": r["timestamp"], "agent_name": r["agent_name"],
                              "content": r["content"]}, ensure_ascii=False))
        else:
            print(_list_line(r))
    if fmt != "jsonl":
        print(f"Total: {len(rows)} records")
    return 0


def cmd_recall_thread(turn_id, before=2, after=2, fmt="tsv") -> int:
    conn = _open_existing_db()
    if conn is None:
        return 1
    try:
        lo_hi = conn.execute("SELECT MIN(rowid), MAX(rowid) FROM memories WHERE turn_id = ?", (turn_id,)).fetchone()
        rows = []
        if lo_hi and lo_hi[0] is not None:
            lo, hi = lo_hi
            prev = [r[0] for r in conn.execute(
                "SELECT turn_id FROM memories WHERE turn_id IS NOT NULL AND turn_id != ? GROUP BY turn_id "
                "HAVING MAX(rowid) < ? ORDER BY MAX(rowid) DESC LIMIT ?", (turn_id, lo, max(0, before))).fetchall()]
            nxt = [r[0] for r in conn.execute(
                "SELECT turn_id FROM memories WHERE turn_id IS NOT NULL AND turn_id != ? GROUP BY turn_id "
                "HAVING MIN(rowid) > ? ORDER BY MIN(rowid) ASC LIMIT ?", (turn_id, hi, max(0, after))).fetchall()]
            turns = prev + [turn_id] + nxt
            marks = ",".join("?" * len(turns))
            rows = conn.execute(f"SELECT id, timestamp, agent_name, turn_id, content FROM memories "
                                f"WHERE turn_id IN ({marks}) ORDER BY rowid", turns).fetchall()
    finally:
        conn.close()
    for r in rows:
        rec = {"id": r[0], "timestamp": r[1], "agent_name": r[2], "turn_id": r[3], "content": r[4]}
        if fmt == "jsonl":
            print(json.dumps(rec, ensure_ascii=False))
        else:
            print(_list_line(rec))
    if fmt != "jsonl":
        print(f"Total: {len(rows)} records")
    return 0


def cmd_show_turn(turn_id) -> int:
    conn = _open_existing_db()
    if conn is None:
        return 1
    try:
        rows = get_memories_by_turn_id(conn, turn_id)
    finally:
        conn.close()
    for r in rows:
        print(f"--- {r['id']} saved={_fmt_saved(r['timestamp'])} ---")
        print(r["content"])
    print(f"Total: {len(rows)} records")
    return 0


# ---------------------------------------------------------------------------
# Server-using commands
# ---------------------------------------------------------------------------

def cmd_buffer(text, agent_name=None) -> int:
    if text == "-":
        text = sys.stdin.read()
    text = sanitize_surrogates(text or "")
    cfg = _load_config()
    if not purify_text(text).strip():
        return 0
    server_ok = ensure_server(cfg)
    session_id = os.environ.get("N3MC_SESSION_ID") or None
    status = _save_record(text, cfg, server_ok, agent_name=agent_name, session_id=session_id, turn_id=None)
    return 1 if status == "error" else 0


def cmd_search(query, since=None, until=None) -> int:
    try:
        s, u = _parse_bounds(since, until)
    except ValueError as e:
        _err(str(e))
        return 2
    cfg = _load_config()
    q = sanitize_surrogates(query or "")[: int(cfg["search_query_max_chars"])]
    if not q.strip():
        sys.stdout.write(render_placeholder(NO_RESULTS, _take_notices()))
        return 0
    server_ok = ensure_server(cfg)
    out = _search_output(q, cfg, server_ok, session_id=os.environ.get("N3MC_SESSION_ID") or None,
                         since=s, until=u)
    sys.stdout.write(out)
    return 0


def cmd_repair() -> int:
    cfg = _load_config()
    server_ok = ensure_server(cfg)
    if _FOREIGN_SERVER_ROOT is not None:
        _err(f"N3MC: --repair skipped: another N3MC folder's server is using port {_current_port(cfg)}")
        return 0
    if not server_ok:
        _err(f"N3MC: --repair skipped: {_NO_SERVER_REASON or 'the memory server is not running'}")
        return 1
    try:
        r = _post("/repair", {}, timeout=None)
    except Exception as e:  # noqa: BLE001
        _err(f"N3MC: --repair failed ({type(e).__name__}: {e})")
        return 1
    n = r.get("count", 0) if isinstance(r, dict) else 0
    if n:
        print(f"Repaired {n} record(s)")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _build_parser():
    p = argparse.ArgumentParser(prog="n3mc", description="N3MemoryCore Free: persistent memory for Claude Code")
    p.add_argument("--init", action="store_true", help="create the data folder and register the hooks")
    p.add_argument("--buffer", metavar="TEXT", help="save TEXT as one record ('-' reads stdin)")
    p.add_argument("--agent-id", dest="agent_id", metavar="NAME", help="agent_name of a --buffer record")
    p.add_argument("--search", metavar="QUERY", help="search the memory")
    p.add_argument("--since", metavar="WHEN")
    p.add_argument("--until", metavar="WHEN")
    p.add_argument("--list", action="store_true", help="list the records")
    p.add_argument("--format", choices=["tsv", "jsonl"], default="tsv")
    p.add_argument("--recall-thread", dest="recall_thread", metavar="TURN_ID")
    p.add_argument("--before", type=int, default=2)
    p.add_argument("--after", type=int, default=2)
    p.add_argument("--show-turn", dest="show_turn", metavar="TURN_ID")
    p.add_argument("--stop", action="store_true", help="session clean-up (does not stop the server)")
    p.add_argument("--repair", action="store_true")
    p.add_argument("--reembed", action="store_true")
    p.add_argument("--hook-submit", dest="hook_submit", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--save-claude-turn", dest="save_claude_turn", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--run-server", dest="run_server", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    for _stream_name in ("stdin", "stdout", "stderr"):
        _s = getattr(sys, _stream_name, None)
        if _s is not None and hasattr(_s, 'reconfigure'):
            try:
                _s.reconfigure(encoding='utf-8')
            except Exception:
                pass
    args = _build_parser().parse_args(argv)
    if args.run_server:
        return run_server()
    if args.hook_submit:
        return cmd_hook_submit()
    if args.save_claude_turn:
        return cmd_save_claude_turn()
    if args.init:
        return cmd_init()
    if args.stop:
        return cmd_stop()
    if args.reembed:
        return cmd_reembed()
    if args.repair:
        return cmd_repair()
    if args.buffer is not None:
        return cmd_buffer(args.buffer, args.agent_id)
    if args.search is not None:
        return cmd_search(args.search, args.since, args.until)
    if args.list:
        try:
            s, u = _parse_bounds(args.since, args.until)
        except ValueError as e:
            _err(str(e))
            return 2
        return cmd_list(s, u, args.format)
    if args.recall_thread is not None:
        return cmd_recall_thread(args.recall_thread, args.before, args.after, args.format)
    if args.show_turn is not None:
        return cmd_show_turn(args.show_turn)
    _build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
