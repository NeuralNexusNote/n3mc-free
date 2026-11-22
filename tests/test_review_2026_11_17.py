"""2026-11-17 review: chunk length cap, Windows hook paths (no model)."""
import pytest

from n3memorycore import n3memory as n3m
from n3memorycore.core import processor as p


@pytest.mark.parametrize("max_chars", [400, 120, 60])
def test_no_chunk_exceeds_max_chars(max_chars):
    text = ("段落の一文目です。二文目も続きます。" * 20 + "\n\n") * 6 + "word " * 300 + "x" * 1500
    chunks = p.chunk_text(text, max_chars=max_chars, overlap=40 if max_chars > 60 else 10)
    assert chunks and all(len(c) <= max_chars for c in chunks)


def test_tail_does_not_push_a_chunk_over_the_limit():
    chunks = p.chunk_text("x" * 10 + "\n\n" + "y" * 400)
    assert all(len(c) <= 400 for c in chunks)
    joined = "".join(chunks)
    assert joined.count("x") >= 10 and joined.count("y") >= 400


def test_every_character_is_kept_in_order():
    text = "".join(f"文{i}。" for i in range(400))
    chunks = p.chunk_text(text)
    rebuilt = ""
    for c in chunks:
        # chunks overlap; find where the new part starts
        k = min(len(rebuilt), len(c))
        while k and not rebuilt.endswith(c[:k]):
            k -= 1
        rebuilt += c[k:]
    assert rebuilt.replace(" ", "") == text


def test_windows_hook_paths_are_recognised():
    assert n3m.is_own_hook(r"C:\Tools\n3mc-hook.exe", "UserPromptSubmit")
    assert n3m.is_own_hook(r"C:\Tools\n3mc-stop-hook.exe", "Stop")
    assert not n3m.is_own_hook(r"C:\Tools\n3mc-stop-hook.exe", "UserPromptSubmit")


def test_init_replaces_instead_of_adding_for_backslash_entries():
    import json
    import os
    import pathlib
    sp = pathlib.Path(os.path.expanduser("~/.claude/settings.json"))
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command",
                              "command": r"C:\Tools\n3mc-hook.exe"}]}],
                                        "Stop": [{"hooks": [{"type": "command", "command": r"C:\Tools\n3mc-stop-hook.exe"}]}]}}))
    n3m.cmd_init()
    s = json.loads(sp.read_text())
    assert all(len([h for e in s["hooks"][ev] for h in e["hooks"]]) == 1 for ev in ("UserPromptSubmit", "Stop"))
