"""Layer 5: the chunk length limit and Windows hook paths. No model needed."""
import json

from n3memorycore import n3memory
from n3memorycore.core.processor import chunk_text


def _all_chars(chunks, ch):
    return "".join(chunks).count(ch)


class TestChunkLimit:
    def test_paragraph_and_sentence_paths_stay_within_max(self):
        text = "\n\n".join(("This is a sentence. " * 15).strip() for _ in range(6))
        assert all(len(c) <= 400 for c in chunk_text(text))

    def test_small_max_chars(self):
        text = "Alpha beta gamma. Delta epsilon zeta! Eta theta iota? " * 10
        assert all(len(c) <= 50 for c in chunk_text(text, max_chars=50, overlap=10))

    def test_japanese_sentences(self):
        text = "これは日本語の文です。" * 100
        assert all(len(c) <= 400 for c in chunk_text(text))

    def test_tail_is_shortened_to_fit(self):
        chunks = chunk_text("x" * 10 + "\n\n" + "y" * 400)
        assert all(len(c) <= 400 for c in chunks)
        assert _all_chars(chunks, "x") >= 10 and _all_chars(chunks, "y") >= 400
        assert "x" * 10 in "".join(chunks) and "y" * 400 in "".join(chunks)


class TestWindowsHookPaths:
    def test_backslash_paths_are_own_hooks(self):
        assert n3memory.is_own_hook(r"C:\Tools\n3mc-hook.exe", "UserPromptSubmit")
        assert n3memory.is_own_hook(r"C:\Tools\n3mc-stop-hook.exe", "Stop")

    def test_init_replaces_a_backslash_entry(self, isolate):
        settings = isolate["home"] / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": r"C:\Tools\n3mc-hook.exe"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": r"C:\Tools\n3mc-stop-hook.exe"}]}]}}),
            encoding="utf-8")
        n3memory.register_hooks()
        data = json.loads(settings.read_text(encoding="utf-8"))
        for event in ("UserPromptSubmit", "Stop"):
            cmds = [h["command"] for e in data["hooks"][event] for h in e["hooks"]]
            assert len(cmds) == 1
            assert "\\" not in cmds[0]
