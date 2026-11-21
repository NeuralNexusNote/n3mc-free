"""Data-flow trace: nothing is lost between the hook input and the stored records (no model)."""
import random
import re

from n3memorycore import n3memory as n3m
from n3memorycore.core import processor as p

ALPHABET = ["あ", "い", "。", "！", "？", ".", "!", "?", " ", "\n", "\n\n", "x", "Y", "漢", "é", "😀", "「", "」", "9"]


def _rebuild(chunks):
    got = ""
    for c in chunks:
        cc = re.sub(r"\s+", "", c)
        k = min(len(got), len(cc))
        while k and not got.endswith(cc[:k]):
            k -= 1
        got += cc[k:]
    return got


def test_chunking_keeps_every_character_in_order_and_under_the_limit():
    rnd = random.Random(7)
    for _ in range(400):
        text = "".join(rnd.choice(ALPHABET) for _ in range(rnd.choice([5, 300, 450, 900, 2500])))
        chunks = p.chunk_text(text)
        assert all(len(c) <= 400 for c in chunks)
        assert _rebuild(chunks) == re.sub(r"\s+", "", text)


def test_special_characters_survive_save(conn):
    text = "%s %% {x} $1 \n \"q\" <tag> & 😀 tab\there\r\nCRLF\r\n日本語の文章です。"
    n3m._save_text("user", text, "T")
    stored = conn.execute("SELECT content FROM memories").fetchone()[0]
    assert stored == "[user] " + text.strip()


def test_long_prompt_is_stored_in_full(conn):
    text = "\n\n".join(f"第{i}章。" + "本文です。" * 40 for i in range(1, 30))
    n3m._save_text("user", text, "T")
    stored = "".join(re.sub(r"^\[user( \d+/\d+)?\] ", "", r[0]) for r in conn.execute(
        "SELECT content FROM memories ORDER BY rowid"))
    for i in range(1, 30):
        assert f"第{i}章。" in stored
