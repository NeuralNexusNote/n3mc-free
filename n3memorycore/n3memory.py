"""N3MemoryCore Free: main CLI module plus the resident FastAPI server.

Commands: --init --buffer --search --list --recall-thread --show-turn --stop --repair
--reembed --hook-submit --save-claude-turn --run-server
"""
import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from . import __version__
from .paths import (
    AUDIT_LOG, CONFIG_FILE, CONTEXT_FILE, CONTEXT_RETIRED, DB_PATH, HOME_DIR, MEMORY_DIR,
    NOTICES_FILE, PID_FILE, SERVER_LOG, SERVER_START_FAILED, TURN_ID_FILE, VEC_MODEL_FILE,
    claude_paths,
)
from .core.database import (
    EMBED_DIM, bigram_text, check_exact_duplicate, count_memories, delete_meta,
    find_unindexed_memories, get_all_memories, get_connection, get_memories_by_turn_id,
    get_meta, init_db, insert_memory_once, serialize_vector, set_meta, strip_fts_punctuation,
)
from .core.processor import (
    _fmt_saved, add_chunk_prefixes, chunk_text, embed_passage, get_model, hybrid_search,
    purify_text, render_memory_context, sanitize_surrogates,
)

try:
    from uuid_extensions import uuid7 as _uuid7
except ImportError:  # pragma: no cover
    _uuid7 = None


def _new_id():
    return str(_uuid7()) if _uuid7 else str(uuid.uuid4())


# ===================================================================== constants

SESSION_ID = os.environ.get("N3MC_SESSION_ID") or str(uuid.uuid4())

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
_INT_RANGES = {
    "server_port": (1, 65535), "search_result_limit": (1, 1365), "pair_top_n": (1, None),
    "pair_max_chars": (1, None), "search_query_max_chars": (1, None),
    "version_min_chars": (1, None), "min_results": (0, None),
}
_NUM_RULES = {  # key -> (low, high, low_inclusive)
    "half_life_days": (0, None, False), "bm25_min_threshold": (0, None, True),
    "min_score": (0, 1, True), "vector_min_cos": (0, 1, True),
    "relative_min_score_ratio": (0, 1, True), "version_sim_threshold": (0, 1, True),
    "reply_sim_alpha": (0, 1, True), "reply_sim_floor": (0, 1, True),
    "reply_sim_span": (0, None, False),
}

AUDIT_MAX_BYTES = 50 * 1024 * 1024
_SYSTEM_PROMPT_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")
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

_PORT = DEFAULT_CONFIG["server_port"]
_FOREIGN_SERVER_ROOT = None
_SERVER_STARTING = False
_QUICK_CHECK_DONE = False
_SERVER_CODE_VERSION = None
_SERVER_PORT = None  # set by run_server: the port this server process listens on
_EPHEMERAL_IDS = {}
_SAVE_FAILED_CLEARED = False


def _utf8():
    for name in ("stdin", "stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is not None and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:
                pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _err(msg):
    print(msg, file=sys.stderr)


# ============================================================ atomic JSON + audit

def _write_json_atomic(path, obj):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _audit(record):
    """Append one JSONL line to audit.log. Never raises."""
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        try:
            line = json.dumps(record, ensure_ascii=False)
            data = (line + "\n").encode("utf-8")
        except (UnicodeEncodeError, ValueError):
            data = (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
        with open(AUDIT_LOG, "ab") as f:
            f.write(data)
    except Exception as e:
        _err(f"N3MC: audit.log write failed: {e}")


# ====================================================================== notices

def _read_notices():
    try:
        with open(NOTICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _notify(key, message):
    """Raise a notice for a human. Printed to stderr, kept in notices.json, shown once a day."""
    try:
        notices = _read_notices()
        cur = notices.get(key)
        if isinstance(cur, dict) and cur.get("message") == message:
            return
        notices[key] = {"message": message, "last_shown": None}
        _write_json_atomic(NOTICES_FILE, notices)
        _err(f"N3MC notice: {message}")
        _audit({"ts": _now_iso(), "hook": "notice", "key": key, "message": message})
    except Exception as e:
        _err(f"N3MC: could not record notice {key}: {e}")


def _clear_notice(key):
    try:
        notices = _read_notices()
        if key in notices:
            del notices[key]
            _write_json_atomic(NOTICES_FILE, notices)
    except Exception:
        pass


def _take_notices():
    """Messages due for display (first time, or last shown >= 24 h ago). Marks them shown."""
    out = []
    try:
        notices = _read_notices()
        now = time.time()
        changed = False
        for key, v in notices.items():
            if not isinstance(v, dict) or "message" not in v:
                continue
            last = v.get("last_shown")
            if last is None or now - float(last) >= 86400:
                out.append(v["message"])
                v["last_shown"] = now
                changed = True
        if changed:
            _write_json_atomic(NOTICES_FILE, notices)
    except Exception:
        pass
    return out


# ======================================================================== config

def _config_value_ok(key, value):
    default = DEFAULT_CONFIG.get(key)
    if key not in DEFAULT_CONFIG or default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if key in _INT_RANGES:
        if not isinstance(value, int):
            return False
        lo, hi = _INT_RANGES[key]
        return (lo is None or value >= lo) and (hi is None or value <= hi)
    if key in _NUM_RULES:
        lo, hi, lo_incl = _NUM_RULES[key]
        if lo is not None and (value < lo if lo_incl else value <= lo):
            return False
        return hi is None or value <= hi
    return True


def _ids_from_db():
    """Most frequent owner_id / local_id already stored in the DB (or None, None)."""
    try:
        if not os.path.exists(DB_PATH):
            return None, None
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            owner = conn.execute("SELECT owner_id FROM memories GROUP BY owner_id "
                                 "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
            local = conn.execute("SELECT local_id FROM memories WHERE local_id IS NOT NULL "
                                 "GROUP BY local_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return (owner[0] if owner else None), (local[0] if local else None)
    except Exception:
        return None, None


def _read_config_file():
    """(dict, raw) or (None, raw) when unreadable. A just-created file is re-read for a moment."""
    raw = b""
    for attempt in range(21):
        try:
            with open(CONFIG_FILE, "rb") as f:
                raw = f.read()
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data, raw
        except FileNotFoundError:
            return None, raw
        except Exception:
            pass
        try:
            fresh = time.time() - os.path.getmtime(CONFIG_FILE) <= 2
        except OSError:
            fresh = False
        if not fresh or attempt == 20:
            break
        time.sleep(0.05)
    return None, raw


def load_config():
    """Read config.json (creating / completing it safely) and return the effective config."""
    os.makedirs(HOME_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        db_owner, db_local = _ids_from_db()
        data = dict(DEFAULT_CONFIG)
        data["owner_id"] = db_owner or str(uuid.uuid4())
        data["local_id"] = db_local or str(uuid.uuid4())
        try:
            with open(CONFIG_FILE, "x", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            return data
        except FileExistsError:
            pass  # another process created it first; read theirs
    data, _raw = _read_config_file()
    if data is None:
        _notify("config_unreadable",
                f"{CONFIG_FILE} could not be read as JSON, so it was left untouched and default "
                "settings are in use. Fix the file; this notice clears itself.")
        cfg = dict(DEFAULT_CONFIG)
        db_owner, db_local = _ids_from_db()
        if "owner_id" not in _EPHEMERAL_IDS:
            _EPHEMERAL_IDS["owner_id"] = db_owner or str(uuid.uuid4())
            _EPHEMERAL_IDS["local_id"] = db_local or str(uuid.uuid4())
        cfg["owner_id"], cfg["local_id"] = _EPHEMERAL_IDS["owner_id"], _EPHEMERAL_IDS["local_id"]
        return cfg
    _clear_notice("config_unreadable")
    cfg = dict(DEFAULT_CONFIG)
    bad = []
    for k, v in data.items():
        if k in DEFAULT_CONFIG and not _config_value_ok(k, v):
            bad.append(k)
            continue
        cfg[k] = v
    changed = False
    merged = dict(data)
    db_owner = db_local = None
    for idk in ("owner_id", "local_id"):
        if not isinstance(cfg.get(idk), str) or not cfg[idk]:
            if db_owner is None and db_local is None:
                db_owner, db_local = _ids_from_db()
            cfg[idk] = (db_owner if idk == "owner_id" else db_local) or str(uuid.uuid4())
            merged[idk] = cfg[idk]
            changed = True
    for k, v in DEFAULT_CONFIG.items():
        if k not in merged:
            merged[k] = cfg[k]
            changed = True
    if bad:
        _notify("config_type",
                "config.json has invalid value(s) for: " + ", ".join(sorted(bad)) +
                ". Those settings are ignored (defaults are used). Fix the file; "
                "this notice clears itself.")
    else:
        _clear_notice("config_type")
        if changed:
            try:
                _write_json_atomic(CONFIG_FILE, merged)
            except Exception as e:
                _err(f"N3MC: could not update config.json: {e}")
    return cfg


def _set_port(cfg):
    global _PORT
    _PORT = cfg["server_port"]


def _code_version():
    h = hashlib.sha256()
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "n3memory.py"), os.path.join(here, "core", "database.py"),
              os.path.join(here, "core", "processor.py")):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    try:
        with open(CONFIG_FILE, "rb") as f:
            raw = f.read()
    except OSError:
        raw = b""
    try:
        h.update(json.dumps(json.loads(raw.decode("utf-8")), sort_keys=True).encode("utf-8"))
    except Exception:
        h.update(raw)
    return h.hexdigest()[:16]


# ======================================================================= DB access

def _is_corruption_error(e):
    if not isinstance(e, sqlite3.DatabaseError) or isinstance(e, sqlite3.OperationalError):
        return False
    msg = str(e).lower()
    return "file is not a database" in msg or "malformed" in msg


def _quarantine_db():
    """Set a failed DB aside (with its -wal / -shm) and let a new empty one be created."""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    base = f"{DB_PATH}.corrupt-{stamp}"
    dest, n = base + ".bak", 1
    while os.path.exists(dest):
        n += 1
        dest = f"{base}-{n}.bak"
    try:
        shutil.move(DB_PATH, dest)
    except FileNotFoundError:
        return None
    for suffix in ("-wal", "-shm"):
        side = DB_PATH + suffix
        if os.path.exists(side):
            try:
                shutil.move(side, dest + suffix)
            except OSError as e:
                _err(f"N3MC: could not move {side}: {e}")
    _err(f"N3MC WARNING: the memory DB failed its integrity check and was set aside as {dest}. "
         f"A new empty DB is in use. To recover records: sqlite3 {dest} .recover | sqlite3 recovered.db")
    _notify("db_corrupt",
            f"The memory DB failed its integrity check and was set aside as {dest}. A new empty "
            f"DB is in use. To recover records: sqlite3 {dest} .recover | sqlite3 recovered.db")
    return dest


def _connect_checked(pragma=None):
    """Open DB_PATH and (optionally) run PRAGMA <pragma>; quarantine the file when it is corrupt."""
    for attempt in (1, 2):
        conn = None
        try:
            conn = get_connection(DB_PATH)
            if pragma:
                row = conn.execute(f"PRAGMA {pragma}").fetchone()
                if row is None or str(row[0]).lower() != "ok":
                    conn.close()
                    conn = None
                    _quarantine_db()
                    if attempt == 1:
                        continue
                    raise sqlite3.DatabaseError("integrity check failed after recovery")
            return conn
        except sqlite3.DatabaseError as e:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt == 1 and _is_corruption_error(e):
                _quarantine_db()
                continue
            raise
    raise sqlite3.DatabaseError("could not open DB")  # pragma: no cover


def _db():
    """A per-call connection with the schema in place."""
    conn = get_connection(DB_PATH)
    try:
        init_db(conn)
    except Exception:
        conn.close()
        raise
    return conn


# ===================================================================== HTTP client

def _http(method, path, body=None, timeout=30, port=None):
    url = f"http://127.0.0.1:{port or _PORT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    # No proxy: HTTP_PROXY must never capture traffic to our own loopback server.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path, body, timeout=30, port=None):
    return _http("POST", path, body, timeout, port)


def _get(path, timeout=30, port=None):
    return _http("GET", path, None, timeout, port)


def _request_shutdown(root, port=None):
    _post("/shutdown", {"root": root}, timeout=5, port=port)


def _health(port=None):
    try:
        h = _get("/health", timeout=5, port=port)
        return h if isinstance(h, dict) and h.get("status") == "ok" else None
    except Exception:
        return None


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _health_decision(health):
    if not health:
        return "none"
    if "root" not in health:
        return "legacy"
    if _norm(health["root"]) != _norm(HOME_DIR):
        return "foreign"
    if health.get("code_version") != _code_version():
        return "stale"
    return "ok"


# ================================================================ server lifecycle

def _cmd_is_server(cmd):
    return any("n3memorycore.n3memory" in c for c in cmd) and "--run-server" in cmd


def _process_home(proc):
    env = proc.environ()
    home = env.get("N3MC_HOME")
    return _norm(os.path.expanduser(home)) if home else _norm(os.path.join(os.path.expanduser("~"), ".n3mc"))


def _pid_is_own_server(pid):
    """True when ``pid`` is a live N3MC server for THIS data folder."""
    try:
        import psutil
    except ImportError:  # broken install: trust a PID file younger than 60 s
        try:
            return time.time() - os.path.getmtime(PID_FILE) < 60
        except OSError:
            return False
    try:
        proc = psutil.Process(pid)
        return _cmd_is_server(proc.cmdline()) and _process_home(proc) == _norm(HOME_DIR)
    except Exception:
        return False


def _is_own_server_process(health):
    pid = (health or {}).get("pid")
    if not pid:
        try:
            import psutil
            for c in psutil.net_connections(kind="tcp"):
                if c.status == "LISTEN" and c.laddr and c.laddr.port == _PORT:
                    pid = c.pid
                    break
        except Exception:
            return False
    return bool(pid) and _pid_is_own_server(int(pid))


def _terminate(pid):
    try:
        import psutil
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(5)
        except psutil.TimeoutExpired:
            p.kill()
        return True
    except Exception:
        return False


def _read_pid_file():
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _remove_pid_file_if(pid):
    if _read_pid_file() == pid:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


def _stop_stale_server(health, quiet=False):
    """Ask the server to stop; fall back to killing it only when verified to be ours."""
    stopped_pid = health.get("pid")
    try:
        _request_shutdown(HOME_DIR)
        asked = True
    except Exception:
        asked = False
    if not asked:
        if _is_own_server_process(health) and stopped_pid and _terminate(int(stopped_pid)):
            pass
        else:
            if not quiet:
                _notify("stale_server",
                        "An old N3MC server could not be stopped. Another N3MC folder may be using "
                        "the same server_port. Check server_port in each folder's config.json "
                        "and restart manually.")
            return False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _health() is None:
            break
        time.sleep(0.3)
    else:
        if not quiet:
            _notify("stale_server",
                    "An old N3MC server could not be stopped. Another N3MC folder may be using "
                    "the same server_port. Check server_port in each folder's config.json "
                    "and restart manually.")
        return False
    if stopped_pid:
        _remove_pid_file_if(int(stopped_pid))
    return True


def _clear_server_notices():
    for k in ("stale_server", "port_conflict", "server_start_failed"):
        _clear_notice(k)
    try:
        os.remove(SERVER_START_FAILED)
    except OSError:
        pass


def _start_server():
    """Launch the server detached and wait for /health. False when it did not come up."""
    global _SERVER_STARTING
    try:
        if time.time() - os.path.getmtime(SERVER_START_FAILED) < 300:
            return False
    except OSError:
        pass
    os.makedirs(MEMORY_DIR, exist_ok=True)
    try:
        if os.path.getsize(SERVER_LOG) > 1024 * 1024:
            open(SERVER_LOG, "wb").close()
    except OSError:
        pass
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        log = open(SERVER_LOG, "ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "n3memorycore.n3memory", "--run-server"],
            stderr=log, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs)
    except Exception as e:
        _err(f"N3MC: could not start the memory server: {e}")
        return False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _health() is not None:
            _clear_server_notices()
            return True
        rc = proc.poll()
        if rc not in (None, 0):
            _notify("server_start_failed",
                    f"The N3MC memory server could not start (exit code {rc}). "
                    f"See {SERVER_LOG} for the reason.")
            try:
                with open(SERVER_START_FAILED, "w", encoding="utf-8") as f:
                    f.write(_now_iso())
            except OSError:
                pass
            _remove_pid_file_if(proc.pid)
            return False
        time.sleep(0.5)
    _SERVER_STARTING = True
    return False


def _listen_ports(pid):
    """TCP ports the process ``pid`` is listening on (empty when psutil cannot tell)."""
    try:
        import psutil
        proc = psutil.Process(pid)
        # psutil 6.0 renamed Process.connections() to net_connections(); psutil>=5.9 is allowed.
        getter = getattr(proc, "net_connections", None) or proc.connections
        conns = getter(kind="tcp")
    except Exception:
        return []
    return sorted({c.laddr.port for c in conns if c.status == "LISTEN" and c.laddr})


def _stop_moved_server(pid):
    """The PID-file server listens on another port (server_port was changed): stop it there.

    It is stopped only through /shutdown, after that port's /health reports this data folder.
    True when it stopped (the PID file is then removed)."""
    for port in _listen_ports(pid):
        if port == _PORT:
            continue
        h = _health(port)
        if not h or "root" not in h or _norm(h["root"]) != _norm(HOME_DIR):
            continue
        try:
            _request_shutdown(HOME_DIR, port)
        except Exception:
            return False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _health(port) is None and not _pid_is_own_server(pid):
                _remove_pid_file_if(pid)
                return True
            time.sleep(0.3)
        return False
    return False


def _ensure_when_no_response():
    global _SERVER_STARTING
    pid = _read_pid_file() if os.path.exists(PID_FILE) else None
    if os.path.exists(PID_FILE):
        if pid and _pid_is_own_server(pid) and _stop_moved_server(pid):
            return _start_server()
        if pid and _pid_is_own_server(pid):
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if _health() is not None:
                    return ensure_server()
                time.sleep(0.5)
            _SERVER_STARTING = True
            return False
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    return _start_server()


def ensure_server():
    """True when a server for this data folder is usable; False -> work without one."""
    global _FOREIGN_SERVER_ROOT, _SERVER_STARTING
    _FOREIGN_SERVER_ROOT = None
    _SERVER_STARTING = False
    health = _health()
    if health is None:
        return _ensure_when_no_response()
    decision = _health_decision(health)
    if decision == "ok":
        _clear_server_notices()
        return True
    if decision == "stale":
        if _stop_stale_server(health):
            return _ensure_when_no_response()
        return True  # same DB, keep using the old server
    if decision == "legacy":
        if _is_own_server_process(health) and _stop_stale_server(health, quiet=True):
            return _ensure_when_no_response()
        _FOREIGN_SERVER_ROOT = "(unknown: the server did not report its folder)"
    else:
        _FOREIGN_SERVER_ROOT = health.get("root")
    _notify("port_conflict",
            f"Another program's server (data folder: {_FOREIGN_SERVER_ROOT}) is using port "
            f"{_PORT}. Memories are saved directly and search is unavailable. Give each N3MC "
            "folder its own server_port in config.json.")
    return False


# ================================================================ record handling

def _do_buffer(conn, cfg, content, agent_name=None, local_id=None, session_id=None,
               turn_id=None, embed=True):
    """Store one record. Shared by POST /buffer and the direct (server-less) path."""
    content = purify_text(content or "")
    if not content.strip():
        return {"status": "skipped", "count": 0, "reason": "empty"}
    if check_exact_duplicate(conn, content, turn_id=turn_id):
        return {"status": "skipped", "count": 0, "reason": "exact_duplicate"}
    vec = None
    if embed:
        try:
            vec = embed_passage(content, cfg["embed_model"])
            if len(vec) != EMBED_DIM:
                _err(f"N3MC: embedding has {len(vec)} dims, expected {EMBED_DIM}; stored without vector")
                vec = None
        except Exception as e:
            _err(f"N3MC: embedding failed ({e}); stored without vector (run n3mc --repair later)")
            vec = None
    before = count_memories(conn)
    rowid = insert_memory_once(
        conn, _new_id(), content, _now_iso(), cfg["owner_id"], local_id or cfg["local_id"],
        agent_name, session_id or SESSION_ID, vec, turn_id=turn_id)
    if rowid is None:
        return {"status": "skipped", "count": 0, "reason": "exact_duplicate"}
    if count_memories(conn) <= before:
        return {"status": "error", "message": "record count did not change after INSERT"}
    return {"status": "ok", "count": 1}


def _buffer_direct(cfg, content, agent_name=None, session_id=None, turn_id=None, local_id=None):
    """Save without the server (and without a vector; --repair adds it later)."""
    global _QUICK_CHECK_DONE
    try:
        pragma = None if _QUICK_CHECK_DONE else "quick_check"
        conn = _connect_checked(pragma)
        _QUICK_CHECK_DONE = True
        try:
            init_db(conn)
            result = _do_buffer(conn, cfg, content, agent_name, local_id, session_id, turn_id,
                                embed=False)
        finally:
            conn.close()
        if result.get("status") == "error":
            raise RuntimeError(result.get("message", "save failed"))
        return result
    except Exception as e:
        _err("⚠️ Physical save failed. Current memories may be lost.")
        _err("⚠️ 物理保存に失敗。現在の記憶は失われる可能性があります。")
        _err(f"N3MC: {type(e).__name__}: {e}")
        _notify("save_failed",
                f"A record could not be saved ({type(e).__name__}: {e}). Current memories may be "
                "lost; if it happens again, check that no other program holds the memory database.")
        return {"status": "error", "message": str(e)}


def _save_record(cfg, server_ok, content, agent_name=None, session_id=None, turn_id=None):
    global _SAVE_FAILED_CLEARED
    result = None
    if server_ok:
        try:
            r = _post("/buffer", {"content": content, "agent_name": agent_name,
                                  "session_id": session_id, "turn_id": turn_id})
            if isinstance(r, dict) and r.get("status") != "error":
                result = r
        except Exception as e:
            _err(f"N3MC: server save failed ({e}); saving directly")
    if result is None:
        result = _buffer_direct(cfg, content, agent_name, session_id, turn_id)
    if result.get("status") == "ok" and not _SAVE_FAILED_CLEARED:
        _SAVE_FAILED_CLEARED = True
        _clear_notice("save_failed")
    return result


def _save_chunks(cfg, server_ok, text, role, turn_id, session_id, agent_name="claude-code"):
    """Whole-message purify first, then chunk, then save every chunk in order."""
    text = purify_text(sanitize_surrogates(text or ""))
    chunks = chunk_text(text, max_chars=400, overlap=40)
    for rec in add_chunk_prefixes(chunks, role) if chunks else []:
        _save_record(cfg, server_ok, rec, agent_name, session_id, turn_id)


# ============================================================ repair / reembed

def _embed_failure_note(e):
    _err(f"N3MC: could not embed ({e})")


def _check_model_mismatch(conn, cfg):
    recorded = get_meta(conn, "embed_model")
    if recorded is None:
        has_vec = conn.execute("SELECT 1 FROM memories_vec LIMIT 1").fetchone() is not None
        recorded = cfg["embed_model"]
        if has_vec:
            try:
                with open(VEC_MODEL_FILE, "r", encoding="utf-8") as f:
                    legacy = f.read().strip()
                if legacy:
                    recorded = legacy
            except OSError:
                pass
        set_meta(conn, "embed_model", recorded)
        set_meta(conn, "embed_dim", EMBED_DIM)
        set_meta(conn, "embed_model_at", _now_iso())
    if recorded != cfg["embed_model"]:
        total = count_memories(conn)
        _notify("embed_model_mismatch",
                f"The stored vectors were made with {recorded}, but config.json sets embed_model "
                f"to {cfg['embed_model']}. Search quality is degraded until they are re-made: run "
                f"n3mc --reembed (it re-embeds all {total} records with {cfg['embed_model']}), "
                f"or set embed_model back to {recorded}.")
    else:
        _clear_notice("embed_model_mismatch")


def repair_records(cfg, max_records=None):
    """Fill missing vec / FTS / bigram rows. Returns the number of records actually changed."""
    conn = _db()
    changed = 0
    try:
        # (1) one-time FTS punctuation clean-up (keyset batches; marker only when finished)
        if get_meta(conn, "fts_punct_cleaned") is None:
            last, done = 0, True
            while True:
                rows = conn.execute("SELECT rowid, content FROM memories_fts WHERE rowid > ? "
                                    "ORDER BY rowid LIMIT 200", (last,)).fetchall()
                if not rows:
                    break
                for r in rows:
                    last = r[0]
                    clean = strip_fts_punctuation(r[1])
                    if clean != r[1]:
                        conn.execute("UPDATE memories_fts SET content = ? WHERE rowid = ?",
                                     (clean, r[0]))
                        changed += 1
                conn.commit()
                if max_records is not None and changed >= max_records:
                    done = False
                    break
            if done:
                set_meta(conn, "fts_punct_cleaned", 1)
        # (2) one-time bigram index for rows saved before the table existed
        if get_meta(conn, "fts_bigram") is None:
            while True:
                rows = conn.execute(
                    "SELECT m.rowid, m.content FROM memories m LEFT JOIN memories_fts_bi b "
                    "ON b.rowid = m.rowid WHERE b.rowid IS NULL ORDER BY m.rowid LIMIT 200"
                ).fetchall()
                if not rows:
                    break
                for r in rows:
                    conn.execute("INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
                                 (r[0], bigram_text(r[1])))
                    changed += 1
                conn.commit()
            set_meta(conn, "fts_bigram", 1)
        # (3) rows missing from memories_vec or memories_fts. OFFSET stays 0: repaired rows drop out.
        while max_records is None or changed < max_records:
            batch = 200 if max_records is None else min(200, max_records - changed)
            rows = find_unindexed_memories(conn, batch)
            if not rows:
                break
            vectors = {}
            for r in rows:  # embeddings first, outside any write transaction
                if not r["has_vec"]:
                    try:
                        vectors[r["rowid"]] = embed_passage(r["content"], cfg["embed_model"])
                    except Exception as e:
                        _embed_failure_note(e)
                        break
            repaired = 0
            for r in rows:
                did = False
                if not r["has_fts"]:
                    conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                                 (r["rowid"], strip_fts_punctuation(r["content"])))
                    if conn.execute("SELECT 1 FROM memories_fts_bi WHERE rowid = ?",
                                    (r["rowid"],)).fetchone() is None:
                        conn.execute("INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
                                     (r["rowid"], bigram_text(r["content"])))
                    did = True
                if not r["has_vec"] and r["rowid"] in vectors and len(vectors[r["rowid"]]) == EMBED_DIM:
                    conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                 (r["rowid"], serialize_vector(vectors[r["rowid"]])))
                    did = True
                repaired += did
            conn.commit()
            changed += repaired
            if repaired == 0:
                break
        _check_model_mismatch(conn, cfg)
    finally:
        conn.close()
    return changed


def cmd_reembed():
    cfg = load_config()
    _set_port(cfg)
    if not os.path.exists(DB_PATH):
        _err(f"No DB found at {DB_PATH}")
        return 1
    health = _health()
    if health is not None and "root" in health and _norm(health["root"]) == _norm(HOME_DIR):
        try:
            _request_shutdown(HOME_DIR)
            for _ in range(50):
                if _health() is None:
                    break
                time.sleep(0.3)
        except Exception:
            pass
    model = cfg["embed_model"]
    try:
        probe = embed_passage("N3MC dimension probe", model)
    except Exception as e:
        _err(f"N3MC: --reembed: could not load {model}: {e}")
        return 1
    conn = _db()
    try:
        dim = int(get_meta(conn, "embed_dim", EMBED_DIM))
        if len(probe) != dim:
            _err(f"N3MC: --reembed: {model} makes {len(probe)}-dimensional vectors but memories_vec "
                 f"has {dim}; a different dimension needs the manual procedure in §3 \"Embeddings\".")
            return 1
        cursor = 0
        if get_meta(conn, "reembed_model") == model:
            cursor = int(get_meta(conn, "reembed_cursor", 0))
        else:
            set_meta(conn, "reembed_model", model)
            set_meta(conn, "reembed_cursor", 0)
        total = count_memories(conn)
        done = conn.execute("SELECT COUNT(*) FROM memories WHERE rowid <= ?", (cursor,)).fetchone()[0]
        while True:
            rows = conn.execute("SELECT rowid, content FROM memories WHERE rowid > ? "
                                "ORDER BY rowid LIMIT 200", (cursor,)).fetchall()
            if not rows:
                break
            vecs = [(r[0], embed_passage(r[1], model)) for r in rows]  # before the write txn
            conn.execute("BEGIN IMMEDIATE")
            try:
                for rowid, vec in vecs:
                    conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid,))
                    conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                 (rowid, serialize_vector(vec)))
                cursor = rows[-1][0]
                conn.execute("INSERT INTO meta(key, value) VALUES ('reembed_cursor', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(cursor),))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            done += len(rows)
            _err(f"Re-embedded {done} / {total}")
        set_meta(conn, "embed_model", model)
        set_meta(conn, "embed_dim", dim)
        set_meta(conn, "embed_model_at", _now_iso())
        delete_meta(conn, "reembed_model", "reembed_cursor")
        _clear_notice("embed_model_mismatch")
        print(f"Re-embedded {total} record(s) with {model}.")
        return 0
    finally:
        conn.close()


# ===================================================================== FastAPI app

def _parse_time_bound(value, is_until=False):
    """CLI/API time bound -> UTC ISO string in the stored timestamp format. ValueError if unreadable."""
    s = (value or "").strip()
    if not s:
        raise ValueError("empty")
    date_only = re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) is not None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(value)
    if date_only:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999) if is_until else dt
    return dt.astimezone(timezone.utc).isoformat()  # naive values are read as local time


def _time_range(since, until):
    s = _parse_time_bound(since, False) if since else None
    u = _parse_time_bound(until, True) if until else None
    if s and u and s > u:
        raise ValueError("--since is later than --until")
    return s, u


def create_app():
    from contextlib import asynccontextmanager
    from typing import Optional

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    class BufferReq(BaseModel):
        content: str
        agent_name: Optional[str] = None
        local_id: Optional[str] = None
        session_id: Optional[str] = None
        turn_id: Optional[str] = None

    class SearchReq(BaseModel):
        query: str
        session_id: Optional[str] = None
        since: Optional[str] = None
        until: Optional[str] = None

    class RepairReq(BaseModel):
        max_records: Optional[int] = None

    class ShutdownReq(BaseModel):
        root: str

    @asynccontextmanager
    async def lifespan(app):
        conn = _connect_checked("integrity_check")
        try:
            init_db(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(lifespan=lifespan, title="N3MemoryCore Free")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # The port this server listens on, not the one config.json says now:
        # after server_port changes, the old server must still answer /health
        # and /shutdown on its own port so the CLI can stop it.
        port = _SERVER_PORT or load_config()["server_port"]
        if "origin" in request.headers:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if request.headers.get("host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok", "root": HOME_DIR,
                "code_version": _SERVER_CODE_VERSION or _code_version(), "pid": os.getpid()}

    @app.post("/buffer")
    def buffer(req: BufferReq):
        cfg = load_config()
        conn = _db()
        try:
            return _do_buffer(conn, cfg, sanitize_surrogates(req.content), req.agent_name,
                              req.local_id, req.session_id, req.turn_id)
        finally:
            conn.close()

    @app.post("/search")
    def search(req: SearchReq):
        cfg = load_config()
        try:
            since, until = _time_range(req.since, req.until)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"cannot read time bound: {e}")
        conn = _db()
        try:
            res = hybrid_search(conn, sanitize_surrogates(req.query), cfg, req.session_id,
                                since, until)
        finally:
            conn.close()
        keep = ("id", "content", "score", "timestamp", "turn_id", "weak", "agent_name",
                "session_id", "filter_score", "cos_sim", "keyword_relevance")
        rows = []
        for r in res["results"]:
            row = {k: r.get(k) for k in keep}
            row["score"] = round(r["score"], 4)
            rows.append(row)
        return {"results": rows, "pairs": res["pairs"], "pairs_omitted": res["pairs_omitted"]}

    @app.post("/repair")
    def repair(req: Optional[RepairReq] = None):
        cfg = load_config()
        n = repair_records(cfg, req.max_records if req else None)
        return {"status": "ok", "count": n}

    @app.get("/list")
    def list_records():
        conn = _db()
        try:
            rows = get_all_memories(conn)
        finally:
            conn.close()
        recs = [{"id": r["id"], "content": r["content"], "timestamp": r["timestamp"],
                 "agent_name": r["agent_name"]} for r in rows]
        return {"records": recs, "total": len(recs)}

    @app.post("/shutdown")
    def shutdown(req: ShutdownReq):
        if _norm(req.root) != _norm(HOME_DIR):
            raise HTTPException(status_code=403, detail="root mismatch")

        def _exit():
            _remove_pid_file_if(os.getpid())
            os._exit(0)

        t = threading.Timer(0.3, _exit)
        t.daemon = True
        t.start()
        return {"status": "ok"}

    @app.delete("/delete/{memory_id}")
    def delete_pro(memory_id: str):
        raise HTTPException(status_code=403, detail="Pro feature")

    @app.post("/gc")
    def gc_pro():
        raise HTTPException(status_code=403, detail="Pro feature")

    @app.post("/import")
    def import_pro():
        raise HTTPException(status_code=403, detail="Pro feature")

    return app


def __getattr__(name):  # module-level ``app`` is built on first use
    if name == "app":
        global _APP
        try:
            return _APP
        except NameError:
            _APP = create_app()
            return _APP
    raise AttributeError(name)


def _claim_pid_file():
    os.makedirs(MEMORY_DIR, exist_ok=True)
    for _ in range(2):
        try:
            with open(PID_FILE, "x", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            pid = _read_pid_file()
            if pid and pid != os.getpid() and _pid_is_own_server(pid):
                return False
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
    return False


def run_server():
    global _SERVER_CODE_VERSION, _SERVER_PORT
    cfg = load_config()
    _SERVER_PORT = cfg["server_port"]
    if cfg["bind_host"] != "127.0.0.1":
        _err(f"N3MC WARNING: bind_host is {cfg['bind_host']}. The server has no authentication; "
             "anyone who can reach this address can read your memories. Use 127.0.0.1.")
    if not _claim_pid_file():
        return 0  # another server for this folder already runs (or is starting)
    atexit.register(lambda: _remove_pid_file_if(os.getpid()))
    try:
        _SERVER_CODE_VERSION = _code_version()
        # No check here: lifespan runs the one full integrity_check at startup.
        conn = _connect_checked(None)
        try:
            init_db(conn)
        finally:
            conn.close()
        try:  # preload before binding the port (first run may download ~470 MB)
            get_model(cfg["embed_model"])
        except Exception as e:
            _err(f"N3MC: embedding model not loaded ({e}); saves will be stored without vectors")
        import uvicorn
        uvicorn.run(create_app(), host=cfg["bind_host"], port=cfg["server_port"],
                    log_level="error")
    finally:
        _remove_pid_file_if(os.getpid())
    return 0


# ============================================================ hook helpers / input

_IMAGE_RE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")


def _strip_images(text):
    return _IMAGE_RE.sub("", text or "")


def _extract_text(prompt):
    """Text of a prompt. Multimodal arrays keep only their text parts; plain text stays as typed."""
    if prompt is None:
        return ""
    if isinstance(prompt, list):
        parts = prompt
    elif isinstance(prompt, str):
        s = prompt.strip()
        parts = None
        if s.startswith("["):
            try:
                data = json.loads(s)
            except ValueError:
                data = None
            if (isinstance(data, list) and data and
                    all(isinstance(p, dict) and isinstance(p.get("type"), str) for p in data)):
                parts = data
        if parts is None:
            return _strip_images(prompt)
    else:
        return ""
    texts = [p.get("text", "") for p in parts
             if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)]
    return _strip_images("\n".join(t for t in texts if t))


def _session_slug(session_id):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(session_id))[:64]


def _turn_file(session_id):
    if session_id:
        return os.path.join(MEMORY_DIR, f"turn_id_{_session_slug(session_id)}.txt")
    return TURN_ID_FILE


def _read_turn_id(session_id):
    try:
        with open(_turn_file(session_id), "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_turn_id(session_id, turn_id):
    path = _turn_file(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(turn_id)
    except OSError as e:
        _err(f"N3MC: could not write turn id: {e}")


def _message_text(content):
    """(has_text_part, text) of a transcript message content."""
    if isinstance(content, str):
        return True, content
    if isinstance(content, list):
        texts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)]
        if any(isinstance(p, dict) and p.get("type") == "text" for p in content):
            return True, "\n".join(texts)
    return False, ""


def _is_real_prompt(obj):
    if obj.get("isMeta"):
        return False
    has, text = _message_text((obj.get("message") or {}).get("content"))
    if not has:
        return False
    t = text.lstrip()
    return not t.startswith(("[Request interrupted by user", "<command-", "<local-command-")
                            + _SYSTEM_PROMPT_PREFIXES)


def _read_previous_reply(transcript_path, current_prompt):
    """Last assistant text block before the current prompt, from the session transcript."""
    if not transcript_path:
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            start = max(0, size - 5 * 1024 * 1024)
            f.seek(start)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.split("\n")
        if start > 0:
            lines = lines[1:]
        first_seen = False
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("isSidechain") is True:
                continue
            typ = obj.get("type")
            if typ not in ("user", "assistant") or obj.get("isMeta"):
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if not first_seen:
                first_seen = True
                if typ == "user" and _is_real_prompt(obj):
                    if _message_text(msg.get("content"))[1].strip() == (current_prompt or "").strip():
                        continue
            if typ == "user":
                if _is_real_prompt(obj):
                    return None
                continue
            content = msg.get("content")
            blocks = [p.get("text", "") for p in content
                      if isinstance(p, dict) and p.get("type") == "text"] if isinstance(content, list) \
                else ([content] if isinstance(content, str) else [])
            blocks = [b for b in blocks if isinstance(b, str) and b.strip()]
            if blocks:
                return blocks[-1]
        return None
    except Exception as e:
        _audit({"ts": _now_iso(), "hook": "UserPromptSubmit", "stage": "transcript_unreadable",
                "reason": str(e)[:200]})
        return None


# ===================================================================== commands

def _placeholder(text, notices):
    lines = ["# Recalled Memory Context", ""]
    for n in notices:
        lines += [f"> ⚠️ N3MC notice: {n}", ""]
    lines.append(text)
    return "\n".join(lines) + "\n"


def _search_output(cfg, server_ok, query, session_id=None, since=None, until=None):
    notices = _take_notices()
    if not (query or "").strip():
        return _placeholder("_No relevant memories found._", notices)
    if _FOREIGN_SERVER_ROOT:
        return _placeholder(f"_(memory search unavailable: another N3MC folder's server is using "
                            f"port {_PORT})_", notices)
    if not server_ok:
        reason = ("the memory server is still starting" if _SERVER_STARTING
                  else "the memory server could not be started")
        return _placeholder(f"_(memory search unavailable: {reason})_", notices)
    try:
        res = _post("/search", {"query": query[:cfg["search_query_max_chars"]],
                                "session_id": session_id, "since": since, "until": until})
    except Exception as e:
        return _placeholder(f"_(memory search unavailable: {type(e).__name__}: {e})_", notices)
    return render_memory_context(res, query[:cfg["search_query_max_chars"]], notices)


def _emit(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def _checked_range(since, until):
    """(since, until, 0) or (None, None, 2) after one stderr line (shared by --search / --list)."""
    # Parse each bound on its own so the error names the flag that failed
    # (not guessed from the message text: "--since ' '" used to be reported as --until).
    for flag, value, is_until in (("--since", since, False), ("--until", until, True)):
        if value:
            try:
                _parse_time_bound(value, is_until)
            except ValueError as e:
                _err(f"{flag}: cannot read '{e}' (use 2026-11-13 or 2026-11-13T09:00)")
                return None, None, 2
    try:
        s, u = _time_range(since, until)
        return s, u, 0
    except ValueError as e:
        _err(str(e))
        return None, None, 2


def _cli_session_id():
    """N3MC_SESSION_ID of the process that runs the CLI, or None.

    The resident server may have been started by another process (usually the hook) with a
    different environment, so --buffer / --search send the caller's value explicitly. Without it
    the server's own session is used (resolution order, §3 "Identifiers")."""
    return os.environ.get("N3MC_SESSION_ID") or None


def cmd_search(query, since=None, until=None):
    s, u, rc = _checked_range(since, until)
    if rc:
        return rc
    cfg = load_config()
    _set_port(cfg)
    query = sanitize_surrogates(query or "")
    server_ok = ensure_server() if query.strip() else False
    _emit(_search_output(cfg, server_ok, query, _cli_session_id(), s, u))
    return 0


def cmd_buffer(text, agent_id=None):
    if text == "-":
        text = sys.stdin.read()
    text = sanitize_surrogates(text or "")
    cfg = load_config()
    _set_port(cfg)
    server_ok = ensure_server()
    r = _save_record(cfg, server_ok, text, agent_id, _cli_session_id())
    return 1 if r.get("status") == "error" else 0


def cmd_repair():
    cfg = load_config()
    _set_port(cfg)
    server_ok = ensure_server()
    if _FOREIGN_SERVER_ROOT:
        _err(f"N3MC: --repair skipped: another N3MC folder's server is using port {_PORT}")
        return 0
    if server_ok:
        try:
            n = _post("/repair", {}, timeout=3600).get("count", 0)
        except Exception as e:
            _err(f"N3MC: --repair failed: {e}")
            return 1
    else:
        n = repair_records(cfg)
    if n:
        print(f"Repaired {n} record(s)")
    return 0


def _read_stdin_payload():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    return raw, payload


def cmd_hook_submit():
    raw, payload = _read_stdin_payload()
    if not isinstance(payload, dict):
        payload = {"prompt": raw}
    payload = sanitize_surrogates(payload)
    cfg = load_config()
    _set_port(cfg)
    prompt = payload.get("prompt", payload.get("message"))
    text = _extract_text(prompt)
    if text.lstrip().startswith(_SYSTEM_PROMPT_PREFIXES):
        _audit({"ts": _now_iso(), "hook": "UserPromptSubmit", "stage": "skipped",
                "reason": "system_prompt"})
        _emit(_placeholder("_No relevant memories found._", []))
        return 0
    session_id = payload.get("session_id")
    server_ok = ensure_server()
    if server_ok:
        try:
            _post("/repair", {"max_records": 200}, timeout=120)
        except Exception as e:
            _err(f"N3MC: repair skipped ({e})")
    prev = payload.get("last_assistant_message") or _read_previous_reply(
        payload.get("transcript_path"), text)
    if prev:
        old_turn = _read_turn_id(session_id) or str(uuid.uuid4())
        _save_chunks(cfg, server_ok, prev, "claude", old_turn, session_id)
    turn_id = str(uuid.uuid4())
    _write_turn_id(session_id, turn_id)
    if text.strip():
        _emit(_search_output(cfg, server_ok, text, session_id))
        _save_chunks(cfg, server_ok, text, "user", turn_id, session_id)
    else:
        _emit(_placeholder("_No relevant memories found._", _take_notices()))
    return 0


def cmd_save_claude_turn():
    _raw, payload = _read_stdin_payload()
    if not isinstance(payload, dict):
        return 0
    payload = sanitize_surrogates(payload)
    msg = payload.get("last_assistant_message")
    if not isinstance(msg, str) or not msg.strip():
        return 0
    cfg = load_config()
    _set_port(cfg)
    session_id = payload.get("session_id")
    turn_id = _read_turn_id(session_id) or str(uuid.uuid4())
    server_ok = ensure_server()
    _save_chunks(cfg, server_ok, msg, "claude", turn_id, session_id)
    return 0


def _oneline(s):
    return re.sub(r"[\r\n\t]", " ", s)


def _open_existing_db():
    if not os.path.exists(DB_PATH):
        _err(f"No DB found at {DB_PATH}")
        return None
    return get_connection(DB_PATH)


def cmd_list(since=None, until=None, fmt="table"):
    s, u, rc = _checked_range(since, until)
    if rc:
        return rc
    conn = _open_existing_db()
    if conn is None:
        return 0
    try:
        rows = get_all_memories(conn, s, u)
    finally:
        conn.close()
    for r in rows:
        if fmt == "jsonl":
            print(json.dumps({"id": r["id"], "timestamp": r["timestamp"],
                              "agent_name": r["agent_name"], "content": r["content"]},
                             ensure_ascii=False))
        else:
            print(f"{r['id']}\t{r['timestamp']}\t{r['agent_name'] or '-'}\t"
                  f"{_oneline(r['content'][:80])}")
    out = sys.stderr if fmt == "jsonl" else sys.stdout
    print(f"Total: {len(rows)} records", file=out)
    return 0


def cmd_recall_thread(turn_id, before=2, after=2, fmt="table"):
    conn = _open_existing_db()
    if conn is None:
        return 0
    try:
        lo, hi = conn.execute("SELECT MIN(rowid), MAX(rowid) FROM memories WHERE turn_id = ?",
                              (turn_id,)).fetchone()
        ids = []
        if lo is not None:
            prev = [r[0] for r in conn.execute(
                "SELECT turn_id FROM memories WHERE turn_id IS NOT NULL AND turn_id <> ? "
                "GROUP BY turn_id HAVING MAX(rowid) < ? ORDER BY MAX(rowid) DESC LIMIT ?",
                (turn_id, lo, before))]
            nxt = [r[0] for r in conn.execute(
                "SELECT turn_id FROM memories WHERE turn_id IS NOT NULL AND turn_id <> ? "
                "GROUP BY turn_id HAVING MIN(rowid) > ? ORDER BY MIN(rowid) ASC LIMIT ?",
                (turn_id, hi, after))]
            ids = prev[::-1] + [turn_id] + nxt
        rows = []
        if ids:
            q = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT id, timestamp, agent_name, turn_id, content FROM memories "
                                f"WHERE turn_id IN ({q}) ORDER BY rowid", ids).fetchall()
    finally:
        conn.close()
    for r in rows:
        if fmt == "jsonl":
            print(json.dumps({"id": r[0], "timestamp": r[1], "agent_name": r[2], "turn_id": r[3],
                              "content": r[4]}, ensure_ascii=False))
        else:
            print(f"{r[0]}\t{r[1]}\t{r[2] or '-'}\t{_oneline(r[4][:80])}")
    out = sys.stderr if fmt == "jsonl" else sys.stdout
    print(f"Total: {len(rows)} records", file=out)
    return 0


def cmd_show_turn(turn_id):
    conn = _open_existing_db()
    if conn is None:
        return 0
    try:
        rows = get_memories_by_turn_id(conn, turn_id)
    finally:
        conn.close()
    for r in rows:
        print(f"--- {r['id']} saved={_fmt_saved(r['timestamp'])} ---")
        print(r["content"])
    print(f"Total: {len(rows)} records")
    return 0


# ----------------------------------------------------------------------- --stop

_BEHAVIOR_MD_MARKER = "<!-- n3mc-behavior v3 -->"
_KNOWN_BEHAVIOR_MD_HASHES = {"ade0a59b94f59f9a", "34ed39efe5a3833d", "5af3c015c06c62c1"}
_BEHAVIOR_MD_CONTENT = '''<!-- n3mc-behavior v3 -->
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
'''


def _read_text_lf(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace").replace("\r\n", "\n")


def _ensure_behavior_md(cwd=None):
    path = claude_paths(cwd)["behavior_md"]
    key = "behavior_md_edited:" + hashlib.sha256(
        os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
    _clear_notice("behavior_md_edited")  # v1.4.0 key
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        _clear_notice(key)
        return
    text = _read_text_lf(path)
    if text.split("\n", 1)[0].strip() == _BEHAVIOR_MD_MARKER:
        _clear_notice(key)
        return
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if digest in _KNOWN_BEHAVIOR_MD_HASHES:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, f"{path}.bak-n3mc-{stamp}")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        _clear_notice(key)
    else:
        _notify(key, f"The rules file {os.path.abspath(path)} has been edited by hand, so it was "
                     "not updated. Merge the new template manually if needed.")


_IMPORT_LINE_RE = re.compile(r"^[ \t]*@[^\n]*memory_context\.md[ \t\r]*$")
_N3MC_COMMENT_RE = re.compile(r"^#\s*(N3MemoryCore\b|Path resolves to|If you have not yet run)")


def _retire_memory_context(cwd=None):
    md = claude_paths(cwd)["claude_md"]
    if os.path.exists(md):
        with open(md, "rb") as f:
            raw = f.read()
        lines = raw.decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
        mine = _norm(CONTEXT_FILE)
        drop = set()
        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            if not _IMPORT_LINE_RE.match(body):
                continue
            target = os.path.expanduser(body.strip()[1:].strip())
            if not os.path.isabs(target) or \
                    os.path.normcase(os.path.normpath(target)) != os.path.normcase(os.path.normpath(CONTEXT_FILE)):
                continue
            drop.add(i)
            j = i - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            comments = []
            while j >= 0 and _N3MC_COMMENT_RE.match(lines[j].strip()):
                comments.append(j)
                j -= 1
            if comments:
                drop.update(comments)
                drop.update(range(comments[0] + 1, i))  # blank lines between comment and import
        if drop:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copy2(md, f"{md}.bak-n3mc-{stamp}")
            kept = "".join(l for i, l in enumerate(lines) if i not in drop)
            with open(md, "wb") as f:
                f.write(kept.encode("utf-8", errors="surrogateescape"))
    if not os.path.exists(CONTEXT_RETIRED) and os.path.exists(CONTEXT_FILE):
        os.remove(CONTEXT_FILE)
        with open(CONTEXT_RETIRED, "w", encoding="utf-8") as f:
            f.write(_now_iso())


def _housekeeping():
    now = time.time()
    try:
        for name in os.listdir(MEMORY_DIR):
            if name.startswith("turn_id_") and name.endswith(".txt"):
                p = os.path.join(MEMORY_DIR, name)
                if now - os.path.getmtime(p) > 30 * 86400:
                    os.remove(p)
    except FileNotFoundError:
        pass
    try:
        if os.path.getsize(AUDIT_LOG) > AUDIT_MAX_BYTES:
            os.replace(AUDIT_LOG, AUDIT_LOG + ".1")
    except FileNotFoundError:
        pass


def cmd_stop():
    for step in (_ensure_behavior_md, _retire_memory_context, _housekeeping):
        try:
            step()
        except Exception as e:
            _err(f"N3MC: --stop {step.__name__}: {e}")
    return 0


# ------------------------------------------------------------------------ --init

def cmd_init():
    import pathlib
    import sysconfig
    import n3memorycore

    settings_path = pathlib.Path(os.path.expanduser("~/.claude/settings.json"))
    pkg_dir = os.path.normcase(os.path.abspath(os.path.dirname(n3memorycore.__file__)))
    ours = {
        "UserPromptSubmit": ("n3mc-hook", "n3memorycore.n3mc_hook"),
        "Stop": ("n3mc-stop-hook", "n3memorycore.n3mc_stop_hook"),
    }
    import shlex

    def tokens(command):
        command = command.replace("\\", "/")
        try:
            return shlex.split(command, posix=True)
        except ValueError:
            return command.split()

    def is_own_hook(command, event):
        exe_name, module = ours[event]
        toks = tokens(command)
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
                if d == pkg_dir:
                    return True
        return False

    def shell_path(p):
        s = pathlib.Path(p).as_posix()
        return f'"{s}"' if " " in s else s

    def resolve_cmd(script_name):
        folders = [sysconfig.get_path("scripts")]
        try:
            folders.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
        except (KeyError, ValueError):
            pass
        for folder in folders:
            for name in (script_name + ".exe", script_name):
                cand = pathlib.Path(folder) / name
                if cand.is_file():
                    return shell_path(cand)
        exe = shutil.which(script_name)
        if exe:
            return shell_path(exe)
        module = {"n3mc-hook": "n3memorycore.n3mc_hook",
                  "n3mc-stop-hook": "n3memorycore.n3mc_stop_hook"}[script_name]
        return f"{shell_path(sys.executable)} -m {module}"

    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8")
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError as e:
            _err(f"{settings_path} is not valid JSON ({e}). Nothing was changed. "
                 "Fix the file, then run n3mc --init again.")
            return 1
        perms = settings.get("permissions", {}) if isinstance(settings, dict) else None
        if (not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict)
                or not isinstance(perms, dict) or not isinstance(perms.get("allow", []), list)):
            _err(f"{settings_path} has an unexpected structure. Nothing was changed.")
            return 1
        before = json.loads(raw)
    else:
        raw, settings, before = None, {}, None

    hooks = settings.setdefault("hooks", {})
    for event, (exe_name, _module) in ours.items():
        kept = []
        for entry in hooks.get(event, []):
            inner = [h for h in entry.get("hooks", []) if not is_own_hook(h.get("command", ""), event)]
            for h in inner:
                c = h.get("command", "")
                if any(m in c for m in ("n3mc_hook", "n3mc-hook", "n3mc_stop_hook", "n3mc-stop-hook")):
                    print(f"Another N3MC hook is registered for {event} and was left in place: {c}")
            if inner:
                kept.append({**entry, "hooks": inner})
        kept.append({"hooks": [{"type": "command", "command": resolve_cmd(exe_name)}]})
        hooks[event] = kept

    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    for rule in PERMISSION_RULES:
        if rule not in allow:
            allow.append(rule)

    if settings != before:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copy2(settings_path, settings_path.with_name(f"settings.json.bak-n3mc-{stamp}"))
        fd, tmp = tempfile.mkstemp(dir=settings_path.parent, prefix=".settings.json.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, settings_path)

    load_config()
    os.makedirs(MEMORY_DIR, exist_ok=True)
    conn = _connect_checked(None)
    try:
        init_db(conn)
    finally:
        conn.close()
    print("次のセッションから保存できます。 (Restart Claude Code to activate.)")
    return 0


# ======================================================================== main

def main(argv=None):
    _utf8()
    p = argparse.ArgumentParser(prog="n3mc", description="N3MemoryCore Free")
    p.add_argument("--init", action="store_true")
    p.add_argument("--buffer", metavar="TEXT")
    p.add_argument("--agent-id", metavar="NAME")
    p.add_argument("--search", metavar="QUERY")
    p.add_argument("--list", action="store_true")
    p.add_argument("--recall-thread", metavar="TURN_ID")
    p.add_argument("--show-turn", metavar="TURN_ID")
    p.add_argument("--before", type=int, default=2)
    p.add_argument("--after", type=int, default=2)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--format", choices=("table", "jsonl"), default="table")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--repair", action="store_true")
    p.add_argument("--reembed", action="store_true")
    p.add_argument("--hook-submit", action="store_true")
    p.add_argument("--save-claude-turn", action="store_true")
    p.add_argument("--run-server", action="store_true")
    p.add_argument("--version", action="version", version=f"n3memorycore {__version__}")
    a = p.parse_args(argv)
    try:
        if a.run_server:
            return run_server()
        if a.init:
            return cmd_init()
        if a.hook_submit:
            return cmd_hook_submit()
        if a.save_claude_turn:
            return cmd_save_claude_turn()
        if a.stop:
            return cmd_stop()
        if a.reembed:
            return cmd_reembed()
        if a.repair:
            return cmd_repair()
        if a.buffer is not None:
            return cmd_buffer(a.buffer, a.agent_id)
        if a.search is not None:
            return cmd_search(a.search, a.since, a.until)
        if a.list:
            return cmd_list(a.since, a.until, a.format)
        if a.recall_thread:
            return cmd_recall_thread(a.recall_thread, a.before, a.after, a.format)
        if a.show_turn:
            return cmd_show_turn(a.show_turn)
    except BrokenPipeError:
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
