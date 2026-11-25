"""Lone-surrogate removal and UTF-8 regressions. No model needed."""
import io
import json
import sqlite3
import sys

from n3memorycore import n3memory, n3mc_hook, n3mc_stop_hook
from n3memorycore.core.processor import sanitize_surrogates, purify_text


class TestSanitizerUnit:
    def test_strips_lone_high_surrogate(self):
        assert sanitize_surrogates("a\ud83db") == "ab"

    def test_strips_lone_low_surrogate(self):
        assert sanitize_surrogates("a\ude00b") == "ab"

    def test_clean_japanese_unchanged(self):
        s = "坂本龍馬 — 幕末の志士"
        assert sanitize_surrogates(s) == s

    def test_complete_emoji_pair_preserved(self):
        assert sanitize_surrogates("smile 😀") == "smile 😀"
        assert sanitize_surrogates("smile 😀") == "smile 😀"

    def test_recursive_dict(self):
        assert sanitize_surrogates({"prompt": "x\ud800", "n": 1}) == {"prompt": "x", "n": 1}

    def test_recursive_list(self):
        assert sanitize_surrogates(["\udfff", {"t": "ok\ud800"}]) == ["", {"t": "ok"}]

    def test_none_passthrough(self):
        assert sanitize_surrogates(None) is None


class TestSanitizedAcceptedBySQLite:
    def test_sanitized_text_inserts_without_unicode_error(self, tmp_path):
        c = sqlite3.connect(str(tmp_path / "s.db"))
        c.execute("CREATE TABLE t (x TEXT)")
        c.execute("INSERT INTO t VALUES (?)", (sanitize_surrogates("質問\ud83dです"),))
        assert c.execute("SELECT x FROM t").fetchone()[0] == "質問です"
        c.close()


class TestPurifyAppliesSanitization:
    def test_purify_strips_surrogates_alongside_code_blocks(self):
        assert purify_text("a\ud83d\n```\ncode\n```\nb") == "a\n[code omitted]\nb"

    def test_purify_handles_empty(self):
        assert purify_text("") == ""
        assert purify_text(None) == ""


class TestAuditLogLoneSurrogate:
    """A lone surrogate in the prompt (a JSON escape) must not cost the audit.log line."""

    def _run_hook(self, module, monkeypatch, capsys):
        monkeypatch.setattr(module, "_run", lambda flag, raw=None: 0)
        raw = json.dumps({"session_id": "s", "prompt": "abc \ud83d def"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        module.main()
        with open(n3memory.AUDIT_LOG, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        assert len(lines) == 1
        assert lines[0]["hook"] == module.HOOK_NAME
        assert lines[0]["payload"]["prompt"] == "abc \ud83d def"
        assert lines[0]["raw"] == raw
        assert "audit.log could not be written" not in capsys.readouterr().err

    def test_hook_keeps_one_line(self, monkeypatch, capsys):
        self._run_hook(n3mc_hook, monkeypatch, capsys)

    def test_stop_hook_keeps_one_line(self, monkeypatch, capsys):
        self._run_hook(n3mc_stop_hook, monkeypatch, capsys)
