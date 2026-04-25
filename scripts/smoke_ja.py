#!/usr/bin/env python3
"""Japanese / encoding smoke test for N3MemoryCore.

Modes:
  --quick   <3s — /health probe + /buffer Japanese roundtrip via HTTP
  (no flag) full — adds: surrogate Stop-hook test + pytest encoding suite

Exit code 0 on full pass; 1 on any failure (with diagnostic to stderr).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# UTF-8 stdio (Windows cp932 mojibake guard)
for _stream_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

HERE = Path(__file__).resolve().parent
N3MC_ROOT = HERE.parent
N3MEMORY = N3MC_ROOT / "n3memory.py"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18520


def _server_url(port=DEFAULT_PORT, host=DEFAULT_HOST):
    return f"http://{host}:{port}"


def _ping_health(timeout=2.0):
    try:
        with urllib.request.urlopen(
            f"{_server_url()}/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _post(path, body, timeout=15.0):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{_server_url()}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ensure_server_running():
    """Spawn the server if /health doesn't answer."""
    if _ping_health():
        return True
    print("[smoke] server not running — spawning ...", file=sys.stderr)
    creationflags = 0
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, str(N3MEMORY), "--server"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    for _ in range(120):
        if _ping_health(timeout=0.3):
            return True
        time.sleep(0.5)
    return False


def quick():
    """3-second roundtrip: /health + /buffer Japanese + /search recall."""
    if not _ensure_server_running():
        print("[smoke] FAIL: server did not come up within 60s", file=sys.stderr)
        return 1
    if not _ping_health():
        print("[smoke] FAIL: /health did not return 200", file=sys.stderr)
        return 1
    print("[smoke] /health OK")

    marker = "smoke_ja_QUICK_" + str(int(time.time()))
    body = {"content": f"日本語スモーク: {marker} — em-dash 含む"}
    resp = _post("/buffer", body)
    if resp.get("status") != "ok":
        print(f"[smoke] FAIL: /buffer returned {resp}", file=sys.stderr)
        return 1
    print(f"[smoke] /buffer OK (count={resp.get('count')})")

    resp = _post("/search", {"query": marker})
    results = resp.get("results", [])
    hit = any(marker in (r.get("content") or "") for r in results)
    if not hit:
        print(f"[smoke] FAIL: /search did not recall marker {marker}",
              file=sys.stderr)
        print(f"  results = {results}", file=sys.stderr)
        return 1
    print(f"[smoke] /search OK (recalled marker, {len(results)} hit(s))")
    return 0


def stop_hook_surrogate_test():
    """Pipe a Stop-hook JSON containing a lone surrogate through n3mc_stop_hook.py
    and confirm the saved record has the surrogate stripped, Japanese preserved."""
    payload = {
        "session_id": "smoke-stop",
        "stop_hook_active": True,
        "last_assistant_message": "海底都市の応答\uD83Dを保存します。",
    }
    raw = json.dumps(payload, ensure_ascii=False)
    proc = subprocess.run(
        [sys.executable, str(N3MC_ROOT / "n3mc_stop_hook.py")],
        input=raw, text=True, encoding="utf-8", errors="replace",
        timeout=60, check=False,
    )
    if proc.returncode != 0:
        print(f"[smoke] FAIL: stop hook exit {proc.returncode}", file=sys.stderr)
        return 1
    # Search for the marker; surrogate must be stripped, Japanese intact.
    resp = _post("/search", {"query": "海底都市の応答"})
    hit = any(
        "海底都市の応答" in (r.get("content") or "") and "\uD83D" not in (r.get("content") or "")
        for r in resp.get("results", [])
    )
    if not hit:
        print("[smoke] FAIL: stop hook output missing or surrogate leaked",
              file=sys.stderr)
        return 1
    print("[smoke] stop hook surrogate roundtrip OK")
    return 0


def run_pytest_encoding():
    """Run pytest tests/test_hooks_encoding.py and surface its result."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_hooks_encoding.py", "-q"],
        cwd=str(N3MC_ROOT), text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode == 0:
        print("[smoke] pytest encoding suite OK")
    else:
        print("[smoke] FAIL: pytest encoding suite failed", file=sys.stderr)
    return proc.returncode


def full():
    rc = quick()
    if rc != 0:
        return rc
    rc = stop_hook_surrogate_test()
    if rc != 0:
        return rc
    rc = run_pytest_encoding()
    return rc


def main():
    p = argparse.ArgumentParser(prog="smoke_ja",
                                description="N3MemoryCore Japanese encoding smoke test")
    p.add_argument("--quick", action="store_true",
                   help="<3s roundtrip only (no Stop hook, no pytest)")
    args = p.parse_args()
    rc = quick() if args.quick else full()
    print("[smoke] PASS" if rc == 0 else "[smoke] FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
