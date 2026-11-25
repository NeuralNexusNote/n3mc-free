"""Layer 4: hook integration (chunking, images, complete recording, --stop). No model needed."""
import io
import json
import os
import sys

import pytest

from n3memorycore import n3memory
from n3memorycore.core.processor import chunk_text, add_chunk_prefixes, purify_text
from n3memorycore.core.database import get_connection, get_all_memories


def _contents():
    c = get_connection(n3memory.DB_PATH)
    try:
        return [r["content"] for r in get_all_memories(c)]
    finally:
        c.close()


def _hook_submit(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return n3memory.cmd_hook_submit()


def _save_turn(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return n3memory.cmd_save_claude_turn()


class TestChunkText:
    def test_short_text_single_record(self):
        assert chunk_text("short") == ["short"]

    def test_long_text_multiple_chunks(self):
        text = " ".join(f"Sentence number {i} is here." for i in range(60))
        chunks = chunk_text(text)
        assert len(chunks) > 1
        assert all(len(c) <= 400 for c in chunks)

    def test_prefixes_numbered(self):
        assert add_chunk_prefixes(["a", "b", "c"], "claude") == ["[claude 1/3] a", "[claude 2/3] b", "[claude 3/3] c"]

    def test_single_chunk_no_number(self):
        assert add_chunk_prefixes(["only"], "user") == ["[user] only"]

    def test_paragraph_split(self):
        p1, p2 = "A" * 300, "B" * 300
        chunks = chunk_text(p1 + "\n\n" + p2)
        assert chunks[0] == p1
        assert chunks[1].endswith(p2)

    def test_hard_window_fallback(self):
        text = "x" * 1000
        chunks = chunk_text(text)
        assert len(chunks) >= 3
        assert all(len(c) <= 400 for c in chunks)
        assert sum(c.count("x") for c in chunks) >= 1000


class TestStripImages:
    def test_plain_text_unchanged(self):
        assert n3memory._strip_images("hello") == "hello"

    def test_base64_image_stripped(self):
        payload = [{"type": "text", "text": "look"},
                   {"type": "image", "source": {"type": "base64", "data": "iVBORw0KGgo="}}]
        assert n3memory._strip_images(payload) == "look"

    def test_image_only_payload_returns_empty(self):
        assert n3memory._strip_images([{"type": "image", "source": {"data": "xx"}}]) == ""


class TestExtractText:
    def test_plain_string(self):
        assert n3memory._extract_text("hello") == "hello"

    def test_multimodal_json_list(self):
        s = json.dumps([{"type": "text", "text": "what is this"}, {"type": "image", "source": {}}])
        assert n3memory._extract_text(s) == "what is this"

    def test_image_only_empty(self):
        assert n3memory._extract_text(json.dumps([{"type": "image", "source": {}}])) == ""

    def test_empty_input(self):
        assert n3memory._extract_text("") == ""
        assert n3memory._extract_text(None) == ""

    def test_json_text_is_kept_as_typed(self):
        for s in ("[1, 2, 3]", '["apple", "banana"]', "[]", '[{"id": 1}]'):
            assert n3memory._extract_text(s) == s


class TestCompleteRecording:
    def test_short_claude_response_saved(self, monkeypatch, capsys):
        assert _save_turn(monkeypatch, {"session_id": "s", "last_assistant_message": "OK"}) == 0
        assert "[claude] OK" in _contents()

    def test_short_user_message_saved(self, monkeypatch, capsys):
        _hook_submit(monkeypatch, {"session_id": "s", "prompt": "hello"})
        assert "[user] hello" in _contents()

    def test_skip_pattern_abolished(self, monkeypatch, capsys):
        for word in ("ok", "yes", "thanks"):
            _hook_submit(monkeypatch, {"session_id": "s", "prompt": word})
        contents = _contents()
        for word in ("ok", "yes", "thanks"):
            assert f"[user] {word}" in contents

    def test_code_block_replaced_in_purify(self, monkeypatch, capsys):
        _save_turn(monkeypatch, {"session_id": "s",
                                 "last_assistant_message": "Here:\n```bash\nls -la\n```\nDone."})
        assert "[claude] Here:\n[code omitted]\nDone." in _contents()

    def test_inline_code_preserved_in_purify(self, monkeypatch, capsys):
        _save_turn(monkeypatch, {"session_id": "s", "last_assistant_message": "Run `n3mc --list` to see."})
        assert "[claude] Run `n3mc --list` to see." in _contents()


class TestStopIdempotency:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        p = tmp_path / "project"
        (p / ".claude").mkdir(parents=True)
        monkeypatch.chdir(p)
        return p

    def test_no_import_added(self, project):
        claude_md = project / ".claude" / "CLAUDE.md"
        claude_md.write_text("# My project\n", encoding="utf-8")
        n3memory.cmd_stop()
        n3memory.cmd_stop()
        assert claude_md.read_text(encoding="utf-8") == "# My project\n"

    def test_rules_file_created(self, project):
        n3memory.cmd_stop()
        rules = project / ".claude" / "rules" / "n3mc-behavior.md"
        assert rules.read_text(encoding="utf-8").splitlines()[0] == "<!-- n3mc-behavior v3 -->"
        n3memory.cmd_stop()
        assert rules.read_text(encoding="utf-8") == n3memory._BEHAVIOR_MD_CONTENT

    def test_home_folder_untouched(self, isolate, monkeypatch):
        home = isolate["home"]
        (home / ".claude").mkdir()
        claude_md = home / ".claude" / "CLAUDE.md"
        claude_md.write_bytes(b"@" + str(isolate["mem"] / "memory_context.md").encode() + b"\n")
        before = claude_md.read_bytes()
        monkeypatch.chdir(home)
        assert n3memory.cmd_stop() == 0
        assert not (home / ".claude" / "rules").exists()
        assert claude_md.read_bytes() == before

    def test_project_dir_from_environment(self, project, tmp_path, monkeypatch, isolate):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other).replace("\\", "/"))
        n3memory.cmd_stop()
        assert (other / ".claude" / "rules" / "n3mc-behavior.md").exists()
        assert not (project / ".claude" / "rules").exists()
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        n3memory.cmd_stop()
        assert (project / ".claude" / "rules" / "n3mc-behavior.md").exists()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(isolate["home"]))
        n3memory.cmd_stop()
        assert not (isolate["home"] / ".claude").exists()
