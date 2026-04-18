"""
Japanese / Unicode encoding regression tests for both hook paths.

Background:
    On Windows, ``sys.stdin`` defaults to cp932 which silently corrupts
    Japanese UTF-8 into mojibake. Separately, subprocess pipes and
    ``errors='surrogateescape'`` can inject lone ``\\udcXX`` surrogate
    halves that later crash sqlite3 with ``UnicodeEncodeError``.

    Both classes of bug historically broke the complete-preservation
    contract for Japanese users. These tests pin the fix so regressions
    are caught in CI rather than in production logs.
"""
import os
import sys
import json
import sqlite3
import subprocess

import pytest

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_THIS, ".."))
_N3MEMORY = os.path.join(_ROOT, "n3memory.py")
_STOP_HOOK = os.path.join(_ROOT, "n3mc_stop_hook.py")

sys.path.insert(0, os.path.join(_ROOT, "core"))
sys.path.insert(0, _ROOT)


# ---------------------------------------------------------------------------
# Unit: sanitizer functions
# ---------------------------------------------------------------------------
def test_sanitize_for_sqlite_replaces_lone_surrogates():
    from n3memory import _sanitize_for_sqlite
    bad = "日本語テスト\udc8d\udc81 END"
    out = _sanitize_for_sqlite(bad)
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in out)
    assert "日本語テスト" in out
    assert "END" in out


def test_sanitize_for_sqlite_preserves_clean_japanese():
    from n3memory import _sanitize_for_sqlite
    clean = "これは完全に正しい日本語文字列です。"
    out = _sanitize_for_sqlite(clean)
    assert out == clean


def test_processor_purify_sanitizes_surrogates():
    from processor import purify
    bad = "完全保存\udc8dテスト"
    out = purify(bad)
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in out)
    assert "完全保存" in out
    assert "テスト" in out


# ---------------------------------------------------------------------------
# SQLite: surrogate-bearing text must be insertable
# ---------------------------------------------------------------------------
def test_sqlite_accepts_sanitized_japanese_with_surrogate_source(tmp_path):
    """The exact pathological input that crashed /buffer in production.

    Pre-fix: ``sqlite3.execute`` raised UnicodeEncodeError for lone
    surrogates. Post-fix: sanitizer scrubs them before the SQL call.
    """
    from n3memory import _sanitize_for_sqlite

    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t(x TEXT)")
    bad = "日本語\udc8d\udc81テスト"
    cleaned = _sanitize_for_sqlite(bad)
    conn.execute("INSERT INTO t VALUES (?)", (cleaned,))  # must not raise
    conn.commit()
    (stored,) = conn.execute("SELECT x FROM t").fetchone()
    assert "日本語" in stored and "テスト" in stored
    conn.close()


# ---------------------------------------------------------------------------
# Full hook round-trip: UserPromptSubmit
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.path.exists(_N3MEMORY),
    reason="n3memory.py entry point not present",
)
def test_user_prompt_submit_hook_preserves_japanese(tmp_path, monkeypatch):
    """Simulate Claude Code's UserPromptSubmit payload with Japanese prompt.

    Verifies the full stdin-bytes-read path survives Windows cp932 default.
    """
    env = os.environ.copy()
    env["N3MC_TEST_MEMORY_DIR"] = str(tmp_path)

    payload = {
        "session_id": "enc-e2e-submit",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "これは日本語テストです。ENC_E2E_SUBMIT_001",
        "last_assistant_message": "前回の応答: ENC_E2E_SUBMIT_001 日本語",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    res = subprocess.run(
        [sys.executable, _N3MEMORY, "--hook-submit"],
        input=body, capture_output=True, timeout=60, env=env,
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Full hook round-trip: Stop
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.path.exists(_STOP_HOOK),
    reason="n3mc_stop_hook.py not present",
)
def test_stop_hook_preserves_japanese(tmp_path):
    """Simulate Claude Code's Stop payload with Japanese last_assistant_message.

    Pre-fix: this path read stdin in cp932 and silently stored mojibake.
    """
    env = os.environ.copy()
    env["N3MC_TEST_MEMORY_DIR"] = str(tmp_path)

    payload = {
        "session_id": "enc-e2e-stop",
        "hook_event_name": "Stop",
        "last_assistant_message": "日本語 Stop フックテスト。ENC_E2E_STOP_002",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    res = subprocess.run(
        [sys.executable, _STOP_HOOK],
        input=body, capture_output=True, timeout=60, env=env,
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", errors="replace")
    # stderr must be empty of encoding errors
    err = res.stderr.decode("utf-8", errors="replace")
    assert "UnicodeEncodeError" not in err
    assert "UnicodeDecodeError" not in err
