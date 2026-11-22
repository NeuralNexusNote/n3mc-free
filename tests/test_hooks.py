"""Layer 4: hook-side logic (chunking, images, complete recording, --stop)."""
import io
import json
import os
import sys
import time

from n3memorycore import n3memory as n3m
from n3memorycore.core import database as db
from n3memorycore.core import processor as p


class TestChunkText:
    def test_short_text_single_record(self):
        assert p.chunk_text("short") == ["short"]

    def test_long_text_multiple_chunks(self):
        chunks = p.chunk_text("word " * 400)
        assert len(chunks) > 1 and all(len(c) <= 400 for c in chunks)

    def test_prefixes_numbered(self):
        assert p.add_chunk_prefixes("user", ["a", "b"]) == ["[user 1/2] a", "[user 2/2] b"]

    def test_single_chunk_no_number(self):
        assert p.add_chunk_prefixes("claude", ["a"]) == ["[claude] a"]

    def test_paragraph_split(self):
        text = ("A" * 300) + "\n\n" + ("B" * 300)
        chunks = p.chunk_text(text)
        assert len(chunks) == 2 and chunks[0] == "A" * 300 and "B" * 300 in chunks[1]

    def test_hard_window_fallback(self):
        chunks = p.chunk_text("x" * 1000)
        assert len(chunks) >= 3 and all(len(c) <= 400 for c in chunks)
        assert "".join(c[:360] if i < len(chunks) - 1 else c for i, c in enumerate(chunks)).count("x") >= 1000


class TestStripImages:
    def test_plain_text_unchanged(self):
        assert n3m._extract_text("hello") == "hello"

    def test_base64_image_stripped(self):
        parts = [{"type": "text", "text": "look"}, {"type": "image", "source": {"data": "AAAA"}}]
        assert n3m._extract_text(json.dumps(parts)) == "look"

    def test_image_only_payload_returns_empty(self):
        assert n3m._extract_text(json.dumps([{"type": "image", "source": {}}])) == ""


class TestExtractText:
    def test_plain_string(self):
        assert n3m._extract_text("abc") == "abc"

    def test_multimodal_json_list(self):
        assert n3m._extract_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"

    def test_image_only_empty(self):
        assert n3m._extract_text([{"type": "image"}]) == ""

    def test_empty_input(self):
        assert n3m._extract_text("") == "" and n3m._extract_text(None) == ""

    def test_json_text_is_kept_as_typed(self):
        for s in ("[1, 2, 3]", '["apple", "banana"]', "[]", '[{"id": 1}]'):
            assert n3m._extract_text(s) == s


def _run_hook(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")),
                                                       encoding="utf-8"))
    return n3m.cmd_hook_submit()


def _contents(conn):
    return [r[0] for r in conn.execute("SELECT content FROM memories ORDER BY rowid")]


class TestCompleteRecording:
    def test_short_claude_response_saved(self, conn, capsys):
        n3m._save_text("claude", "ok", "T1")
        assert "[claude] ok" in _contents(conn)

    def test_short_user_message_saved(self, conn):
        n3m._save_text("user", "hi", "T1")
        assert "[user] hi" in _contents(conn)

    def test_skip_pattern_abolished(self, conn, monkeypatch, capsys):
        for word in ("ok", "yes", "thanks"):
            _run_hook(monkeypatch, {"session_id": "s", "prompt": word})
        c = _contents(conn)
        assert all(f"[user] {w}" in c for w in ("ok", "yes", "thanks"))

    def test_code_block_replaced_in_purify(self):
        assert p.purify_text("a\n```\nx\ny\n```") == "a\n[code omitted]"

    def test_inline_code_preserved_in_purify(self):
        assert p.purify_text("run `ls -la` now") == "run `ls -la` now"

    def test_audit_is_written_by_the_hook_entry(self, monkeypatch):
        from n3memorycore import n3mc_hook
        n3mc_hook.write_audit("UserPromptSubmit", json.dumps({"prompt": "x" * 5000}))
        rec = json.loads(open(n3mc_hook.AUDIT_LOG, encoding="utf-8").read().splitlines()[-1])
        assert rec["hook"] == "UserPromptSubmit" and len(rec["raw"]) == 4096 and len(rec["payload"]["prompt"]) == 5000

    def test_long_reply_keeps_every_chunk_and_a_changed_reply_is_not_deduplicated(self, conn):
        base = "\n".join(f"項目{i}: 設定値は{i * 7}で、説明文がここに入ります。" * 6 for i in range(12))
        changed = base.replace("設定値は7", "設定値は9")
        n3m._save_text("claude", base, "TA")
        n_first = db.count_memories(conn)
        n3m._save_text("claude", changed, "TB")
        assert n_first > 3 and db.count_memories(conn) > n_first
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE turn_id='TA'").fetchone()[0] == n_first


class TestStopIdempotency:
    def test_no_import_added(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert n3m.cmd_stop() == 0
        assert not (tmp_path / ".claude" / "CLAUDE.md").exists()

    def test_home_folder_untouched(self, tmp_path, monkeypatch):
        """With the current folder = the home folder, --stop creates nothing in .claude/rules/ and leaves
        .claude/CLAUDE.md byte for byte (2026-11-22); the housekeeping still runs."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        claude_md = home / ".claude" / "CLAUDE.md"
        import_line = f"@{n3m.CONTEXT_FILE}\n"
        claude_md.write_bytes(("# mine\r\n" + import_line).encode("utf-8"))
        before = claude_md.read_bytes()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.chdir(home)
        old = os.path.join(n3m.MEMORY_DIR, "turn_id_old.txt")
        open(old, "w").write("x")
        t = time.time() - 31 * 86400
        os.utime(old, (t, t))
        assert n3m._cwd_is_home()
        assert n3m.cmd_stop() == 0
        assert not (home / ".claude" / "rules").exists()
        assert claude_md.read_bytes() == before
        assert not os.path.exists(old)
        monkeypatch.chdir(tmp_path)  # any other folder: the rules file is written as before
        n3m.cmd_stop()
        assert (tmp_path / ".claude" / "rules" / "n3mc-behavior.md").exists()

    def test_rules_file_created(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        n3m.cmd_stop()
        text = (tmp_path / ".claude" / "rules" / "n3mc-behavior.md").read_text(encoding="utf-8")
        assert text.splitlines()[0] == "<!-- n3mc-behavior v3 -->" and "## Weak Matches" in text
