"""Encoding regression suite (CHANGELOG v1.2.1, restored in v1.3.0).

Pins the Windows / Japanese fixes:
  - lone UTF-16 surrogate stripping (sqlite3 binding crash)
  - clean Japanese passthrough (no false positives)
  - SQLite accepts the sanitized output
  - Mojibake heuristic + cp932 -> utf-8 recovery
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from n3memorycore.core import database as db
from n3memorycore.core import processor as proc
from n3memorycore import n3memory as nm


# --------------------------------------------------------------------------- 1
class TestSanitizerUnit:
    def test_strips_lone_high_surrogate(self):
        out = proc.sanitize_surrogates("hello \uD83D world")
        assert "\uD83D" not in out
        assert "hello" in out and "world" in out

    def test_strips_lone_low_surrogate(self):
        out = proc.sanitize_surrogates("\uDCFFstart end\uDCFE")
        assert "\uDCFF" not in out
        assert "\uDCFE" not in out
        assert "start end" in out

    def test_clean_japanese_unchanged(self):
        text = "こんにちは、世界。"
        assert proc.sanitize_surrogates(text) == text

    def test_complete_emoji_pair_preserved(self):
        # A complete emoji is a high+low surrogate pair in UTF-16, but in
        # Python str it is a single non-surrogate codepoint, so it must pass.
        text = "smile 😀 here"
        assert proc.sanitize_surrogates(text) == text

    def test_recursive_dict(self):
        payload = {"k": "x\uD83Dy", "nested": {"k2": "\uDCFFa"}}
        out = proc.sanitize_surrogates(payload)
        assert out == {"k": "xy", "nested": {"k2": "a"}}

    def test_recursive_list(self):
        payload = ["\uD83Da", "ok", {"k": "b\uDCFE"}]
        out = proc.sanitize_surrogates(payload)
        assert out == ["a", "ok", {"k": "b"}]

    def test_none_passthrough(self):
        assert proc.sanitize_surrogates(None) is None


# --------------------------------------------------------------------------- 2
class TestSanitizedAcceptedBySQLite:
    def test_sanitized_text_inserts_without_unicode_error(self, tmp_path):
        # Without sanitization, SQLite would raise UnicodeEncodeError.
        dirty = "data \uD83D corrupted"
        clean = proc.sanitize_surrogates(dirty)

        conn = db.get_connection(str(tmp_path / "t.db"))
        db.init_db(conn)
        db.insert_memory(
            conn,
            id=str(uuid.uuid4()),
            content=clean,
            timestamp=datetime.now(timezone.utc).isoformat(),
            owner_id=str(uuid.uuid4()),
        )
        row = conn.execute("SELECT content FROM memories").fetchone()
        assert row[0] == "data  corrupted"
        conn.close()


# --------------------------------------------------------------------------- 3
class TestPurifyAppliesSanitization:
    def test_purify_strips_surrogates_alongside_code_blocks(self):
        text = "before \uD83D after\n```\ncode\n```"
        out = proc.purify_text(text)
        assert "\uD83D" not in out
        assert "[code omitted]" in out

    def test_purify_handles_empty(self):
        assert proc.purify_text("") == ""
        assert proc.purify_text(None) is None


# --------------------------------------------------------------------------- 4
class TestMojibakeHeuristic:
    def test_clean_japanese_not_flagged(self):
        assert nm._looks_mojibake("これはテストです") is False

    def test_classic_mojibake_flagged(self):
        # 「こんにちは」 mis-decoded as cp932
        text = "縺薙ｓ縺ｫ縺｡縺ｯ"
        assert nm._looks_mojibake(text) is True

    def test_short_text_not_flagged(self):
        # Below the 2-hint threshold.
        assert nm._looks_mojibake("縺a") is False
        assert nm._looks_mojibake("normal text") is False


# --------------------------------------------------------------------------- 5
class TestMojibakeRecoveryRoundtrip:
    def test_recovery_returns_recovered_text(self):
        # 「こんにちは」 round-tripped through cp932 misdecoding.
        original = "こんにちは"
        mojibake = original.encode("utf-8").decode("cp932", errors="replace")
        # Sanity: mojibake should match the heuristic
        assert nm._looks_mojibake(mojibake)
        recovered = nm._try_recover_mojibake(mojibake)
        assert recovered is not None
        assert original in recovered

    def test_recovery_skips_clean_text(self):
        assert nm._try_recover_mojibake("クリーンな日本語") is None

    def test_recovery_skips_english(self):
        assert nm._try_recover_mojibake("hello world") is None
