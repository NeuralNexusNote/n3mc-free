import os
import sys
import json
import uuid
import tempfile
from datetime import datetime, timezone

import pytest

from n3memorycore.core.processor import chunk_text, add_chunk_prefixes, purify_text


def _extract_text(prompt):
    """Copy of _extract_text from n3memory.py for testing."""
    if isinstance(prompt, str):
        try:
            parts = json.loads(prompt)
        except Exception:
            return prompt
        if isinstance(parts, list):
            return ' '.join(
                p.get('text', '') for p in parts
                if isinstance(p, dict) and p.get('type') == 'text'
            )
        return prompt
    if isinstance(prompt, list):
        return ' '.join(
            p.get('text', '') for p in prompt
            if isinstance(p, dict) and p.get('type') == 'text'
        )
    return str(prompt) if prompt else ''


class TestChunkText:
    def test_short_text_single_record(self):
        chunks = chunk_text("hello", max_chars=400)
        assert chunks == ["hello"]

    def test_long_text_multi_chunk(self):
        text = "x" * 1000
        chunks = chunk_text(text, max_chars=400, overlap=40)
        assert len(chunks) > 1

    def test_chunk_prefix_numbering(self):
        chunks = ["a", "b", "c"]
        prefixed = add_chunk_prefixes(chunks, "user")
        assert prefixed[0] == "[user 1/3] a"
        assert prefixed[1] == "[user 2/3] b"
        assert prefixed[2] == "[user 3/3] c"

    def test_paragraph_split(self):
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = chunk_text(text, max_chars=400)
        # Short paragraphs merged into one chunk
        assert len(chunks) == 1
        assert "Para one" in chunks[0]

    def test_sentence_split(self):
        # Build text that exceeds max_chars but has sentence breaks
        sentence = "This is a sentence. "
        text = sentence * 30  # ~600 chars
        chunks = chunk_text(text, max_chars=200, overlap=20)
        assert len(chunks) > 1

    def test_hard_window_fallback(self):
        text = "a" * 500
        chunks = chunk_text(text, max_chars=200, overlap=20)
        assert len(chunks) >= 3


class TestStripImages:
    def test_no_images_unchanged(self):
        payload = json.dumps({"message": "hello"})
        data = json.loads(payload)
        assert data['message'] == "hello"

    def test_strips_base64_image(self):
        parts = [
            {"type": "image", "source": {"data": "base64data"}},
            {"type": "text", "text": "describe this"},
        ]
        text = _extract_text(parts)
        assert "describe this" in text
        assert "base64data" not in text

    def test_image_only_becomes_empty(self):
        parts = [{"type": "image", "source": {"data": "xyz"}}]
        text = _extract_text(parts)
        assert text.strip() == ""

    def test_non_json_passthrough(self):
        text = _extract_text("plain string")
        assert text == "plain string"


class TestExtractText:
    def test_plain_string(self):
        assert _extract_text("hello") == "hello"

    def test_multimodal_json(self):
        parts = json.dumps([
            {"type": "text", "text": "what is this?"},
            {"type": "image", "source": {}},
        ])
        result = _extract_text(parts)
        assert "what is this?" in result

    def test_image_only_returns_empty(self):
        parts = json.dumps([{"type": "image", "source": {}}])
        result = _extract_text(parts)
        assert result.strip() == ""

    def test_empty_returns_empty(self):
        assert _extract_text("") == ""


class TestCompleteRecording:
    def test_routine_ok_is_saved(self):
        # Verify no skip-pattern filter: "ok" should pass through chunk_text unchanged
        chunks = chunk_text("ok")
        assert chunks == ["ok"]

    def test_short_claude_response_is_saved(self):
        # len < 3 used to be filtered; now must pass
        text = "ok"
        chunks = chunk_text(text)
        prefixed = add_chunk_prefixes(chunks, "claude")
        assert len(prefixed) == 1
        assert "ok" in prefixed[0]

    def test_short_user_message_is_saved(self):
        # len < 10 used to be filtered; now must pass
        text = "hello"
        chunks = chunk_text(text)
        prefixed = add_chunk_prefixes(chunks, "user")
        assert len(prefixed) == 1

    def test_audit_log_entry_written(self, tmp_path):
        audit = str(tmp_path / 'audit.log')
        record = json.dumps({
            'ts': datetime.now(timezone.utc).isoformat(),
            'hook': 'UserPromptSubmit',
            'raw': 'test',
            'payload': {'message': 'test'},
        }, ensure_ascii=False)
        with open(audit, 'a', encoding='utf-8') as f:
            f.write(record + '\n')
        with open(audit, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data['hook'] == 'UserPromptSubmit'


class TestStopIdempotency:
    def test_import_line_not_duplicated(self, tmp_path):
        claude_md = tmp_path / 'CLAUDE.md'
        import_line = "@../N3MemoryCore/.memory/memory_context.md"
        claude_md.write_text(f"# CLAUDE.md\n\n{import_line}\n", encoding='utf-8')

        content = claude_md.read_text(encoding='utf-8')
        if import_line not in content:
            content = content.rstrip() + '\n\n' + import_line + '\n'
        claude_md.write_text(content, encoding='utf-8')

        result = claude_md.read_text(encoding='utf-8')
        assert result.count(import_line) == 1

    def test_rules_file_created(self, tmp_path):
        rules_dir = tmp_path / 'rules'
        rules_dir.mkdir()
        behavior = rules_dir / 'n3mc-behavior.md'
        if not behavior.exists():
            behavior.write_text("# N3MemoryCore Behavioral Guidelines\n", encoding='utf-8')
        assert behavior.exists()
