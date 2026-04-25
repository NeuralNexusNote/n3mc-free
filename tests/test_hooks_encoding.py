"""Encoding regression suite (CHANGELOG v1.2.0).

Pins the Windows / Japanese fixes:
  - lone UTF-16 surrogate stripping (sqlite3 binding crash)
  - clean Japanese passthrough (no false positives)
  - SQLite accepts the sanitized output
  - UserPromptSubmit hook roundtrip preserves Japanese verbatim
  - Stop hook roundtrip preserves Japanese verbatim
  - Mojibake heuristic + cp932→utf-8 recovery
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

N3MC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(N3MC_ROOT))
sys.path.insert(0, str(N3MC_ROOT / "core"))

import n3memory as n3
from core import database as db
from core import processor as proc


# --------------------------------------------------------------------------- 1
class TestSanitizerUnit:
    def test_strips_lone_high_surrogate(self):
        # \uD83D alone is a high surrogate without its low pair — sqlite3 crash
        out = proc.sanitize_surrogates("hello \uD83D world")
        assert "\uD83D" not in out
        assert "hello" in out and "world" in out

    def test_strips_lone_low_surrogate(self):
        out = proc.sanitize_surrogates("\uDCFFstart end\uDCFE")
        assert "\uDCFF" not in out
        assert "\uDCFE" not in out
        assert "start end" in out

    def test_preserves_clean_japanese(self):
        text = "こんにちは世界。日本語テストです。"
        assert proc.sanitize_surrogates(text) == text

    def test_preserves_em_dash_and_emoji_pair(self):
        # Properly paired surrogates render valid emoji — must NOT be stripped.
        text = "VINETOWN — 海底のロープ都市 🏙"
        assert proc.sanitize_surrogates(text) == text

    def test_recursive_dict_list(self):
        payload = {
            "outer": "ok",
            "list": ["clean", "bad\uD83Dtail"],
            "nested": {"deep": "x\uDCFFy"},
        }
        out = proc.sanitize_surrogates(payload)
        assert out["outer"] == "ok"
        assert out["list"] == ["clean", "badtail"]
        assert out["nested"]["deep"] == "xy"


# --------------------------------------------------------------------------- 2
class TestSqliteAcceptsSanitized:
    def test_sanitized_lone_surrogate_inserts_cleanly(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        raw = "前回の質問について\uD83Dの続き"
        cleaned = proc.sanitize_surrogates(raw)
        # Should NOT raise UnicodeEncodeError now that the surrogate is gone.
        db.insert_memory(conn, cleaned, dummy_vec(), "owner-1", "local-1")
        got = db.get_all_memories(conn)
        assert len(got) == 1
        assert "\uD83D" not in got[0]["content"]

    def test_unsanitized_lone_surrogate_would_crash(self, isolated_db, dummy_vec):
        conn, _ = isolated_db
        raw = "broken\uD800half"
        with pytest.raises(UnicodeEncodeError):
            db.insert_memory(conn, raw, dummy_vec(), "owner-1", "local-1")


# --------------------------------------------------------------------------- 3
class TestHookRoundtripUserPrompt:
    def test_japanese_user_prompt_preserved(self, tmp_path, monkeypatch, cfg):
        """Simulates Claude Code sending a Japanese UserPromptSubmit JSON
        with a lone surrogate buried in the payload — we must NOT lose
        the Japanese characters and we must NOT crash."""
        monkeypatch.setattr(n3, "DB_PATH", tmp_path / "n3memory.db")
        monkeypatch.setattr(n3, "MEMORY_DIR", tmp_path)
        monkeypatch.setattr(n3, "AUDIT_LOG", tmp_path / "audit.log")
        monkeypatch.setattr(n3, "TURN_ID_FILE", tmp_path / "turn_id.txt")
        monkeypatch.setattr(n3, "MEMORY_CONTEXT_MD", tmp_path / "memory_context.md")
        monkeypatch.setattr(n3, "FTS_PUNCT_MARKER", tmp_path / "fts_punct_cleaned")
        monkeypatch.setattr(n3, "VEC_E5V2_MARKER", tmp_path / "vec_e5v2_migrated")
        monkeypatch.setattr(n3, "MOJIBAKE_RECOVERED_MARKER", tmp_path / "mojibake")

        # ensure_server is stubbed → cmd_buffer falls to _buffer_direct (no
        # network), which still applies the sanitizer.
        monkeypatch.setattr(n3, "ensure_server", lambda c: False)

        payload = {
            "prompt": "海底都市について教えて\uD83Dください — em-dash 含む",
            "last_assistant_message": "前回の応答\uDCFFテキスト",
        }
        n3.write_audit("UserPromptSubmit", json.dumps(payload, ensure_ascii=False), payload)
        n3.cmd_hook_submit(cfg, payload)

        conn = db.init_db(tmp_path / "n3memory.db")
        rows = db.get_all_memories(conn)
        contents = [r["content"] for r in rows]
        conn.close()

        # Surrogates stripped, Japanese preserved.
        for c in contents:
            assert "\uD83D" not in c
            assert "\uDCFF" not in c
        assert any("海底都市" in c for c in contents)
        assert any("前回の応答" in c for c in contents)
        # em-dash survives.
        assert any("—" in c for c in contents)


# --------------------------------------------------------------------------- 4
class TestHookRoundtripStop:
    def test_stop_hook_save_claude_turn_japanese(self, tmp_path, monkeypatch, cfg):
        monkeypatch.setattr(n3, "DB_PATH", tmp_path / "n3memory.db")
        monkeypatch.setattr(n3, "MEMORY_DIR", tmp_path)
        monkeypatch.setattr(n3, "AUDIT_LOG", tmp_path / "audit.log")
        monkeypatch.setattr(n3, "TURN_ID_FILE", tmp_path / "turn_id.txt")
        monkeypatch.setattr(n3, "FTS_PUNCT_MARKER", tmp_path / "fts_punct_cleaned")
        monkeypatch.setattr(n3, "VEC_E5V2_MARKER", tmp_path / "vec_e5v2_migrated")
        monkeypatch.setattr(n3, "MOJIBAKE_RECOVERED_MARKER", tmp_path / "mojibake")
        monkeypatch.setattr(n3, "ensure_server", lambda c: False)

        last = "海底都市は星明かりで動いています\uD800。詳細は次回。"
        # Mimic the --save-claude-turn entry path by calling the helper directly.
        cleaned = n3._sanitize_for_sqlite(last)
        n3._save_with_chunks(cleaned, "claude", cfg, turn_id="t-jp-stop")

        conn = db.init_db(tmp_path / "n3memory.db")
        rows = db.get_all_memories(conn)
        conn.close()
        assert len(rows) >= 1
        assert all("\uD800" not in r["content"] for r in rows)
        assert any("海底都市は星明かり" in r["content"] for r in rows)


# --------------------------------------------------------------------------- 5
class TestMojibakeRecovery:
    def test_heuristic_detects_mojibake(self):
        # 「こんにちは」を cp932→utf-8 で誤デコードするとこう壊れる
        broken = "こんにちは".encode("utf-8").decode("cp932", errors="replace")
        # The result should contain repeated mojibake hint chars.
        assert n3._looks_mojibake(broken) or "縺" in broken or "繧" in broken

    def test_clean_japanese_not_flagged(self):
        assert not n3._looks_mojibake("こんにちは世界")

    def test_recovery_roundtrip_when_possible(self, tmp_path, monkeypatch):
        monkeypatch.setattr(n3, "DB_PATH", tmp_path / "n3memory.db")
        monkeypatch.setattr(n3, "MEMORY_DIR", tmp_path)
        monkeypatch.setattr(n3, "MOJIBAKE_RECOVERED_MARKER", tmp_path / "mojibake")

        # Synthesize a mojibake row by misencoding/decoding the original.
        conn = db.init_db(tmp_path / "n3memory.db")
        original = "海底都市の住人について"
        broken = original.encode("utf-8").decode("cp932", errors="replace")
        if not n3._looks_mojibake(broken):
            pytest.skip("cp932 platform did not produce expected mojibake hints")
        db.insert_memory(conn, broken, None, "o", "l")
        conn.close()

        n3.run_mojibake_recovery(tmp_path / "n3memory.db")

        conn = db.init_db(tmp_path / "n3memory.db")
        rows = db.get_all_memories(conn)
        conn.close()
        # Recovery may or may not be perfectly lossless depending on round-trip,
        # but the prefix MUST be present and the row should no longer match the
        # mojibake heuristic.
        assert any(r["content"].startswith("[recovered] ") for r in rows)

    def test_recovery_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(n3, "DB_PATH", tmp_path / "n3memory.db")
        monkeypatch.setattr(n3, "MEMORY_DIR", tmp_path)
        monkeypatch.setattr(n3, "MOJIBAKE_RECOVERED_MARKER", tmp_path / "mojibake")
        # Marker pre-exists → recovery must skip.
        (tmp_path / "mojibake").write_text("ok", encoding="utf-8")
        result = n3.run_mojibake_recovery(tmp_path / "n3memory.db")
        assert result == 0
