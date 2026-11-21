"""Surrogate stripping and UTF-8 regression tests."""
from n3memorycore import n3memory as n3m
from n3memorycore.core import database as db
from n3memorycore.core import processor as p


class TestSanitizerUnit:
    def test_strips_lone_high_surrogate(self):
        assert p.sanitize_surrogates("a\ud83db") == "ab"

    def test_strips_lone_low_surrogate(self):
        assert p.sanitize_surrogates("a\ude00b") == "ab"

    def test_clean_japanese_unchanged(self):
        assert p.sanitize_surrogates("日本語のテキスト") == "日本語のテキスト"

    def test_complete_emoji_pair_preserved(self):
        assert p.sanitize_surrogates("x😀y") == "x😀y"

    def test_recursive_dict(self):
        assert p.sanitize_surrogates({"a": "b\ud83d", "c": {"d": "e\ude00"}}) == {"a": "b", "c": {"d": "e"}}

    def test_recursive_list(self):
        assert p.sanitize_surrogates(["a\ud83d", ["b\ude00"]]) == ["a", ["b"]]

    def test_none_passthrough(self):
        assert p.sanitize_surrogates(None) is None and p.sanitize_surrogates(5) == 5


class TestSanitizedAcceptedBySQLite:
    def test_sanitized_text_inserts_without_unicode_error(self, conn, cfg):
        text = p.sanitize_surrogates("bad \ud83d surrogate")
        db.insert_memory(conn, "i", text, "2026-01-01T00:00:00+00:00", cfg["owner_id"])
        assert db.count_memories(conn) == 1


class TestPurifyAppliesSanitization:
    def test_purify_strips_surrogates_alongside_code_blocks(self):
        assert p.purify_text("a\ud83d\n```\nx\ny\n```") == "a\n[code omitted]"

    def test_purify_handles_empty(self):
        assert p.purify_text("") == "" and p.purify_text(None) == ""


def test_a_lone_surrogate_prompt_is_still_saved(conn, monkeypatch, capsys):
    import io
    import sys
    raw = '{"session_id": "s", "prompt": "emoji \\ud83d here"}'
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw.encode()), encoding="utf-8"))
    assert n3m.cmd_hook_submit() == 0
    capsys.readouterr()  # must not raise UnicodeEncodeError
    assert [r[0] for r in conn.execute("SELECT content FROM memories")] == ["[user] emoji  here"]
