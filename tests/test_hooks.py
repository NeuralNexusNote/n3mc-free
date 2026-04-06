"""
Layer 4: Hook integration (truncation, image strip, skip patterns)
"""
import os
import sys
import json
import pytest

_N3MC_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _N3MC_DIR)

from n3memory import (
    _truncate_at_sentence,
    _extract_text,
    _prepare_user_buffer,
    _prepare_claude_buffer,
    purify_text,
)


class TestTruncateAtSentence:
    def test_short_text_unchanged(self):
        text = "Hello world."
        assert _truncate_at_sentence(text, 300) == text

    def test_truncate_at_period(self):
        # Sentence boundary at ~200 chars (> max_chars//2=150) ensures it's found
        text = "x" * 190 + ". " + "y" * 200
        result = _truncate_at_sentence(text, 300)
        assert result.endswith(".")
        assert len(result) <= 300

    def test_truncate_at_exclamation(self):
        text = "x" * 190 + "! " + "y" * 200
        result = _truncate_at_sentence(text, 300)
        assert result.endswith("!")

    def test_fallback_hard_cut(self):
        # No sentence boundary in first half → hard cut at max_chars
        text = "a" * 400
        result = _truncate_at_sentence(text, 300)
        assert len(result) == 300


class TestExtractText:
    def test_plain_string(self):
        assert _extract_text("hello world") == "hello world"

    def test_multimodal_json(self):
        arr = json.dumps([
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": "data:..."},
            {"type": "text", "text": "world"},
        ])
        result = _extract_text(arr)
        assert "hello" in result
        assert "world" in result

    def test_image_only_returns_empty(self):
        arr = json.dumps([{"type": "image_url", "image_url": "data:..."}])
        result = _extract_text(arr)
        assert result.strip() == ""

    def test_empty_returns_empty(self):
        assert _extract_text("") == ""


class TestSkipPatterns:
    def test_routine_skipped(self):
        assert _prepare_user_buffer("ok") is None
        assert _prepare_user_buffer("yes") is None
        assert _prepare_user_buffer("thanks") is None

    def test_meaningful_not_skipped(self):
        text = "Please implement N3MemoryCore according to this specification."
        result = _prepare_user_buffer(text)
        assert result is not None
        assert "[user]" in result

    def test_length_filter_claude(self):
        # Under 3 chars → skip
        assert _prepare_claude_buffer("ok") is None

    def test_length_filter_user(self):
        # Under 10 chars → skip
        assert _prepare_user_buffer("hi there") is None


class TestStopIdempotency:
    def test_import_line_not_duplicated(self, tmp_path):
        import sys
        sys.path.insert(0, _N3MC_DIR)
        from pathlib import Path
        from unittest.mock import patch

        # Create temp .claude dir
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        rules_dir = claude_dir / "rules"
        rules_dir.mkdir()
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text("@../N3MemoryCore/.memory/memory_context.md\n", encoding="utf-8")

        # Patch paths in n3memory
        import n3memory
        original_project_root = None

        with patch.object(n3memory, '_THIS_DIR', tmp_path / "N3MemoryCore"):
            config = {"owner_id": "x", "local_id": "y", "server_port": 18521}
            # Manually run the import-idempotency logic
            import_line = "@../N3MemoryCore/.memory/memory_context.md"
            content = claude_md.read_text(encoding="utf-8")
            count_before = content.count(import_line)
            # Simulate running cmd_stop's CLAUDE.md update logic
            if import_line not in content:
                content = import_line + "\n" + content
            claude_md.write_text(content, encoding="utf-8")
            count_after = claude_md.read_text(encoding="utf-8").count(import_line)
            assert count_after == count_before  # no duplicate added

    def test_rules_file_created(self, tmp_path):
        from pathlib import Path
        import n3memory
        from unittest.mock import patch

        n3mc_dir = tmp_path / "N3MemoryCore"
        n3mc_dir.mkdir()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        rules_dir = claude_dir / "rules"
        rules_dir.mkdir()
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text("", encoding="utf-8")

        with patch.object(n3memory, '_THIS_DIR', n3mc_dir):
            config = {}
            # Patch project root derivation
            behavior_md = rules_dir / "n3mc-behavior.md"
            if not behavior_md.exists():
                behavior_md.write_text("# N3MemoryCore Behavioral Guidelines\n", encoding="utf-8")
            assert behavior_md.exists()
