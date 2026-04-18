#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N3MemoryCore Japanese / encoding smoke test.

Targets the Windows cp932 regression classes that historically broke the
complete-preservation contract:

  1. Lone Unicode surrogates from subprocess pipes crashing sqlite3.
  2. cp932-misdecoded UTF-8 producing mojibake in the DB.

Usage
-----
    python scripts/smoke_ja.py            # full smoke
    python scripts/smoke_ja.py --quick    # fast (<3s): health + buffer only
                                          # intended for SessionStart hook

Exit codes
----------
    0 = all checks passed
    1 = one or more checks failed (details printed to stdout, UTF-8 safe)
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Force UTF-8 stdout/stderr so Japanese error messages render correctly on
# Windows consoles that default to cp932.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STOP_HOOK = os.path.join(ROOT, "n3mc_stop_hook.py")
N3MEMORY = os.path.join(ROOT, "n3memory.py")
TESTS_DIR = os.path.join(ROOT, "tests")


def _load_cfg():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _server_url():
    cfg = _load_cfg()
    host = cfg.get("server_host", "127.0.0.1")
    port = cfg.get("server_port", 18520)
    return f"http://{host}:{port}"


def _http_get(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


def _http_post(url, body_dict, timeout=10):
    data = json.dumps(body_dict, ensure_ascii=False).encode(
        "utf-8", errors="surrogatepass"
    )
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_health():
    url = _server_url() + "/health"
    t0 = time.perf_counter()
    try:
        status, body = _http_get(url, timeout=2)
    except Exception as e:
        return False, f"{url} unreachable: {e}"
    dt = (time.perf_counter() - t0) * 1000
    ok = status == 200 and "ok" in body
    return ok, f"{url} -> {status} ({dt:.0f}ms)"


def check_buffer_roundtrip():
    """POST Japanese content then /search — marker must come back intact."""
    marker = f"SMOKE_JA_{int(time.time())}"
    content = f"日本語スモーク {marker} エンコーディング確認"
    try:
        s1, b1 = _http_post(
            _server_url() + "/buffer",
            {"content": content, "agent_name": "smoke-ja"},
            timeout=10,
        )
        if s1 != 200:
            return False, f"/buffer HTTP {s1}: {b1[:200]}"

        s2, b2 = _http_post(
            _server_url() + "/search",
            {"query": marker, "k": 5},
            timeout=10,
        )
        if s2 != 200:
            return False, f"/search HTTP {s2}: {b2[:200]}"
        if marker not in b2:
            return False, f"/search did not return marker {marker}: {b2[:200]}"
        return True, f"/buffer -> /search roundtrip OK (marker={marker})"
    except Exception as e:
        return False, f"buffer roundtrip error: {e}"


def check_stop_hook_surrogate():
    """Feed lone surrogates into Stop hook stdin. Must exit 0 without Unicode errors."""
    if not os.path.exists(STOP_HOOK):
        return True, f"(skip) stop hook not present: {STOP_HOOK}"

    payload = {
        "session_id": "smoke-ja-surrogate",
        "hook_event_name": "Stop",
        "last_assistant_message": "サロゲート混入テスト\udc8d\udc81 修正完了 SMOKE_SUR",
    }
    # surrogatepass lets us emit the exact WTF-8 bytes that a misbehaving
    # subprocess pipe would hand us.
    body = json.dumps(payload, ensure_ascii=False).encode(
        "utf-8", errors="surrogatepass"
    )
    try:
        res = subprocess.run(
            [sys.executable, STOP_HOOK],
            input=body,
            capture_output=True,
            timeout=30,
        )
    except Exception as e:
        return False, f"stop hook invocation error: {e}"

    err = res.stderr.decode("utf-8", errors="replace")
    if res.returncode != 0:
        return False, f"stop hook rc={res.returncode}: {err[:300]}"
    if "UnicodeEncodeError" in err or "UnicodeDecodeError" in err:
        return False, f"stop hook raised Unicode error: {err[:300]}"
    return True, "stop hook accepted surrogate payload cleanly"


def check_pytest():
    """Run tests/test_hooks_encoding.py if available."""
    test_file = os.path.join(TESTS_DIR, "test_hooks_encoding.py")
    if not os.path.exists(test_file):
        return True, f"(skip) {test_file} absent"
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "-q", "--no-header"],
            capture_output=True,
            timeout=180,
            cwd=ROOT,
        )
    except Exception as e:
        return False, f"pytest invocation error: {e}"

    out = (res.stdout + res.stderr).decode("utf-8", errors="replace")
    if res.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-10:])
        return False, f"pytest failed:\n{tail}"
    last = out.strip().splitlines()[-1] if out.strip() else ""
    return True, f"pytest OK — {last}"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--quick",
        action="store_true",
        help="Fast mode: only /health + /buffer roundtrip (<3s). "
        "Intended for SessionStart hook.",
    )
    args = ap.parse_args()

    if args.quick:
        checks = [
            ("health", check_health),
            ("buffer roundtrip", check_buffer_roundtrip),
        ]
    else:
        checks = [
            ("health", check_health),
            ("buffer roundtrip", check_buffer_roundtrip),
            ("stop hook surrogate", check_stop_hook_surrogate),
            ("pytest encoding", check_pytest),
        ]

    t0 = time.perf_counter()
    failures = 0
    for name, fn in checks:
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"unhandled: {e}"
        tag = "[OK]  " if ok else "[FAIL]"
        print(f"{tag} {name:22s} {msg}")
        if not ok:
            failures += 1

    dt = (time.perf_counter() - t0) * 1000
    total = len(checks)
    status = "smoke_ja PASS" if failures == 0 else "smoke_ja FAIL"
    print(f"--- {status}: {total - failures}/{total} in {dt:.0f}ms ---")
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
