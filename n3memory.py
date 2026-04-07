"""
N3MemoryCore - n3memory.py
Main CLI entry point + FastAPI server definition
"""
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# UTF-8 reconfigure (Windows cp932 fix)
# ---------------------------------------------------------------------------
def _reconfigure_utf8():
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent.resolve()
_MEMORY_DIR = _THIS_DIR / ".memory"
_DB_PATH = _MEMORY_DIR / "n3memory.db"
_PID_FILE = _MEMORY_DIR / "n3mc.pid"
_CONTEXT_FILE = _MEMORY_DIR / "memory_context.md"
_CONFIG_FILE = _THIS_DIR / "config.json"
_VEC_MIGRATED_MARKER = _MEMORY_DIR / "vec_e5v2_migrated"
_FTS_CLEANED_MARKER = _MEMORY_DIR / "fts_punct_cleaned"

# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG = {
    "owner_id": None,
    "local_id": None,
    "server_port": 18520,
    "dedup_threshold": 0.95,
    "half_life_days": 90,
    "bm25_min_threshold": 0.1,
    "search_result_limit": 20,
    "context_char_limit": 3000,
    "min_score": 0.2,
    "search_query_max_chars": 2000,
}


def _load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    if _CONFIG_FILE.exists():
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            cfg.update(loaded)
        except Exception as e:
            print(f"[N3MC] WARNING: config.json parse error: {e}", file=sys.stderr)
            # Attempt DB recovery for owner_id / local_id
            try:
                if _DB_PATH.exists():
                    import sqlite3 as _sqlite3
                    conn = _sqlite3.connect(str(_DB_PATH))
                    row = conn.execute(
                        "SELECT owner_id FROM memories GROUP BY owner_id ORDER BY COUNT(*) DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        cfg["owner_id"] = row[0]
                    row2 = conn.execute(
                        "SELECT local_id FROM memories GROUP BY local_id ORDER BY COUNT(*) DESC LIMIT 1"
                    ).fetchone()
                    if row2:
                        cfg["local_id"] = row2[0]
                    conn.close()
            except Exception:
                pass

    # Generate missing UUIDs
    changed = False
    if not cfg.get("owner_id"):
        cfg["owner_id"] = str(uuid.uuid4())
        changed = True
    if not cfg.get("local_id"):
        cfg["local_id"] = str(uuid.uuid4())
        changed = True

    # Fill any missing default fields
    for k, v in _DEFAULT_CONFIG.items():
        if k not in cfg and v is not None:
            cfg[k] = v
            changed = True

    if changed:
        _save_config(cfg)
    return cfg


def _save_config(cfg: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# DB integrity check
# ---------------------------------------------------------------------------
def _check_db_integrity(db_path: Path) -> bool:
    if not db_path.exists():
        return True
    try:
        import sqlite3 as _s
        conn = _s.connect(str(db_path))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result and result[0] == "ok"
    except Exception:
        return False


def _handle_corrupt_db(db_path: Path) -> None:
    bak = db_path.parent / f"{db_path.name}.corrupt.bak"
    db_path.rename(bak)
    print(
        f"[N3MC] WARNING: DB corruption detected. Renamed to {bak}. "
        f"A new empty DB will be created. "
        f"To recover, restore from backup and restart.",
        file=sys.stderr
    )


# ---------------------------------------------------------------------------
# Server process management
# ---------------------------------------------------------------------------
def _get_server_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid() -> Optional[int]:
    try:
        if _PID_FILE.exists():
            return int(_PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        pass
    return None


def _ping_server(port: int, timeout: float = 2.0) -> bool:
    try:
        import urllib.request
        req = urllib.request.urlopen(
            f"{_get_server_url(port)}/health", timeout=timeout
        )
        return req.status == 200
    except Exception:
        return False


def _start_server(port: int) -> None:
    """Start the FastAPI server in background using PID file atomic creation."""
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    server_script = str(_THIS_DIR / "n3memory.py")

    # Atomic PID file creation to prevent duplicate launches
    try:
        fd = os.open(str(_PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, b"0")  # placeholder
        os.close(fd)
    except FileExistsError:
        # Another process is starting — wait for it
        return

    proc = subprocess.Popen(
        [sys.executable, server_script, "--run-server"],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    _PID_FILE.write_text(str(proc.pid), encoding="utf-8")


def ensure_server(config: dict) -> bool:
    """Ensure server is running. Returns True if server is responsive."""
    port = config.get("server_port", 18520)

    # Check if PID file exists and process is alive
    pid = _read_pid()
    if pid and _is_process_running(pid):
        if _ping_server(port):
            return True
        # Process alive but not responding — wait a bit
        for _ in range(10):
            time.sleep(0.5)
            if _ping_server(port):
                return True

    # Server not running or not responding — (re)start
    if _PID_FILE.exists():
        _PID_FILE.unlink(missing_ok=True)

    _start_server(port)

    # Wait for server to become responsive (up to 60s for first-run model download)
    for i in range(120):
        time.sleep(0.5)
        # Check if another process is still in startup (pid=0 placeholder)
        pid = _read_pid()
        if pid == 0:
            continue
        if _ping_server(port):
            return True

    print("[N3MC] WARNING: Server did not start in time.", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _post(url: str, data: dict, timeout: float = 30.0) -> Optional[dict]:
    import urllib.request
    import urllib.error
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _get(url: str, timeout: float = 30.0) -> Optional[dict]:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------
def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate at a sentence boundary within max_chars."""
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    for end_char in ('.', '!', '?', '\n'):
        idx = chunk.rfind(end_char)
        if idx > max_chars // 2:
            return chunk[:idx + 1]
    return chunk


def _buffer_direct(content: str, config: dict, agent_id: Optional[str] = None) -> bool:
    """Fallback: write directly to DB without embedding (server offline)."""
    sys.path.insert(0, str(_THIS_DIR / "core"))
    from database import get_connection, init_db, insert_memory, check_exact_duplicate, count_memories

    try:
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)

        if not _check_db_integrity(_DB_PATH):
            _handle_corrupt_db(_DB_PATH)

        conn = get_connection(str(_DB_PATH))
        init_db(conn)

        # Purify (same as server endpoint) — strip code blocks
        content = purify_text(content)

        if check_exact_duplicate(conn, content):
            conn.close()
            return True

        before = count_memories(conn)
        from uuid_extensions import uuid7 as _gen_uuid7
        record_id = str(_gen_uuid7())
        ts = datetime.now(tz=timezone.utc).isoformat()

        insert_memory(
            conn, record_id, content, ts,
            config["owner_id"], None,
            config.get("local_id"),
            agent_id,
        )
        after = count_memories(conn)
        conn.close()

        if after <= before:
            print("⚠️ Physical save failed. Current memories may be lost.", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"⚠️ Physical save failed. Current memories may be lost. Error: {e}", file=sys.stderr)
        return False


def cmd_buffer(text: str, config: dict, agent_id: Optional[str] = None) -> None:
    """Save a memory record."""
    if not text or not text.strip():
        return

    port = config.get("server_port", 18520)
    url = f"{_get_server_url(port)}/buffer"

    data = {"content": text}
    if agent_id:
        data["agent_id"] = agent_id

    result = _post(url, data)
    if result is None:
        # Server offline — direct write
        _buffer_direct(text, config, agent_id)
    elif result.get("status") != "ok":
        print(f"⚠️ Physical save failed. Current memories may be lost. {result}", file=sys.stderr)


def cmd_search(query: str, config: dict) -> list:
    """Search memories and output results."""
    query = query[:config.get("search_query_max_chars", 2000)]

    port = config.get("server_port", 18520)
    url = f"{_get_server_url(port)}/search"

    result = _post(url, {"query": query})
    if result is None:
        print("[N3MC] Search failed: server not responding.", file=sys.stderr)
        return []

    results = result.get("results", [])
    _write_context(results)
    return results


def _write_context(results: list) -> None:
    """Write search results to memory_context.md AND stdout."""
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    if results:
        lines.append("# N3MemoryCore — Relevant Memories\n")
        for r in results:
            score = r.get("score", 0)
            content = r.get("content", "")
            ts = r.get("timestamp", "")
            lines.append(f"- [{score:.4f}] ({ts[:10]}) {content}")
        output = "\n".join(lines)
    else:
        output = ""

    # Write to file
    try:
        _CONTEXT_FILE.write_text(output, encoding="utf-8")
    except Exception as e:
        print(f"[N3MC] WARNING: Failed to write memory_context.md: {e}", file=sys.stderr)

    # Print to stdout (required for Claude to see results)
    if output:
        print(output)


def cmd_repair(config: dict) -> None:
    """Repair unindexed records."""
    port = config.get("server_port", 18520)
    url = f"{_get_server_url(port)}/repair"
    result = _post(url, {})
    if result and result.get("count", 0) > 0:
        print(f"[N3MC] Repaired {result['count']} records.", file=sys.stderr)


def cmd_list(config: dict) -> None:
    """List all memory records."""
    port = config.get("server_port", 18520)
    url = f"{_get_server_url(port)}/list"
    result = _get(url)
    if result is None:
        print("[N3MC] List failed: server not responding.", file=sys.stderr)
        return

    records = result.get("records", [])
    for r in records:
        rec_id = r.get("id", "")
        ts = r.get("timestamp", "")[:19]
        agent = r.get("agent_id") or ""
        content = r.get("content", "")[:80]
        print(f"{rec_id}\t{ts}\t{agent}\t{content}")
    print(f"Total: {result.get('total', len(records))} records")


def cmd_stop(config: dict) -> None:
    """Session cleanup: ensure CLAUDE.md @import and n3mc-behavior.md exist."""
    project_root = _THIS_DIR.parent
    claude_dir = project_root / ".claude"
    rules_dir = claude_dir / "rules"
    claude_md = claude_dir / "CLAUDE.md"
    behavior_md = rules_dir / "n3mc-behavior.md"

    # Ensure rules dir
    rules_dir.mkdir(parents=True, exist_ok=True)

    # Idempotently create n3mc-behavior.md
    if not behavior_md.exists():
        behavior_md.write_text(
            "# N3MemoryCore Behavioral Guidelines\n\n"
            "## Fully Automatic Saving\n"
            "All conversations are automatically saved by hooks. Claude does NOT need to call `--buffer` manually.\n"
            "Do NOT use your own judgment to decide what to save — the hooks handle everything.\n\n"
            "## Active RAG\n"
            "When knowledge is insufficient, proactively execute `--search` to retrieve relevant\n"
            "memories. Search first, then answer. The command is auto-approved — no confirmation needed.\n\n"
            "## Large Text Handling\n"
            "When a user pastes a long text (spec, article, log), do NOT save it as-is.\n"
            "Read and understand the full content, extract each key fact as a separate short sentence\n"
            "(~50-200 chars), and save each with its own `--buffer` call.\n"
            "This is the ONE exception where manual `--buffer` is appropriate.\n\n"
            "## Command Visibility\n"
            "N3MemoryCore commands (`--search`, `--buffer`) are auto-approved via `permissions.allow`\n"
            "in settings.json. They execute silently without Yes/No prompts.\n\n"
            "## Session Start\n"
            "Memory context is loaded via @import from memory_context.md in CLAUDE.md.\n",
            encoding="utf-8",
        )

    # Idempotently ensure CLAUDE.md has @import line
    import_line = "@../N3MemoryCore/.memory/memory_context.md"
    claude_dir.mkdir(parents=True, exist_ok=True)

    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        # Remove legacy N3MC_AUTO zone if present
        import re
        content = re.sub(
            r'<!-- N3MC_AUTO_START -->.*?<!-- N3MC_AUTO_END -->',
            '',
            content,
            flags=re.DOTALL,
        )
        if import_line not in content:
            content = import_line + "\n" + content
        claude_md.write_text(content, encoding="utf-8")
    else:
        claude_md.write_text(import_line + "\n", encoding="utf-8")


def cmd_hook_submit(config: dict) -> None:
    """
    UserPromptSubmit hook entry point.
    Reads JSON from stdin: {message/prompt, last_assistant_message}
    Order: --repair -> --buffer(claude) -> --search -> --buffer(user)
    """
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        return

    ensure_server(config)

    # Extract user message
    user_msg = data.get("message") or data.get("prompt") or ""
    user_text = _extract_text(user_msg)

    # Extract last assistant message
    last_claude = data.get("last_assistant_message") or ""
    if isinstance(last_claude, list):
        last_claude = _extract_text(last_claude)

    # 1. repair
    try:
        cmd_repair(config)
    except Exception:
        pass

    # 2. buffer: save Claude's previous response
    if last_claude:
        claude_content = _prepare_claude_buffer(last_claude)
        if claude_content:
            cmd_buffer(claude_content, config, agent_id="claude-code")

    # 3. search
    if user_text and len(user_text.strip()) >= 2:
        try:
            cmd_search(user_text, config)
        except Exception:
            pass

    # 4. buffer: save user message
    if user_text:
        user_content = _prepare_user_buffer(user_text)
        if user_content:
            cmd_buffer(user_content, config, agent_id="claude-code")


def _extract_text(msg) -> str:
    """Extract text from plain string or multimodal JSON array."""
    if isinstance(msg, str):
        # Try to parse as JSON array
        try:
            arr = json.loads(msg)
            if isinstance(arr, list):
                return " ".join(
                    item.get("text", "") for item in arr
                    if isinstance(item, dict) and item.get("type") == "text"
                ).strip()
        except Exception:
            pass
        return msg
    elif isinstance(msg, list):
        return " ".join(
            item.get("text", "") for item in msg
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
    return str(msg) if msg else ""


_SKIP_PATTERNS = re.compile(
    r'^(ok|yes|no|sure|thanks|thank you|got it|understood|sounds good|'
    r'great|perfect|alright|okay|yep|nope|cool|noted|right|exactly|correct|'
    r'i see|i understand|please|go ahead|proceed|continue|done|finish)[\.\!\?]*$',
    re.IGNORECASE,
)


def _prepare_user_buffer(text: str) -> Optional[str]:
    """Prepare user message for buffering with noise filter."""
    text = text.strip()
    if len(text) < 10:
        return None
    if _SKIP_PATTERNS.match(text):
        return None
    cleaned = purify_text(text)
    truncated = _truncate_at_sentence(cleaned, 300)
    return f"[user] {truncated}" if truncated else None


def _prepare_claude_buffer(text: str) -> Optional[str]:
    """Prepare Claude response for buffering with noise filter."""
    text = text.strip()
    if len(text) < 3:
        return None
    cleaned = purify_text(text)
    truncated = _truncate_at_sentence(cleaned, 300)
    return f"[claude] {truncated}" if truncated else None


def purify_text(text: str) -> str:
    """Replace multi-line code blocks with [code omitted]."""
    return re.sub(r'```[\s\S]*?```', '[code omitted]', text)


# ---------------------------------------------------------------------------
# FastAPI server (run in subprocess)
# ---------------------------------------------------------------------------
def run_server():
    """Start the FastAPI server. Called with --run-server flag."""
    import sys
    sys.path.insert(0, str(_THIS_DIR / "core"))

    config = _load_config()
    port = config.get("server_port", 18520)

    # Check DB integrity
    if _DB_PATH.exists() and not _check_db_integrity(_DB_PATH):
        _handle_corrupt_db(_DB_PATH)

    # Preload embedding model before uvicorn starts
    from processor import get_model, embed_passage, embed_query, hybrid_search, purify
    from database import (
        get_connection, init_db, insert_memory, check_exact_duplicate,
        count_memories, find_unindexed_memories, serialize_vector,
        strip_fts_punctuation, delete_memory, get_all_memories,
    )
    from uuid_extensions import uuid7 as _gen_uuid7

    try:
        get_model()
    except Exception as e:
        print(f"[N3MC] Model load warning: {e}", file=sys.stderr)

    # Initialize DB
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    conn_init = get_connection(str(_DB_PATH))
    init_db(conn_init)
    conn_init.close()

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    # Session ID for this server run
    session_id = str(uuid.uuid4())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(lifespan=lifespan)

    class BufferRequest(BaseModel):
        content: str
        agent_id: Optional[str] = None
        local_id: Optional[str] = None

    class SearchRequest(BaseModel):
        query: str

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/buffer")
    def buffer_endpoint(req: BufferRequest):
        if not req.content or not req.content.strip():
            return {"status": "error", "message": "empty content"}

        conn = get_connection(str(_DB_PATH))
        try:
            text = purify(req.content)

            # Exact dedup
            if check_exact_duplicate(conn, text):
                conn.close()
                return {"status": "ok", "count": 0}

            # Vector dedup
            try:
                qvec = embed_passage(text)
                from database import search_vector
                vec_results = search_vector(conn, qvec, k=1)
                if vec_results:
                    from processor import cosine_sim_from_l2
                    top_cos = cosine_sim_from_l2(vec_results[0][1])
                    if top_cos >= config.get("dedup_threshold", 0.95):
                        conn.close()
                        return {"status": "ok", "count": 0}
            except Exception:
                qvec = None

            before = count_memories(conn)
            record_id = str(_gen_uuid7())
            ts = datetime.now(tz=timezone.utc).isoformat()
            local_id_val = req.local_id or config.get("local_id")

            insert_memory(
                conn, record_id, text, ts,
                config["owner_id"],
                qvec,
                local_id_val,
                req.agent_id,
                session_id,
            )
            after = count_memories(conn)
            conn.close()

            if after <= before:
                return JSONResponse(
                    status_code=500,
                    content={"status": "error", "message": "Physical save verification failed"}
                )
            return {"status": "ok", "count": 1}
        except Exception as e:
            conn.close()
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.post("/search")
    def search_endpoint(req: SearchRequest):
        if not req.query:
            return {"results": []}
        query = req.query[:config.get("search_query_max_chars", 2000)]
        conn = get_connection(str(_DB_PATH))
        try:
            results = hybrid_search(conn, query, config)
            conn.close()
            return {"results": results}
        except Exception as e:
            conn.close()
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.post("/repair")
    def repair_endpoint():
        conn = get_connection(str(_DB_PATH))
        try:
            repaired = _do_repair(conn, config)
            conn.close()
            return {"status": "ok", "count": repaired}
        except Exception as e:
            conn.close()
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.get("/list")
    def list_endpoint():
        conn = get_connection(str(_DB_PATH))
        try:
            rows = get_all_memories(conn)
            conn.close()
            records = [
                {
                    "id": r["id"],
                    "content": r["content"],
                    "timestamp": r["timestamp"],
                    "agent_id": r["agent_id"],
                }
                for r in rows
            ]
            return {"records": records, "total": len(records)}
        except Exception as e:
            conn.close()
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def _do_repair(conn, config: dict) -> int:
    """
    Repair unindexed records. Returns number of records repaired.
    Also runs one-time FTS punctuation cleaning migration.
    """
    import sys
    sys.path.insert(0, str(_THIS_DIR / "core"))
    from processor import embed_passage
    from database import (
        find_unindexed_memories, serialize_vector, strip_fts_punctuation,
        get_memory_by_rowid,
    )

    # One-time FTS punctuation cleaning
    fts_cleaned = _FTS_CLEANED_MARKER.exists()
    if not fts_cleaned:
        _run_fts_punct_cleaning(conn)
        _FTS_CLEANED_MARKER.touch()

    # Vector re-index migration (e5-base-v2)
    if not _VEC_MIGRATED_MARKER.exists():
        _run_vec_reindex(conn)
        _VEC_MIGRATED_MARKER.touch()

    # Find unindexed records.
    # NOTE: Do NOT use OFFSET pagination here. After each batch is repaired and
    # committed, those rows leave the WHERE clause, so OFFSET would skip records.
    # Instead, re-query with LIMIT each iteration — repaired rows naturally fall out.
    batch_size = 200
    repaired = 0

    while True:
        rows = conn.execute("""
            SELECT m.id, m.content, m.timestamp, m.rowid,
                   CASE WHEN mv.rowid IS NOT NULL THEN 1 ELSE 0 END AS has_vec,
                   CASE WHEN mf.rowid IS NOT NULL THEN 1 ELSE 0 END AS has_fts
            FROM memories m
            LEFT JOIN memories_vec mv ON mv.rowid = m.rowid
            LEFT JOIN memories_fts mf ON mf.rowid = m.rowid
            WHERE mv.rowid IS NULL OR mf.rowid IS NULL
            LIMIT ?
        """, (batch_size,)).fetchall()

        if not rows:
            break

        for row in rows:
            rowid = row[3]
            content = row[1]
            has_vec = row[4]
            has_fts = row[5]

            if not has_vec:
                try:
                    vec = embed_passage(content)
                    conn.execute(
                        "INSERT OR REPLACE INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                        (rowid, serialize_vector(vec))
                    )
                except Exception:
                    pass

            if not has_fts:
                try:
                    conn.execute(
                        "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                        (rowid, strip_fts_punctuation(content))
                    )
                except Exception:
                    pass

            repaired += 1

        conn.commit()

    if repaired > 0:
        print(f"[N3MC] Repaired {repaired} unindexed records.", file=sys.stderr)

    return repaired


def _run_fts_punct_cleaning(conn) -> None:
    """One-time migration: re-index FTS records with punctuation stripping."""
    import sys
    sys.path.insert(0, str(_THIS_DIR / "core"))
    from database import strip_fts_punctuation

    batch_size = 200
    offset = 0
    cleaned = 0

    while True:
        rows = conn.execute(
            "SELECT rowid, content FROM memories LIMIT ? OFFSET ?",
            (batch_size, offset)
        ).fetchall()
        if not rows:
            break
        for row in rows:
            rowid = row[0]
            content = row[1]
            stripped = strip_fts_punctuation(content)
            # Delete existing FTS entry and re-insert
            try:
                conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                    (rowid, stripped)
                )
                cleaned += 1
            except Exception:
                pass
        conn.commit()
        offset += batch_size

    if cleaned > 0:
        print(f"[N3MC] FTS punctuation cleaning: {cleaned} records re-indexed.", file=sys.stderr)


def _run_vec_reindex(conn) -> None:
    """One-time vector re-index migration (for model upgrade)."""
    import sys
    sys.path.insert(0, str(_THIS_DIR / "core"))
    from processor import embed_passage
    from database import serialize_vector

    batch_size = 200
    offset = 0
    reindexed = 0

    while True:
        rows = conn.execute(
            "SELECT rowid, content FROM memories LIMIT ? OFFSET ?",
            (batch_size, offset)
        ).fetchall()
        if not rows:
            break
        for row in rows:
            rowid = row[0]
            content = row[1]
            try:
                vec = embed_passage(content)
                conn.execute(
                    "INSERT OR REPLACE INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                    (rowid, serialize_vector(vec))
                )
                reindexed += 1
            except Exception:
                pass
        conn.commit()
        offset += batch_size


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main():
    _reconfigure_utf8()

    if len(sys.argv) < 2:
        print("Usage: python n3memory.py [--buffer TEXT] [--search QUERY] [--repair] [--list] [--stop] [--hook-submit] [--run-server]")
        return

    cmd = sys.argv[1]

    if cmd == "--run-server":
        run_server()
        return

    config = _load_config()

    # --stop and --hook-submit manage server themselves (or don't need it).
    # All other commands need the server running.
    if cmd not in ("--stop", "--hook-submit"):
        ensure_server(config)

    if cmd == "--buffer":
        agent_id = None
        if "--agent-id" in sys.argv:
            idx = sys.argv.index("--agent-id")
            if idx + 1 < len(sys.argv):
                agent_id = sys.argv[idx + 1]

        if len(sys.argv) > 2 and sys.argv[2] == "-":
            text = sys.stdin.read()
        elif len(sys.argv) > 2 and not sys.argv[2].startswith("--"):
            text = sys.argv[2]
        else:
            text = sys.stdin.read()

        if text.strip():
            cmd_buffer(text, config, agent_id=agent_id)

    elif cmd == "--search":
        if len(sys.argv) > 2:
            query = " ".join(a for a in sys.argv[2:] if not a.startswith("--"))
        else:
            query = sys.stdin.read().strip()
        if query:
            cmd_search(query, config)

    elif cmd == "--repair":
        cmd_repair(config)

    elif cmd == "--list":
        cmd_list(config)

    elif cmd == "--stop":
        cmd_stop(config)

    elif cmd == "--hook-submit":
        cmd_hook_submit(config)

    else:
        print(f"[N3MC] Unknown command: {cmd}", file=sys.stderr)


if __name__ == "__main__":
    main()
