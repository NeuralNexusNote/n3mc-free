"""Layer 4: hook helpers — chunking labels, image strip, multimodal extract,
complete-recording (no length filter), --stop idempotency."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure n3memory is importable
N3MC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(N3MC_ROOT))

import n3memory as n3
from core import processor as proc


class TestChunkText:
    def test_short_text_single_record(self):
        chunks = proc.chunk_text("hello")
        assert chunks == ["hello"]

    def test_long_text_multi_chunk(self):
        text = ("paragraph one. " * 50) + "\n\n" + ("paragraph two. " * 50)
        chunks = proc.chunk_text(text)
        assert len(chunks) >= 2

    def test_chunk_prefix_numbering(self):
        text = "x" * 1200
        labelled = n3._label_chunks(text, "user")
        assert all(c.startswith("[user ") for c in labelled)
        assert "1/" in labelled[0]

    def test_single_chunk_label(self):
        labelled = n3._label_chunks("short", "claude")
        assert labelled == ["[claude] short"]


class TestStripImages:
    def test_no_images_unchanged(self):
        assert n3.strip_images("plain text") == "plain text"

    def test_strips_base64_image(self):
        s = "before data:image/png;base64,iVBORw0KGgo= after"
        out = n3.strip_images(s)
        assert "iVBORw0KGgo" not in out
        assert "[image omitted]" in out

    def test_strip_in_dict(self):
        d = {"text": "x", "img": "data:image/jpeg;base64,AAAA="}
        out = n3.strip_images(d)
        assert "[image omitted]" in out["img"]


class TestExtractText:
    def test_plain_string(self):
        assert n3.extract_text("hello") == "hello"

    def test_multimodal_json(self):
        msg = [
            {"type": "image", "source": "x"},
            {"type": "text", "text": "describe this"},
        ]
        assert n3.extract_text(msg) == "describe this"

    def test_image_only_returns_empty(self):
        msg = [{"type": "image", "source": "x"}]
        assert n3.extract_text(msg) == ""

    def test_empty_returns_empty(self):
        assert n3.extract_text(None) == ""
        assert n3.extract_text("") == ""
        assert n3.extract_text([]) == ""


class TestCompleteRecording:
    """Verify the spec §5 Complete-Recording Contract: no length / skip filter."""

    def test_routine_ok_is_recorded(self):
        # The hook layer should accept 'ok' / 'yes' / 'thanks' — there is
        # no _SKIP_PATTERNS filter. We exercise this via _label_chunks.
        labelled = n3._label_chunks("ok", "user")
        assert labelled == ["[user] ok"]

    def test_short_message_is_chunked(self):
        labelled = n3._label_chunks("hi", "claude")
        assert labelled == ["[claude] hi"]

    def test_audit_log_entry_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(n3, "AUDIT_LOG", tmp_path / "audit.log")
        monkeypatch.setattr(n3, "MEMORY_DIR", tmp_path)
        n3.write_audit("UserPromptSubmit", '{"x":1}', {"x": 1})
        assert (tmp_path / "audit.log").exists()
        line = (tmp_path / "audit.log").read_text(encoding="utf-8").strip()
        rec = json.loads(line)
        assert rec["hook"] == "UserPromptSubmit"
        assert rec["payload"] == {"x": 1}


class TestStopIdempotency:
    def test_at_import_not_duplicated(self, tmp_path, monkeypatch):
        # Redirect MEMORY_CONTEXT_MD so the @import line points to a tmp path.
        monkeypatch.setattr(n3, "MEMORY_CONTEXT_MD", tmp_path / ".memory" / "memory_context.md")
        project = tmp_path / "proj"
        project.mkdir()
        n3._ensure_at_import(project)
        n3._ensure_at_import(project)
        text = (project / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        # Exactly one '@' line referencing memory_context
        count = sum(1 for ln in text.splitlines()
                    if ln.startswith("@") and "memory_context.md" in ln)
        assert count == 1

    def test_rules_file_created(self, tmp_path):
        n3._ensure_behavior_file(tmp_path)
        f = tmp_path / ".claude" / "rules" / "n3mc-behavior.md"
        assert f.exists()
        content = f.read_text(encoding="utf-8")
        assert "Fully Automatic Saving" in content

    def test_legacy_zone_removed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(n3, "MEMORY_CONTEXT_MD", tmp_path / ".memory" / "memory_context.md")
        project = tmp_path / "p2"
        project.mkdir()
        claude_md = project / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text(
            "header\n<!-- N3MC_AUTO_START -->\nold\n<!-- N3MC_AUTO_END -->\nfooter\n",
            encoding="utf-8",
        )
        n3._ensure_at_import(project)
        text = claude_md.read_text(encoding="utf-8")
        assert "N3MC_AUTO_START" not in text
        assert "@" in text
