"""Layer 4: hook integration — complete recording, chunk, audit log (spec §7)."""
import json
import uuid
import os
import tempfile
import pytest

from n3memorycore.core.processor import (
    chunk_text, add_chunk_prefixes, purify_text, sanitize_surrogates,
)


# ---------------------------------------------------------------------------
class TestChunkText:

    def test_short_text_single_record(self):
        chunks = chunk_text('Hello world')
        assert len(chunks) == 1
        assert chunks[0] == 'Hello world'

    def test_long_text_multiple_chunks(self):
        text = 'A' * 1000
        chunks = chunk_text(text, max_chars=400, overlap=40)
        assert len(chunks) > 1
        # All content preserved (with overlap, total chars >= original)
        total = sum(len(c) for c in chunks)
        assert total >= len(text)

    def test_prefixes_numbered(self):
        chunks = ['chunk one', 'chunk two', 'chunk three']
        prefixed = add_chunk_prefixes(chunks, 'user')
        assert prefixed[0].startswith('[user 1/3]')
        assert prefixed[1].startswith('[user 2/3]')
        assert prefixed[2].startswith('[user 3/3]')

    def test_single_chunk_no_number(self):
        prefixed = add_chunk_prefixes(['only one'], 'claude')
        assert prefixed[0].startswith('[claude] ')

    def test_paragraph_split(self):
        text = ('First paragraph.\n\n' * 5).strip()
        chunks = chunk_text(text, max_chars=40, overlap=5)
        assert len(chunks) >= 2

    def test_hard_window_fallback(self):
        # No paragraph/sentence breaks → hard window
        # 800 chars with max=400, overlap=40 → [0:400], [360:760], [720:800] = 3 chunks
        text = 'A' * 800
        chunks = chunk_text(text, max_chars=400, overlap=40)
        assert len(chunks) >= 2
        assert len(chunks[0]) == 400


# ---------------------------------------------------------------------------
class TestStripImages:
    """_extract_text filters out image payloads, returning only text parts."""

    def test_plain_text_unchanged(self):
        from n3memorycore.n3memory import _extract_text
        assert _extract_text('hello world') == 'hello world'

    def test_base64_image_stripped(self):
        from n3memorycore.n3memory import _extract_text
        payload = json.dumps([
            {'type': 'text', 'text': 'describe the image'},
            {'type': 'image_url', 'url': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA'},
        ])
        result = _extract_text(payload)
        assert 'iVBORw0KGgoAAAANSUhEUgAA' not in result
        assert 'describe the image' in result

    def test_image_only_payload_returns_empty(self):
        from n3memorycore.n3memory import _extract_text
        payload = json.dumps([
            {'type': 'image_url', 'url': 'data:image/jpeg;base64,/9j/4AAQSkZJRg=='},
        ])
        result = _extract_text(payload)
        assert result.strip() == ''


# ---------------------------------------------------------------------------
class TestExtractText:

    def test_plain_string(self):
        from n3memorycore.n3memory import _extract_text
        assert _extract_text('hello') == 'hello'

    def test_multimodal_json_list(self):
        from n3memorycore.n3memory import _extract_text
        payload = json.dumps([
            {'type': 'text', 'text': 'user text'},
            {'type': 'image_url', 'url': 'data:...'},
        ])
        result = _extract_text(payload)
        assert result == 'user text'

    def test_image_only_empty(self):
        from n3memorycore.n3memory import _extract_text
        payload = json.dumps([{'type': 'image_url', 'url': 'data:...'}])
        result = _extract_text(payload)
        assert result.strip() == ''

    def test_empty_input(self):
        from n3memorycore.n3memory import _extract_text
        assert _extract_text('') == ''
        assert _extract_text(None) == ''


# ---------------------------------------------------------------------------
class TestCompleteRecording:
    """spec §5: no length filter, no skip-pattern filter."""

    def test_short_claude_response_saved(self, tmp_path):
        """'ok' (2 chars) must be saved — no 3-char filter."""
        db_path = str(tmp_path / 'cr.db')
        from n3memorycore.core.database import get_connection, init_db, count_memories, insert_memory
        conn = get_connection(db_path)
        init_db(conn)
        chunks = add_chunk_prefixes(['ok'], 'claude')
        for c in chunks:
            insert_memory(conn, str(uuid.uuid4()), c, '2026-01-01T00:00:00', owner_id='o')
        assert count_memories(conn) == 1
        conn.close()

    def test_short_user_message_saved(self, tmp_path):
        """5-char user message must be saved — no 10-char filter."""
        db_path = str(tmp_path / 'su.db')
        from n3memorycore.core.database import get_connection, init_db, count_memories, insert_memory
        conn = get_connection(db_path)
        init_db(conn)
        chunks = add_chunk_prefixes(['hello'], 'user')
        for c in chunks:
            insert_memory(conn, str(uuid.uuid4()), c, '2026-01-01T00:00:00', owner_id='o')
        assert count_memories(conn) == 1
        conn.close()

    def test_skip_pattern_abolished(self, tmp_path):
        """Words like 'ok', 'yes', 'thanks' must be saved."""
        db_path = str(tmp_path / 'sp.db')
        from n3memorycore.core.database import get_connection, init_db, count_memories, insert_memory
        conn = get_connection(db_path)
        init_db(conn)
        for word in ['ok', 'yes', 'thanks']:
            chunks = add_chunk_prefixes([word], 'user')
            for c in chunks:
                insert_memory(conn, str(uuid.uuid4()), c, '2026-01-01T00:00:00', owner_id='o')
        assert count_memories(conn) == 3
        conn.close()

    def test_code_block_replaced_in_purify(self):
        text = 'Context\n```python\nprint("x")\n```\nDone'
        result = purify_text(text)
        assert '[code omitted]' in result
        assert 'print' not in result

    def test_inline_code_preserved_in_purify(self):
        text = 'Use `x = 1` here'
        result = purify_text(text)
        assert '`x = 1`' in result


# ---------------------------------------------------------------------------
class TestStopIdempotency:
    """spec §4: --stop @import and behavior.md must be idempotent."""

    def test_import_not_duplicated(self, tmp_path, monkeypatch):
        from n3memorycore import n3memory
        from n3memorycore.paths import CONTEXT_FILE

        claude_dir = tmp_path / '.claude'
        claude_dir.mkdir()
        rules_dir = claude_dir / 'rules'
        rules_dir.mkdir()
        claude_md = claude_dir / 'CLAUDE.md'

        # Patch claude_paths to use tmp
        monkeypatch.setattr(
            n3memory, 'claude_paths',
            lambda cwd=None: {
                'CLAUDE_DIR':  str(claude_dir),
                'CLAUDE_MD':   str(claude_md),
                'RULES_DIR':   str(rules_dir),
                'BEHAVIOR_MD': str(rules_dir / 'n3mc-behavior.md'),
            }
        )

        cfg = {}
        n3memory.cmd_stop(cfg)
        n3memory.cmd_stop(cfg)  # second call must be idempotent

        content = claude_md.read_text(encoding='utf-8')
        import_line = f"@{CONTEXT_FILE}"
        assert content.count(import_line) == 1

    def test_rules_file_created(self, tmp_path, monkeypatch):
        from n3memorycore import n3memory

        claude_dir = tmp_path / '.claude'
        claude_dir.mkdir()
        rules_dir = claude_dir / 'rules'
        rules_dir.mkdir()
        behavior_md = rules_dir / 'n3mc-behavior.md'

        monkeypatch.setattr(
            n3memory, 'claude_paths',
            lambda cwd=None: {
                'CLAUDE_DIR':  str(claude_dir),
                'CLAUDE_MD':   str(claude_dir / 'CLAUDE.md'),
                'RULES_DIR':   str(rules_dir),
                'BEHAVIOR_MD': str(behavior_md),
            }
        )

        n3memory.cmd_stop({})
        assert behavior_md.exists()
