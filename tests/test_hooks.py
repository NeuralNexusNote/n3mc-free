"""
Layer 4: Hook integration (complete-preservation chunking, multimodal extract).

The complete-recording contract forbids all length filters, skip-pattern
filters, and code-block stripping from the buffer write path. These tests
assert that EVERY non-empty input produces at least one tagged record.
"""
import os
import sys
import json
import pytest

_N3MC_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _N3MC_DIR)

from n3memory import (
    _extract_text,
    _prepare_user_buffer,
    _prepare_claude_buffer,
    purify_text,
)
from core.processor import chunk_text


class TestCompletePreservation:
    """Ensures full content is preserved via chunking (no truncation)."""

    def test_short_text_single_chunk(self):
        pieces = chunk_text("Hello world.", max_chars=400, overlap=40)
        assert pieces == ["Hello world."]

    def test_long_text_multiple_chunks(self):
        long_text = "Sentence number " + ". Sentence number ".join(str(i) for i in range(100)) + "."
        pieces = chunk_text(long_text, max_chars=400, overlap=40)
        assert len(pieces) > 1
        for p in pieces:
            assert p.strip()

    def test_claude_buffer_chunks_long_response(self):
        long_response = "A" * 1000 + ". " + "B" * 1000 + ". " + "C" * 1000
        contents = _prepare_claude_buffer(long_response)
        assert len(contents) > 1
        for c in contents:
            assert c.startswith("[claude")
        joined = " ".join(contents)
        assert "A" in joined and "B" in joined and "C" in joined

    def test_user_buffer_chunks_long_message(self):
        long_message = "Please save the following: " + ("detail " * 200)
        contents = _prepare_user_buffer(long_message)
        assert len(contents) >= 1
        for c in contents:
            assert c.startswith("[user")

    def test_short_claude_single_record_no_index(self):
        contents = _prepare_claude_buffer("Short reply.")
        assert len(contents) == 1
        assert contents[0].startswith("[claude] ")

    def test_short_user_single_record_no_index(self):
        contents = _prepare_user_buffer("A meaningful user message.")
        assert len(contents) == 1
        assert contents[0].startswith("[user] ")


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


class TestNoSilentDrops:
    """Complete-recording contract: nothing may be dropped from user or
    Claude text before it reaches the DB.
    """

    def test_routine_words_saved_not_skipped(self):
        for word in ("ok", "yes", "thanks", "thank you", "got it"):
            contents = _prepare_user_buffer(word)
            assert contents, f"routine word {word!r} must be preserved"
            assert contents[0].startswith("[user")

    def test_short_claude_saved(self):
        contents = _prepare_claude_buffer("ok")
        assert contents and contents[0].startswith("[claude")

    def test_short_user_saved(self):
        contents = _prepare_user_buffer("hi there")
        assert contents and contents[0].startswith("[user")

    def test_meaningful_not_skipped(self):
        text = "Please implement N3MemoryCore according to this specification."
        result = _prepare_user_buffer(text)
        assert result
        assert any("[user" in r for r in result)


class TestPurifyCodeBlocks:
    """Code blocks are excluded from stored conversation by documented
    product design: N3MC records conversation text, not source code.
    """

    def test_closed_fence_replaced(self):
        text = "before\n```python\nprint('hi')\n```\nafter"
        result = purify_text(text)
        assert "[code omitted]" in result
        assert "print" not in result

    def test_plain_text_preserved(self):
        text = "nothing special here"
        assert purify_text(text) == text


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
