"""Layer 5: regression tests. Embeddings come from deterministic fake vectors; no model needed."""
import http.server
import inspect
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import n3memorycore
from n3memorycore import n3memory, n3mc_hook, n3mc_stop_hook
from n3memorycore.core import database, processor
from n3memorycore.core.database import (
    get_connection, init_db, insert_memory, search_fts, search_vector, _build_fts_query, get_all_memories,
    count_memories, find_unindexed_memories, get_meta,
)
from n3memorycore.core.processor import hybrid_search, render_memory_context, chunk_text, _fmt_saved
from tests.conftest import (
    add, basis, fake_embed, vec_with_cos, FakeModel, use_model, ts_days_ago, TEST_PORT, REAL_HTTP,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(n3memorycore.__file__)))


def _cfg(**kw):
    c = dict(n3memory.DEFAULT_CONFIG)
    c.update(n3memory.SPEC_SEARCH_SETTINGS)  # these tests check the search of the specification
    c.update(kw)
    return c


def _contents(rows=None):
    if rows is not None:
        return [r["content"] for r in rows]
    if not os.path.exists(n3memory.DB_PATH):
        return []
    c = get_connection(n3memory.DB_PATH)
    try:
        return [r["content"] for r in get_all_memories(c)]
    finally:
        c.close()


def _records():
    c = get_connection(n3memory.DB_PATH)
    try:
        return get_all_memories(c)
    finally:
        c.close()


def _notices():
    try:
        with open(n3memory.NOTICES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def _audit_lines():
    try:
        with open(n3memory.AUDIT_LOG, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def _stdin(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))


def _own_health(pid=4242):
    n3memory._load_config()
    return {"status": "ok", "root": n3memory.HOME_DIR, "code_version": n3memory._client_code_version(), "pid": pid}


def _serve_health(monkeypatch, by_port):
    """_get returns by_port[port] (a dict) or raises like a closed port."""
    def fake_get(path, timeout=5, port=None):
        port = port or n3memory._current_port()
        h = by_port.get(port) if isinstance(by_port, dict) else by_port(port)
        if h is None:
            raise urllib.error.URLError("refused")
        return h
    monkeypatch.setattr(n3memory, "_get", fake_get)


class FakeProc:
    def __init__(self, pid=50001, rc=None):
        self.pid = pid
        self.rc = rc

    def poll(self):
        return self.rc


def _client(port=TEST_PORT):
    return TestClient(n3memory.app, base_url=f"http://127.0.0.1:{port}")


# ---------------------------------------------------------------------------
# FTS
# ---------------------------------------------------------------------------

class TestFtsQuoting:
    def test_symbols_do_not_raise(self, conn):
        add(conn, "plain record")
        for q in ['C:\\Users\\test #tag = @a * $b % AND OR NOT "q"', 'NEAR "x" \x00\x07 ctrl', "a\\b/c|d^e~f+g"]:
            search_fts(conn, q)

    def test_symbol_word_hits(self, conn):
        add(conn, "release note #tag2026 is ready")
        add(conn, "something else")
        assert [h["content"] for h in search_fts(conn, "#tag2026")] == ["release note #tag2026 is ready"]

    def test_fts_exception_does_not_stop_hybrid(self, conn, monkeypatch):
        use_model(monkeypatch, FakeModel({"q": basis(0)}))
        add(conn, "vector hit", basis(0))

        def boom(*a, **k):
            raise sqlite3.OperationalError("fts5: syntax error")
        monkeypatch.setattr(processor, "search_fts", boom)
        assert [r["content"] for r in hybrid_search(conn, "q", _cfg())["results"]] == ["vector hit"]

    def test_words_joined_with_or(self, conn):
        add(conn, "the quick brown fox")
        assert search_fts(conn, "what is the quick fox doing in my database")
        assert " OR " in _build_fts_query("quick fox")

    def test_short_words_dropped(self, conn):
        assert _build_fts_query("AI 東京 is ok") == ""
        assert search_fts(conn, "AI 東京") == []

    def test_query_punctuation_splits_words(self, conn):
        add(conn, "架空の惑星「アルファ9」の気温設定")
        assert search_fts(conn, "「アルファ9」の気温は？")
        q = _build_fts_query("「アルファ9」の気温は？")
        for term in ("アルフ", "ルファ", "ファ9", "の気温", "気温は"):
            assert f'"{term}"' in q

    def test_max_40_terms(self):
        assert len(re.findall(r'"[^"]*"', _build_fts_query(" ".join(f"term{i}" for i in range(80))))) == 40

    def test_cjk_words_split_into_trigrams(self):
        assert _build_fts_query("記憶装置") == '"記憶装" OR "憶装置"'


class TestFtsIndexWords:
    ROWS = ["[user] 今日の天気は晴れでした", "[claude] 晴れの日は散歩がおすすめです", "[user] ログの調べ方を教えて",
            "[claude] 次のコマンドを実行します\n[code omitted]", "[user] 設定ファイルの場所は?",
            "[claude] 設定は ~/.n3mc/config.json にあります", "[user] ブラウザの拡張を作りたい",
            "[claude] 白樺の記憶の設定です"]

    def _fill(self, conn):
        for r in self.ROWS:
            add(conn, r)

    def test_n3mc_words_not_indexed(self, conn):
        self._fill(conn)
        for q in ("code", "omitted", "user"):
            assert search_fts(conn, q) == [], q
        hits = search_fts(conn, "Claude Code の設定")
        assert [h["content"] for h in hits] == ["[claude] 白樺の記憶の設定です"]

    def test_content_keeps_prefix_and_placeholder(self, conn):
        self._fill(conn)
        contents = _contents(get_all_memories(conn))
        assert "[claude] 次のコマンドを実行します\n[code omitted]" in contents

    def test_user_brackets_stay_indexed(self, conn):
        add(conn, "[user] the planet [Alpha-9] and the word [claude] mid-sentence")
        assert search_fts(conn, "Alpha-9")
        assert search_fts(conn, "claude")


class TestVectorFilter:
    def test_since_returns_records_outside_the_global_knn(self, conn):
        for i in range(30):
            add(conn, f"old {i}", basis(0), ts=ts_days_ago(100))
        add(conn, "recent", vec_with_cos(0.9), ts=ts_days_ago(1))
        assert "recent" not in [h["content"] for h in search_vector(conn, basis(0), 3)]
        hits = search_vector(conn, basis(0), 3, since=ts_days_ago(10))
        assert [h["content"] for h in hits] == ["recent"]

    def test_falls_back_to_exact_distance(self, conn):
        add(conn, "old", basis(0), ts=ts_days_ago(100))
        add(conn, "recent", vec_with_cos(0.9), ts=ts_days_ago(1))

        class NoRowidIn:
            def __init__(self, c):
                self.c = c
                self.fallback = False

            def execute(self, sql, params=()):
                if "rowid IN" in sql and "k = ?" in sql:
                    raise sqlite3.OperationalError("unsupported")
                if "vec_distance_l2" in sql:
                    self.fallback = True
                return self.c.execute(sql, params)
        wrapped = NoRowidIn(conn)
        hits = search_vector(wrapped, basis(0), 3, since=ts_days_ago(10))
        assert wrapped.fallback and [h["content"] for h in hits] == ["recent"]


class TestNoSimilarityDedup:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, cfg, conn):
        use_model(monkeypatch, FakeModel({}))

    def test_similar_chunks_all_saved(self, monkeypatch):
        monkeypatch.setattr(n3memory, "embed_passage", lambda c, name=None: vec_with_cos(0.97 + 0.001 * len(c) % 0.02))
        for i in range(12):
            r = n3memory.api_buffer(n3memory.BufferRequest(content=f"[claude {i + 1}/12] item {i}: value = {i}",
                                                           turn_id="T"))
            assert r["status"] == "ok"
        assert count_memories(get_connection(n3memory.DB_PATH)) == 12

    def test_same_turn_exact_skipped(self):
        n3memory.api_buffer(n3memory.BufferRequest(content="[user] same", turn_id="T"))
        r = n3memory.api_buffer(n3memory.BufferRequest(content="[user] same", turn_id="T"))
        assert r["reason"] == "exact_duplicate"

    def test_other_turn_exact_saved(self):
        n3memory.api_buffer(n3memory.BufferRequest(content="[user] same", turn_id="T1"))
        assert n3memory.api_buffer(n3memory.BufferRequest(content="[user] same", turn_id="T2"))["status"] == "ok"

    def test_no_turn_exact_skipped_globally(self):
        n3memory.api_buffer(n3memory.BufferRequest(content="manual note", turn_id="T1"))
        r = n3memory.api_buffer(n3memory.BufferRequest(content="manual note"))
        assert r["status"] == "skipped"


# ---------------------------------------------------------------------------
# Search shaping, weak matches and rendering
# ---------------------------------------------------------------------------

class TestSearchShaping:
    @pytest.fixture(autouse=True)
    def _model(self, monkeypatch):
        use_model(monkeypatch, FakeModel({"q": basis(0)}))

    def _strong(self, res):
        return [r["content"] for r in res["results"] if not r["weak"]]

    def test_below_vector_min_cos_dropped(self, conn):
        add(conn, "close", basis(0))
        add(conn, "not close enough", vec_with_cos(0.84))
        assert self._strong(hybrid_search(conn, "q", _cfg(min_results=0))) == ["close"]

    def test_relative_cut(self, conn):
        add(conn, "top", basis(0))
        add(conn, "lower", vec_with_cos(0.86))
        assert self._strong(hybrid_search(conn, "q", _cfg(min_results=0))) == ["top", "lower"]
        assert self._strong(hybrid_search(conn, "q", _cfg(min_results=0, relative_min_score_ratio=0.9))) == ["top"]

    def _versions(self, conn):
        long_v1 = "[claude] setting: " + "alpha " * 30 + "v1"
        long_v2 = "[claude] setting: " + "alpha " * 30 + "v2"
        rep = basis(5)
        add(conn, "[user] make the setting (old)", basis(0), ts=ts_days_ago(5), turn_id="OLD")
        add(conn, long_v1, rep, ts=ts_days_ago(5), turn_id="OLD")
        add(conn, "[user] make the setting (new)", vec_with_cos(0.86), ts=ts_days_ago(1), turn_id="NEW")
        add(conn, long_v2, rep, ts=ts_days_ago(1), turn_id="NEW")

    def test_version_group_survives_relative_cut(self, conn):
        self._versions(conn)
        res = hybrid_search(conn, "q", _cfg(min_results=0, relative_min_score_ratio=0.9))
        assert self._strong(res) == ["[user] make the setting (new)", "[user] make the setting (old)"]

    def test_newer_version_goes_up(self, conn):
        self._versions(conn)
        res = hybrid_search(conn, "q", _cfg(min_results=0))
        assert res["results"][0]["turn_id"] == "NEW"
        assert res["results"][0]["filter_score"] == pytest.approx(0.7)
        res = hybrid_search(conn, "q", _cfg(min_results=0, version_order_enabled=False))
        assert res["results"][0]["turn_id"] == "OLD"

    def test_identical_content_collapsed(self, conn):
        add(conn, "same content", basis(0), ts=ts_days_ago(10))
        add(conn, "same content", basis(0), ts=ts_days_ago(1))
        res = hybrid_search(conn, "q", _cfg(min_results=0))
        assert len(res["results"]) == 1
        newest = get_all_memories(conn)[-1]
        assert res["results"][0]["id"] == newest["id"]

    def test_pair_limits(self, conn):
        for t in "ABCD":
            add(conn, f"[user] question {t}", basis(0), turn_id=t)
            add(conn, f"[claude] answer {t} " + "x" * 30, basis(9), turn_id=t)
        res = hybrid_search(conn, "q", _cfg(min_results=0))
        assert len(res["pairs"]) == 3
        res = hybrid_search(conn, "q", _cfg(min_results=0, pair_max_chars=40))
        assert len(res["pairs"]) == 1
        (t, rows), = res["pairs"].items()
        assert len(rows) == 1 and res["pairs_omitted"] == {t: 1}
        res = hybrid_search(conn, "q", _cfg(min_results=0, pair_max_chars=10))
        assert res["pairs"] == {} and list(res["pairs_omitted"].values()) == [2]

    def test_other_session_old_record_survives(self, conn):
        add(conn, "current session, low similarity", vec_with_cos(0.86), session_id="cur")
        add(conn, "other session, 30 days, high similarity", vec_with_cos(0.99), ts=ts_days_ago(30), session_id="old")
        res = hybrid_search(conn, "q", _cfg(min_results=0), session_id="cur")
        assert self._strong(res) == ["current session, low similarity", "other session, 30 days, high similarity"]

    def test_other_session_keyword_only_hit_survives(self, conn):
        for i in range(30):
            add(conn, f"filler record {i}", basis(3))
        add(conn, "the zebrafish experiment notes", basis(4), session_id="old")
        res = hybrid_search(conn, "zebrafish", _cfg(min_results=0), session_id="cur")
        assert self._strong(res) == ["the zebrafish experiment notes"]
        assert res["results"][0]["filter_score"] >= 0.2 > res["results"][0]["score"]

    def test_400_day_old_record_returned_alone(self, conn):
        add(conn, "very old but relevant", basis(0), ts=ts_days_ago(400))
        assert self._strong(hybrid_search(conn, "q", _cfg(min_results=0))) == ["very old but relevant"]

    def test_new_and_old_both_returned_new_first(self, conn):
        add(conn, "old relevant", basis(0), ts=ts_days_ago(400))
        add(conn, "new relevant", basis(0), ts=ts_days_ago(1))
        assert self._strong(hybrid_search(conn, "q", _cfg(min_results=0))) == ["new relevant", "old relevant"]


class TestWeakMatches:
    @pytest.fixture(autouse=True)
    def _model(self, monkeypatch):
        use_model(monkeypatch, FakeModel({"q": basis(0)}))

    def _base(self, conn):
        add(conn, "A strong", basis(0))
        add(conn, "B below vmin", vec_with_cos(0.84, other=1))
        add(conn, "C lower", vec_with_cos(0.80, other=2))
        add(conn, "D far", vec_with_cos(0.5, other=3))

    def test_fill_to_min_results(self, conn):
        self._base(conn)
        res = hybrid_search(conn, "q", _cfg())
        assert [(r["content"], r["weak"]) for r in res["results"]] == [
            ("A strong", False), ("B below vmin", True), ("C lower", True)]

    def test_weak_uses_real_cos(self, conn):
        self._base(conn)
        b = [r for r in hybrid_search(conn, "q", _cfg())["results"] if r["content"] == "B below vmin"][0]
        assert b["cos_sim"] == pytest.approx(0.84, abs=1e-4)
        assert b["filter_score"] == pytest.approx(0.84 * 0.7, abs=1e-4)

    def test_min_results_zero(self, conn):
        self._base(conn)
        assert len(hybrid_search(conn, "q", _cfg(min_results=0))["results"]) == 1

    def test_enough_results_no_fill(self, conn):
        for i in range(3):
            add(conn, f"strong {i}", basis(0))
        add(conn, "B below vmin", vec_with_cos(0.84))
        res = hybrid_search(conn, "q", _cfg())
        assert not any(r["weak"] for r in res["results"]) and len(res["results"]) == 3

    def test_limit_below_min_results(self, conn):
        self._base(conn)
        assert len(hybrid_search(conn, "q", _cfg(search_result_limit=2))["results"]) == 2

    def test_skip_same_content_and_full_turn(self, conn):
        add(conn, "[user] T1 a", basis(0), turn_id="T1")
        add(conn, "[claude] T1 b", basis(0), turn_id="T1")
        add(conn, "[claude] T1 c", vec_with_cos(0.84), turn_id="T1")
        add(conn, "[user] T1 a", vec_with_cos(0.83), turn_id="T9")
        add(conn, "other weak", vec_with_cos(0.80))
        res = hybrid_search(conn, "q", _cfg(version_order_enabled=False))
        assert sorted((r["content"], r["weak"]) for r in res["results"]) == [
            ("[claude] T1 b", False), ("[user] T1 a", False), ("other weak", True)]
        assert res["results"][-1]["content"] == "other weak"

    def test_orthogonal_not_added(self, conn):
        add(conn, "A strong", basis(0))
        add(conn, "orthogonal", basis(7))
        assert [r["content"] for r in hybrid_search(conn, "q", _cfg())["results"]] == ["A strong"]

    def test_weak_not_used_for_pairs(self, conn):
        add(conn, "A strong", basis(0))
        add(conn, "[user] weak turn", vec_with_cos(0.84), turn_id="W")
        res = hybrid_search(conn, "q", _cfg())
        assert res["results"][1]["weak"] and res["pairs"] == {}

    def test_empty_query_returns_nothing(self, conn):
        self._base(conn)
        assert hybrid_search(conn, "", _cfg())["results"] == []

    def test_search_endpoint_flags_weak(self, conn, cfg):
        self._base(conn)
        n3memory._SERVER_STATE["cfg"] = dict(cfg, **n3memory.SPEC_SEARCH_SETTINGS)
        res = n3memory.api_search(n3memory.SearchRequest(query="q"))
        assert [r["weak"] for r in res["results"]] == [False, True, True]


class TestRender:
    def _data(self, weak=True, strong=True):
        rows = []
        if strong:
            rows.append({"id": "1", "content": "strong row", "score": 0.81, "timestamp": "2026-11-13T01:34:00+00:00",
                         "turn_id": "T1", "weak": False, "agent_name": "claude-code"})
        if weak:
            rows.append({"id": "2", "content": "weak row", "score": 0.41, "timestamp": "2025-08-02T12:15:00+00:00",
                         "turn_id": None, "weak": True, "agent_name": None})
        return {"results": rows, "pairs": {"T1": [{"id": "1", "content": "[user] q", "timestamp":
                                                    "2026-11-13T01:34:00+00:00"}]},
                "pairs_omitted": {"T1": 2}}

    def test_saved_is_local_time(self):
        local = datetime(2026, 11, 13, 1, 34, tzinfo=timezone.utc).astimezone()
        assert _fmt_saved("2026-11-13T01:34:56+00:00") == f"{local:%Y-%m-%d %H:%M} (UTC{local:%z})"

    def test_version_sentence(self):
        out = render_memory_context("q", self._data())
        assert "the one with the newest `saved` time is the current one" in out

    def test_omitted_line_names_show_turn(self):
        out = render_memory_context("q", self._data())
        assert "_(2 more chunk(s) of this exchange omitted. Full text: " \
               "`python -m n3memorycore.n3memory --show-turn T1`)_" in out

    def test_notice_comes_first(self):
        out = render_memory_context("q", self._data(), notices=["fix me"])
        lines = out.splitlines()
        assert lines[0] == "# Recalled Memory Context" and lines[1] == "Search query: q"
        assert lines[3] == "> ⚠️ N3MC notice: fix me"

    def test_weak_between_top_and_pairs(self):
        out = render_memory_context("q", self._data())
        top = out.index("## Top matches (use these to answer the question)")
        weak = out.index("## Possibly related (weak matches — may be unrelated)")
        pairs = out.index("## Previous matching Q-A exchanges (supplementary context)")
        assert top < weak < pairs
        assert "## [1] score=0.8100" in out and "## [2] score=0.4100" in out
        assert out[weak:].split("\n")[2].endswith(" -")

    def test_no_top_matches_with_weak(self):
        out = render_memory_context("q", self._data(strong=False))
        assert "## Top matches" not in out
        assert out.index("_No relevant memories found._") < out.index("## Possibly related")
        assert "## [1] score=0.4100" in out

    def test_no_weak_heading_without_weak(self):
        assert "Possibly related" not in render_memory_context("q", self._data(weak=False))


# ---------------------------------------------------------------------------
# --stop: memory_context.md and the rules file
# ---------------------------------------------------------------------------

class TestNoContextFile:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        p = tmp_path / "proj"
        (p / ".claude").mkdir(parents=True)
        monkeypatch.chdir(p)
        return p

    def _ctx(self):
        return os.path.join(n3memory.MEMORY_DIR, "memory_context.md")

    def _write(self, project, text, encoding="utf-8", bom=False):
        data = text.encode(encoding)
        if bom:
            data = b"\xef\xbb\xbf" + data
        (project / ".claude" / "CLAUDE.md").write_bytes(data)

    def _read(self, project):
        return (project / ".claude" / "CLAUDE.md").read_bytes()

    def test_search_does_not_create_context_file(self, monkeypatch, capsys):
        monkeypatch.setattr(n3memory, "ensure_server", lambda cfg=None: True)
        monkeypatch.setattr(n3memory, "_post", lambda *a, **k: {"results": [], "pairs": {}, "pairs_omitted": {}})
        n3memory.cmd_search("anything")
        assert "# Recalled Memory Context" in capsys.readouterr().out
        assert not os.path.exists(self._ctx())

    def test_import_removed_with_backup_crlf(self, project):
        text = ("# My notes\r\n# N3MemoryCore — memory context\r\n# Path resolves to the data folder\r\n\r\n"
                f"@{self._ctx()}\r\nkeep this\r\n")
        self._write(project, text)
        n3memory.cmd_stop()
        assert self._read(project) == b"# My notes\r\nkeep this\r\n"
        assert list((project / ".claude").glob("CLAUDE.md.bak-n3mc-*"))

    @pytest.mark.parametrize("variant", ["slash", "backslash", "space"])
    def test_path_variants(self, project, isolate, monkeypatch, variant):
        ctx = self._ctx()
        if variant == "slash":
            line = "@" + ctx.replace("\\", "/")
        elif variant == "backslash":
            line = "@" + ctx.replace("/", "\\") if os.name == "nt" else "@" + ctx
        else:
            spaced = isolate["tmp"] / "my data"
            mem = spaced / ".memory"
            mem.mkdir(parents=True)
            monkeypatch.setattr(n3memory, "MEMORY_DIR", str(mem))
            line = "@" + str(mem / "memory_context.md")
        self._write(project, f"top\n{line}\nbottom\n")
        n3memory.cmd_stop()
        assert self._read(project) == b"top\nbottom\n"

    def test_no_matching_line_untouched(self, project):
        self._write(project, "# nothing to do\n")
        n3memory.cmd_stop()
        assert self._read(project) == b"# nothing to do\n"
        assert not list((project / ".claude").glob("CLAUDE.md.bak-n3mc-*"))

    def test_old_file_removed_and_marker(self):
        with open(self._ctx(), "w", encoding="utf-8") as f:
            f.write("old")
        n3memory.cmd_stop()
        assert not os.path.exists(self._ctx())
        assert os.path.exists(os.path.join(n3memory.MEMORY_DIR, ".memory_context_retired"))

    @pytest.mark.parametrize("line", ["@../.memory/memory_context.md", "@C:/other/.memory/memory_context.md",
                                      "@/other/.memory/memory_context.md", "@docs/notes_about_memory_context.md"])
    def test_other_imports_kept(self, project, line):
        self._write(project, f"{line}\n")
        n3memory.cmd_stop()
        assert self._read(project) == f"{line}\n".encode()

    def test_bom_first_line_removed_and_bom_kept(self, project):
        self._write(project, f"@{self._ctx()}\nrest\n", bom=True)
        n3memory.cmd_stop()
        assert self._read(project) == b"\xef\xbb\xbfrest\n"

    def test_cp932_untouched(self, project, capsys):
        self._write(project, f"日本語のメモ\n@{self._ctx()}\n", encoding="cp932")
        before = self._read(project)
        assert n3memory.cmd_stop() == 0
        assert self._read(project) == before


class TestBehaviorMd:
    @pytest.fixture
    def project(self, tmp_path):
        p = tmp_path / "proj"
        (p / ".claude" / "rules").mkdir(parents=True)
        return p

    def _rules(self, project):
        return project / ".claude" / "rules" / "n3mc-behavior.md"

    def test_current_marker_untouched(self, project):
        self._rules(project).write_text("<!-- n3mc-behavior v3 -->\nmy own edits\n", encoding="utf-8")
        n3memory._ensure_behavior_md(str(project))
        assert self._rules(project).read_text(encoding="utf-8") == "<!-- n3mc-behavior v3 -->\nmy own edits\n"

    def test_known_hashes_present(self):
        assert {"ade0a59b94f59f9a", "34ed39efe5a3833d", "5af3c015c06c62c1"} <= set(n3memory._KNOWN_BEHAVIOR_MD_HASHES)

    def test_known_template_replaced_with_backup(self, project, monkeypatch):
        old = "<!-- n3mc-behavior v2 -->\n# old template\r\n"
        monkeypatch.setattr(n3memory, "_KNOWN_BEHAVIOR_MD_HASHES",
                            set(n3memory._KNOWN_BEHAVIOR_MD_HASHES) | {n3memory._content_hash16(old)})
        self._rules(project).write_bytes(old.encode())
        n3memory._ensure_behavior_md(str(project))
        new = self._rules(project).read_text(encoding="utf-8")
        assert new.splitlines()[0] == "<!-- n3mc-behavior v3 -->" and "## Weak Matches" in new
        assert list((project / ".claude" / "rules").glob("n3mc-behavior.md.bak-n3mc-*"))

    def test_hand_edited_not_replaced_and_notice(self, project):
        self._rules(project).write_text("my own rules\n", encoding="utf-8")
        n3memory._ensure_behavior_md(str(project))
        assert self._rules(project).read_text(encoding="utf-8") == "my own rules\n"
        keys = [k for k in _notices() if k.startswith("behavior_md_edited:")]
        assert len(keys) == 1 and len(keys[0].split(":")[1]) == 12
        assert str(self._rules(project).resolve()) in _notices()[keys[0]]["message"] or \
            os.path.abspath(str(self._rules(project))) in _notices()[keys[0]]["message"]

    def test_two_edited_files_two_notices(self, project, tmp_path):
        other = tmp_path / "proj2"
        (other / ".claude" / "rules").mkdir(parents=True)
        for p in (project, other):
            self._rules(p).write_text("edited\n", encoding="utf-8")
            n3memory._ensure_behavior_md(str(p))
        assert len([k for k in _notices() if k.startswith("behavior_md_edited:")]) == 2

    def test_notice_cleared_when_marker_current(self, project):
        self._rules(project).write_text("edited\n", encoding="utf-8")
        n3memory._ensure_behavior_md(str(project))
        self._rules(project).write_text(n3memory._BEHAVIOR_MD_CONTENT, encoding="utf-8")
        n3memory._ensure_behavior_md(str(project))
        assert not [k for k in _notices() if k.startswith("behavior_md_edited")]


# ---------------------------------------------------------------------------
# Server check, start and API
# ---------------------------------------------------------------------------

class TestServerDecision:
    def test_decisions(self):
        h = _own_health()
        assert n3memory._health_decision(h) == "ok"
        assert n3memory._health_decision({**h, "code_version": "0" * 16}) == "stale"
        assert n3memory._health_decision({**h, "root": "/somewhere/else"}) == "foreign"
        assert n3memory._health_decision({"status": "ok"}) == "legacy"
        assert n3memory._health_decision(None) == "none"

    def test_legacy_unverifiable_is_foreign(self, monkeypatch):
        n3memory._load_config()
        _serve_health(monkeypatch, {TEST_PORT: {"status": "ok"}})
        monkeypatch.setattr(n3memory, "_is_own_server_process", lambda pid=None, port=None: False)
        assert n3memory.ensure_server() is False
        assert n3memory._FOREIGN_SERVER_ROOT == n3memory.LEGACY_ROOT
        assert "port_conflict" in _notices()

    def test_shutdown_used_first(self, monkeypatch):
        stale = {**_own_health(), "code_version": "0" * 16}
        _serve_health(monkeypatch, {TEST_PORT: stale})
        calls = []
        monkeypatch.setattr(n3memory, "_request_shutdown", lambda port=None: calls.append(("shutdown", port)) or True)
        monkeypatch.setattr(n3memory, "_wait_port_free", lambda port: True)
        monkeypatch.setattr(n3memory, "_is_own_server_process", lambda *a, **k: calls.append("psutil") or True)
        monkeypatch.setattr(n3memory, "_start_server", lambda cfg: calls.append("start") or True)
        assert n3memory.ensure_server() is True
        assert calls == [("shutdown", TEST_PORT), "start"]

    def test_shutdown_root_mismatch_403(self, monkeypatch):
        monkeypatch.setattr(n3memory, "_exit_server", lambda: None)
        with _client() as c:
            assert c.post("/shutdown", json={"root": "/not/this/folder"}).status_code == 403
            assert c.post("/shutdown", json={"root": n3memory.HOME_DIR}).json() == {"status": "ok"}

    def test_dead_pid_file_does_not_wait(self, monkeypatch):
        with open(n3memory.PID_FILE, "w") as f:
            f.write("99999991")
        started, waited = [], []
        monkeypatch.setattr(n3memory, "_start_server", lambda cfg: started.append(1) or True)
        monkeypatch.setattr(n3memory, "_wait_for_starting", lambda cfg: waited.append(1) or False)
        assert n3memory.ensure_server(n3memory._load_config()) is True
        assert started == [1] and waited == [] and not os.path.exists(n3memory.PID_FILE)

    def _other_port_setup(self, monkeypatch, other_health):
        n3memory._load_config()
        with open(n3memory.PID_FILE, "w") as f:
            f.write("4242")
        _serve_health(monkeypatch, {TEST_PORT + 1: other_health})
        monkeypatch.setattr(n3memory, "_pid_status", lambda pid, age: "starting")
        monkeypatch.setattr(n3memory, "_listening_ports", lambda pid: {TEST_PORT + 1})
        monkeypatch.setattr(n3memory, "_wait_pid_exit", lambda pid, seconds=15: None)
        calls = []
        monkeypatch.setattr(n3memory, "_request_shutdown", lambda port=None: calls.append(("shutdown", port)) or True)
        monkeypatch.setattr(n3memory, "_start_server", lambda cfg: calls.append("start") or True)
        monkeypatch.setattr(n3memory, "_wait_for_starting", lambda cfg: calls.append("wait") or False)
        return calls

    def test_server_on_other_port_is_moved(self, monkeypatch):
        calls = self._other_port_setup(monkeypatch, {**_own_health(), "pid": 4242})
        assert n3memory.ensure_server() is True
        assert calls == [("shutdown", TEST_PORT + 1), "start"]
        assert not os.path.exists(n3memory.PID_FILE)

    def test_other_root_on_other_port_untouched(self, monkeypatch):
        calls = self._other_port_setup(monkeypatch, {"status": "ok", "root": "/other", "code_version": "x", "pid": 4242})
        assert n3memory.ensure_server() is False
        assert calls == ["wait"]

    def test_host_check_uses_the_listening_port(self, isolate):
        with _client(TEST_PORT) as c:
            cfgfile = isolate["data"] / "config.json"
            data = json.loads(cfgfile.read_text(encoding="utf-8"))
            data["server_port"] = TEST_PORT + 10
            cfgfile.write_text(json.dumps(data), encoding="utf-8")
            assert c.get("/health").status_code == 200
            r = c.get("/health", headers={"Host": f"127.0.0.1:{TEST_PORT + 10}"})
            assert r.status_code == 403


class TestNotices:
    @pytest.mark.parametrize("bad", ["yesterday", {"x": 1}, True])
    def test_broken_last_shown(self, bad):
        with open(n3memory.NOTICES_FILE, "w", encoding="utf-8") as f:
            json.dump({"k": {"message": "hello", "last_shown": bad}}, f)
        assert n3memory._take_notices() == ["hello"]
        shown = _notices()["k"]["last_shown"]
        assert isinstance(shown, float) and abs(shown - time.time()) < 60
        assert n3memory._take_notices() == []


class TestServerStart:
    @pytest.fixture(autouse=True)
    def _cfg(self, isolate):
        n3memory._load_config()

    def test_wait_is_bounded_by_the_clock(self, monkeypatch):
        # A server that accepts but never answers /health: every check runs into its timeout.
        # The wait must end after SERVER_WAIT_SECONDS of clock time, not after 120 checks.
        clock = {"t": 1000.0}
        timeouts = []

        def slow_get(path, timeout=5, port=None):
            timeouts.append(timeout)
            clock["t"] += timeout
            raise urllib.error.URLError("timed out")
        monkeypatch.setattr(n3memory.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(n3memory, "_get", slow_get)
        monkeypatch.setattr(n3memory, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
        start = clock["t"]
        assert n3memory._wait_for_starting(n3memory._CFG) is False
        assert clock["t"] - start <= n3memory.SERVER_WAIT_SECONDS + n3memory.POLL_INTERVAL
        assert len(timeouts) < 20 and max(timeouts) <= n3memory.HEALTH_TIMEOUT
        clock["t"] = start = 5000.0
        timeouts.clear()
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: FakeProc())
        assert n3memory._start_server(n3memory._CFG) is False
        assert clock["t"] - start <= n3memory.SERVER_WAIT_SECONDS + n3memory.POLL_INTERVAL

    def test_child_exit_stops_waiting(self, monkeypatch):
        spawned = []
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: spawned.append(kw) or FakeProc(rc=3))
        assert n3memory.ensure_server() is False
        msg = _notices()["server_start_failed"]["message"]
        assert "exit code 3" in msg and n3memory.SERVER_LOG in msg
        assert os.path.exists(n3memory.SERVER_START_FAILED)
        assert not os.path.exists(n3memory.PID_FILE)
        assert len(spawned) == 1

    def test_no_new_start_within_5_minutes(self, monkeypatch):
        with open(n3memory.SERVER_START_FAILED, "w") as f:
            f.write("x")
        spawned = []
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: spawned.append(1) or FakeProc())
        assert n3memory.ensure_server() is False and spawned == []

    def test_later_success_clears_notice_and_marker(self, monkeypatch):
        with open(n3memory.SERVER_START_FAILED, "w") as f:
            f.write("x")
        old = time.time() - 600
        os.utime(n3memory.SERVER_START_FAILED, (old, old))
        n3memory._notify("server_start_failed", "failed before")
        state = {"up": False}
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: state.update(up=True) or FakeProc())
        _serve_health(monkeypatch, lambda port: _own_health() if state["up"] else None)
        assert n3memory.ensure_server() is True
        assert "server_start_failed" not in _notices()
        assert not os.path.exists(n3memory.SERVER_START_FAILED)

    def test_still_starting_not_replaced(self, monkeypatch):
        with open(n3memory.PID_FILE, "w") as f:
            f.write("4242")
        monkeypatch.setattr(n3memory, "_pid_status", lambda pid, age: "starting")
        monkeypatch.setattr(n3memory, "_listening_ports", lambda pid: set())
        spawned = []
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: spawned.append(1) or FakeProc())
        assert n3memory.ensure_server() is False
        assert n3memory._NO_SERVER_REASON == "the memory server is still starting"
        assert spawned == [] and open(n3memory.PID_FILE).read() == "4242"

    def test_popen_flags_and_server_log(self, monkeypatch):
        seen = {}

        def spawn(cmd, **kw):
            seen.update(kw, cmd=cmd)
            return FakeProc(rc=1)
        monkeypatch.setattr(n3memory, "_spawn", spawn)
        n3memory.ensure_server()
        assert seen["cmd"][-3:] == ["-m", "n3memorycore.n3memory", "--run-server"]
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            assert seen["creationflags"] & flags == flags
        else:
            assert seen["start_new_session"] is True
        assert os.path.abspath(seen["stderr"].name) == os.path.abspath(n3memory.SERVER_LOG)
        assert seen["stdout"] == subprocess.DEVNULL and seen["stdin"] == subprocess.DEVNULL
        assert n3memory._same_path(seen["env"]["N3MC_HOME"], n3memory.HOME_DIR)

    def test_two_concurrent_ensure_server_spawn_once(self, monkeypatch):
        state = {"spawns": 0}
        lock = threading.Lock()

        def spawn(cmd, **kw):
            with lock:
                state["spawns"] += 1
            time.sleep(0.2)
            return FakeProc(pid=4243)
        monkeypatch.setattr(n3memory, "_spawn", spawn)
        monkeypatch.setattr(n3memory, "_sleep", lambda s: time.sleep(0.01))
        monkeypatch.setattr(n3memory, "_is_own_server_process", lambda pid=None, port=None: True)
        monkeypatch.setattr(n3memory, "_listening_ports", lambda pid: set())
        _serve_health(monkeypatch, lambda port: _own_health() if state["spawns"] else None)
        threads = [threading.Thread(target=n3memory.ensure_server) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert state["spawns"] == 1

    def test_popen_exception_releases_reservation(self, monkeypatch):
        n3memory.ensure_server()  # the autouse _spawn raises
        assert not os.path.exists(n3memory.PID_FILE)
        state = {"up": False}
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: state.update(up=True) or FakeProc())
        _serve_health(monkeypatch, lambda port: _own_health() if state["up"] else None)
        assert n3memory.ensure_server() is True

    def test_loser_does_not_rewrite_pid_file(self, monkeypatch):
        with open(n3memory.PID_FILE, "w") as f:
            f.write("12345")
        monkeypatch.setattr(n3memory, "_pid_status", lambda pid, age: "starting")
        spawned = []
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: spawned.append(1) or FakeProc())
        assert n3memory._start_server(n3memory._CFG) is False
        assert spawned == [] and open(n3memory.PID_FILE).read() == "12345"

    def test_empty_young_pid_file_is_starting(self, monkeypatch):
        open(n3memory.PID_FILE, "w").close()
        spawned = []
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: spawned.append(1) or FakeProc())
        assert n3memory.ensure_server() is False
        assert spawned == [] and os.path.exists(n3memory.PID_FILE)
        old = time.time() - 120
        os.utime(n3memory.PID_FILE, (old, old))
        n3memory.ensure_server()
        assert spawned == [1]

    def test_cwd_is_the_data_folder(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: seen.update(kw) or FakeProc(rc=1))
        monkeypatch.chdir(tmp_path)
        n3memory.ensure_server()
        assert n3memory._same_path(seen["cwd"], n3memory.HOME_DIR)
        os.remove(n3memory.SERVER_START_FAILED)
        monkeypatch.chdir(REPO_ROOT)
        n3memory.ensure_server()
        assert n3memory._same_path(seen["cwd"], REPO_ROOT)


class _Recorder(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, dict(self.headers)))
        body = json.dumps(self.server.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *a):
        pass


def _local_server(reply):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    srv.seen, srv.reply = [], reply
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TestServerApi:
    ENDPOINTS = [("get", "/health"), ("post", "/buffer"), ("post", "/search"), ("post", "/repair"),
                 ("get", "/list"), ("post", "/shutdown"), ("delete", "/delete/x"), ("post", "/gc"), ("post", "/import")]

    def test_foreign_host_403(self):
        with _client() as c:
            for method, path in self.ENDPOINTS:
                r = getattr(c, method)(path, headers={"Host": "evil.example"})
                assert r.status_code == 403, path

    def test_local_hosts_pass(self):
        with _client() as c:
            for host in (f"127.0.0.1:{TEST_PORT}", f"localhost:{TEST_PORT}"):
                assert c.get("/health", headers={"Host": host}).status_code == 200

    def test_endpoints_are_plain_functions(self):
        for fn in (n3memory.api_buffer, n3memory.api_search, n3memory.api_repair, n3memory.api_list):
            assert not inspect.iscoroutinefunction(fn)

    def test_repair_respects_max_records(self, conn, cfg, monkeypatch):
        use_model(monkeypatch, FakeModel())
        for i in range(450):
            conn.execute("INSERT INTO memories (id, content, timestamp, owner_id) VALUES (?, ?, ?, 'o')",
                         (f"id{i}", f"row {i}", ts_days_ago(0)))
        conn.commit()
        n3memory.api_repair(n3memory.RepairRequest(max_records=200))
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] <= 200
        n3memory.api_repair(n3memory.RepairRequest(max_records=200))
        n3memory.api_repair(n3memory.RepairRequest(max_records=200))
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] <= 200
        n3memory.api_repair(None)
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 450

    def test_hook_submit_sends_the_limit(self, monkeypatch, capsys):
        sent = []
        monkeypatch.setattr(n3memory, "ensure_server", lambda cfg=None: True)

        def post(path, body=None, timeout=30, port=None):
            sent.append((path, body))
            if path == "/search":
                return {"results": [], "pairs": {}, "pairs_omitted": {}}
            return {"status": "ok", "count": 0}
        monkeypatch.setattr(n3memory, "_post", post)
        _stdin(monkeypatch, {"session_id": "s", "prompt": "hello"})
        n3memory.cmd_hook_submit()
        assert ("/repair", {"max_records": 200}) in sent

    def test_proxy_is_not_used(self, monkeypatch):
        n3mc = _local_server({"status": "ok", "root": "x"})
        proxy = _local_server({"status": "proxy"})
        try:
            monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_address[1]}")
            monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_address[1]}")
            monkeypatch.delenv("NO_PROXY", raising=False)
            monkeypatch.delenv("no_proxy", raising=False)
            r = REAL_HTTP["_get"]("/health", port=n3mc.server_address[1])
            assert r == {"status": "ok", "root": "x"}
            assert proxy.seen == [] and len(n3mc.seen) == 1
            REAL_HTTP["_post"]("/buffer", {"content": "x"}, port=n3mc.server_address[1])
            for _, headers in n3mc.seen:
                assert "Origin" not in headers and "origin" not in {k.lower() for k in headers}
        finally:
            n3mc.shutdown()
            proxy.shutdown()

    def test_origin_rejected(self):
        with _client() as c:
            for method, path in self.ENDPOINTS:
                for origin in ("http://evil.example", "null"):
                    r = getattr(c, method)(path, headers={"Origin": origin})
                    assert r.status_code == 403, (path, origin)
            assert c.get("/health").status_code == 200


class TestForeignServer:
    @pytest.fixture(autouse=True)
    def _foreign(self, monkeypatch):
        _serve_health(monkeypatch, {TEST_PORT: {"status": "ok", "root": "/other/folder", "code_version": "x",
                                                "pid": 1}})
        self.posted = []
        monkeypatch.setattr(n3memory, "_post", lambda path, body=None, **k: self.posted.append(path))

    def test_save_goes_to_own_db(self):
        assert n3memory.cmd_buffer("saved directly") == 0
        assert _contents() == ["saved directly"] and self.posted == []

    def test_search_not_done(self, capsys):
        n3memory.cmd_search("anything")
        out = capsys.readouterr().out
        assert f"_(memory search unavailable: another N3MC folder's server is using port {TEST_PORT})_" in out
        assert self.posted == []

    def test_repair_does_nothing(self, capsys):
        assert n3memory.cmd_repair() == 0
        assert self.posted == [] and "skipped" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------

class TestConfig:
    def _write(self, isolate, data):
        p = isolate["data"] / "config.json"
        p.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        return p

    def test_unreadable_not_overwritten(self, isolate):
        p = self._write(isolate, b"{ not json")
        cfg = n3memory._load_config()
        assert p.read_bytes() == b"{ not json"
        assert cfg["server_port"] == 18520
        assert "config_unreadable" in _notices()

    def test_wrong_type_only_that_key(self, isolate):
        p = self._write(isolate, {"vector_min_cos": "0.83", "server_port": TEST_PORT})
        cfg = n3memory._load_config()
        assert cfg["vector_min_cos"] == n3memory.DEFAULT_CONFIG["vector_min_cos"] and cfg["server_port"] == TEST_PORT
        assert "vector_min_cos" in _notices()["config_type"]["message"]
        assert json.loads(p.read_text(encoding="utf-8"))["vector_min_cos"] == "0.83"

    def test_fixing_clears_notices(self, isolate):
        self._write(isolate, b"{ not json")
        n3memory._load_config()
        self._write(isolate, {"vector_min_cos": "0.83"})
        n3memory._load_config()
        assert "config_unreadable" not in _notices()
        self._write(isolate, {"vector_min_cos": 0.83})
        n3memory._load_config()
        assert "config_type" not in _notices()

    def test_whitespace_and_key_order_keep_code_version(self):
        a = n3memory.compute_code_version(b'{"a": 1, "b": 2}')
        assert a == n3memory.compute_code_version(b'{\n  "b": 2,\n  "a": 1\n}\n')
        assert a != n3memory.compute_code_version(b'{"a": 1, "b": 3}')

    @pytest.mark.parametrize("key,value", [("search_query_max_chars", 2000.0), ("half_life_days", 0),
                                           ("server_port", 70000), ("server_port", -1), ("min_score", 1.5),
                                           ("min_results", -1), ("min_results", 1.5),
                                           ("search_result_limit", 1366)])
    def test_out_of_range_ignored(self, isolate, key, value):
        self._write(isolate, {key: value})
        cfg = n3memory._load_config()
        assert cfg[key] == n3memory.DEFAULT_CONFIG[key]
        assert key in _notices()["config_type"]["message"]

    @pytest.mark.parametrize("key,value", [("min_results", 0), ("search_result_limit", 1365)])
    def test_edge_values_accepted(self, isolate, key, value):
        self._write(isolate, {key: value})
        assert n3memory._load_config()[key] == value
        assert "config_type" not in _notices()

    def test_k_4095_vector_search_works(self, conn):
        add(conn, "x", basis(0))
        assert search_vector(conn, basis(0), 3 * 1365)

    def test_missing_file_exclusive_create(self, isolate, monkeypatch):
        p = isolate["data"] / "config.json"
        p.unlink()
        theirs = {"owner_id": "their-owner", "local_id": "their-local", "server_port": TEST_PORT}

        def other_process_wins():
            p.write_text(json.dumps(theirs), encoding="utf-8")
            return None, None
        monkeypatch.setattr(n3memory, "_recover_ids_from_db", other_process_wins)
        cfg = n3memory._load_config()
        assert cfg["owner_id"] == "their-owner"
        assert json.loads(p.read_text(encoding="utf-8"))["owner_id"] == "their-owner"

    def test_update_uses_os_replace(self, isolate, monkeypatch):
        replaced = []
        real = os.replace
        monkeypatch.setattr(n3memory.os, "replace", lambda a, b: replaced.append(b) or real(a, b))
        n3memory._load_config()
        assert os.path.abspath(n3memory.CONFIG_FILE) in [os.path.abspath(x) for x in replaced]

    def test_two_processes_end_with_disk_ids(self, isolate):
        self._write(isolate, {"server_port": TEST_PORT + 5, "min_score": 0.25})
        code = ("from n3memorycore import n3memory; c = n3memory._load_config(); "
                "print(c['owner_id'], c['local_id'])")
        env = os.environ.copy()
        env["N3MC_HOME"] = str(isolate["data"])
        procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True)
                 for _ in range(2)]
        outs = [p.communicate(timeout=120)[0].split() for p in procs]
        disk = json.loads((isolate["data"] / "config.json").read_text(encoding="utf-8"))
        for owner, local in outs:
            assert owner == disk["owner_id"] and local == disk["local_id"]
        assert disk["server_port"] == TEST_PORT + 5 and disk["min_score"] == 0.25

    def test_server_and_client_versions(self, isolate):
        self._write(isolate, {"server_port": TEST_PORT})  # keys get filled by whoever reads first
        with _client() as c:
            health = c.get("/health").json()
            n3memory._load_config()
            assert n3memory._health_decision(health) == "ok"
            data = json.loads((isolate["data"] / "config.json").read_text(encoding="utf-8"))
            data["vector_min_cos"] = 0.83
            self._write(isolate, data)
            n3memory._load_config()
            assert n3memory._health_decision(c.get("/health").json()) == "stale"
        n3memory._SERVER_STATE.update(cfg=None, code_version=None, port=None)
        with _client() as c:
            assert n3memory._health_decision(c.get("/health").json()) == "ok"

    def test_paths_py_change_changes_version(self, tmp_path):
        pkg = tmp_path / "pkgcopy"
        shutil.copytree(os.path.dirname(n3memorycore.__file__), pkg,
                        ignore=shutil.ignore_patterns("__pycache__"))
        before = n3memory.compute_code_version(b"{}", pkg_dir=str(pkg))
        with open(pkg / "paths.py", "a", encoding="utf-8") as f:
            f.write("\n# changed\n")
        assert n3memory.compute_code_version(b"{}", pkg_dir=str(pkg)) != before


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _transcript(tmp_path, lines):
    p = tmp_path / "session.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for line in lines:
            f.write((line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n")
    return str(p)


def _u(text, **kw):
    return {"type": "user", "message": {"role": "user", "content": text}, **kw}


def _a(*parts, **kw):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(parts)}, **kw}


def _t(text):
    return {"type": "text", "text": text}


class TestHookSubmit:
    def _submit(self, monkeypatch, prompt, transcript=None, session="s1"):
        payload = {"session_id": session, "prompt": prompt}
        if transcript is not None:
            payload["transcript_path"] = transcript
        _stdin(monkeypatch, payload)
        return n3memory.cmd_hook_submit()

    def _claude_rows(self):
        return [(r["content"], r["turn_id"]) for r in _records() if r["content"].startswith("[claude")]

    def test_task_notification_skipped(self, monkeypatch, capsys):
        n3memory._write_turn_id("s1", "T0")
        self._submit(monkeypatch, "<task-notification>job done</task-notification>")
        assert _contents() == [] and n3memory._read_turn_id("s1") == "T0"
        assert any(l.get("reason") == "system_prompt" for l in _audit_lines())

    def test_stop_keeps_turn_id(self, monkeypatch):
        n3memory._write_turn_id("s1", "T0")
        _stdin(monkeypatch, {"session_id": "s1", "last_assistant_message": "answer"})
        n3memory.cmd_save_claude_turn()
        assert n3memory._read_turn_id("s1") == "T0"
        assert [r["turn_id"] for r in _records()] == ["T0"]

    def test_previous_reply_from_transcript(self, monkeypatch, tmp_path, capsys):
        n3memory._write_turn_id("s1", "T1")
        tr = _transcript(tmp_path, [
            _u("first question"),
            _a({"type": "thinking", "thinking": "hmm"}, _t("Part one"), {"type": "tool_use", "name": "Bash"}),
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
            _a(_t("Final answer block"), {"type": "tool_use", "name": "x"}),
            _u("meta", isMeta=True),
            _u("<command-name>/clear</command-name>"),
            _a(_t("sidechain reply"), isSidechain=True),
            "not json at all",
            {"type": "summary", "summary": "x"},
            _u("second question"),
        ])
        self._submit(monkeypatch, "second question", tr)
        assert self._claude_rows() == [("[claude] Final answer block", "T1")]

    def test_interrupted_reply_found(self, monkeypatch, tmp_path, capsys):
        n3memory._write_turn_id("s1", "T1")
        tr = _transcript(tmp_path, [_u("q1"), _a(_t("partial reply")),
                                    _u([{"type": "text", "text": "[Request interrupted by user]"}])])
        self._submit(monkeypatch, "q2", tr)
        assert self._claude_rows() == [("[claude] partial reply", "T1")]

    def test_resent_question_does_not_return_older_reply(self, monkeypatch, tmp_path, capsys):
        n3memory._write_turn_id("s1", "T1")
        tr = _transcript(tmp_path, [_u("q0"), _a(_t("answer 0")), _u("same q"), _u("same q")])
        self._submit(monkeypatch, "same q", tr)
        assert self._claude_rows() == []

    def test_no_duplicate_when_stop_saved(self, monkeypatch, tmp_path, capsys):
        n3memory._write_turn_id("s1", "T1")
        _stdin(monkeypatch, {"session_id": "s1", "last_assistant_message": "the reply"})
        n3memory.cmd_save_claude_turn()
        tr = _transcript(tmp_path, [_u("q1"), _a(_t("the reply"))])
        self._submit(monkeypatch, "q2", tr)
        assert self._claude_rows() == [("[claude] the reply", "T1")]

    @pytest.mark.parametrize("which", ["missing", "broken", "absent"])
    def test_bad_transcript_logged(self, monkeypatch, tmp_path, capsys, which):
        searched = []
        monkeypatch.setattr(n3memory, "_search_output", lambda q, *a, **k: searched.append(q) or "# Recalled\n")
        if which == "missing":
            tr = str(tmp_path / "nope.jsonl")
        elif which == "broken":
            tr = str(tmp_path / "dir_not_file")
            os.mkdir(tr)
        else:
            tr = None
        self._submit(monkeypatch, "question", tr)
        stages = [l for l in _audit_lines() if l.get("stage") == "transcript_unreadable"]
        assert len(stages) == 1
        if which == "absent":
            assert stages[0]["reason"] == "no transcript_path"
        assert searched == ["question"] and "[user] question" in _contents()

    def test_image_only_writes_new_turn(self, monkeypatch, capsys):
        n3memory._write_turn_id("s1", "T0")
        self._submit(monkeypatch, json.dumps([{"type": "image", "source": {"data": "x"}}]))
        assert n3memory._read_turn_id("s1") not in (None, "T0")
        assert _contents() == []

    def test_system_reminder_then_user_text(self, monkeypatch, capsys):
        n3memory._write_turn_id("s1", "T0")
        searched = []
        monkeypatch.setattr(n3memory, "_search_output", lambda q, *a, **k: searched.append(q) or "")
        self._submit(monkeypatch, "<system-reminder>ctx</system-reminder>\n<system-reminder>more</system-reminder>\n"
                                  "  what is N3MC?  ")
        assert _contents() == ["[user] what is N3MC?"] and searched == ["what is N3MC?"]
        assert n3memory._read_turn_id("s1") != "T0"

    @pytest.mark.parametrize("prompt", ["<system-reminder>only</system-reminder>",
                                        "<system-reminder>x</system-reminder><task-notification>y</task-notification>",
                                        "<system-reminder>never closed"])
    def test_system_reminder_skipped_cases(self, monkeypatch, capsys, prompt):
        n3memory._write_turn_id("s1", "T0")
        self._submit(monkeypatch, prompt)
        assert _contents() == [] and n3memory._read_turn_id("s1") == "T0"

    def test_transcript_reminder_line_is_a_prompt(self, monkeypatch, tmp_path, capsys):
        n3memory._write_turn_id("s1", "T1")
        tr = _transcript(tmp_path, [_u("q0"), _a(_t("a0")),
                                    _u("<system-reminder>side</system-reminder>\nreal question")])
        self._submit(monkeypatch, "new question", tr)
        assert self._claude_rows() == []

    def test_search_exception_still_saves(self, monkeypatch, capsys):
        n3memory._write_turn_id("s1", "T0")

        def boom(*a, **k):
            raise RuntimeError("search exploded")
        monkeypatch.setattr(n3memory, "_search_output", boom)
        self._submit(monkeypatch, "keep me")
        assert "_(memory search unavailable: RuntimeError)_" in capsys.readouterr().out
        assert "[user] keep me" in _contents() and n3memory._read_turn_id("s1") != "T0"

    def test_save_claude_turn_uses_turn_read_first(self, monkeypatch):
        n3memory._write_turn_id("s1", "T1")

        def ensure(cfg=None):
            n3memory._write_turn_id("s1", "T2")  # the next prompt's hook wrote a new turn meanwhile
            return False
        monkeypatch.setattr(n3memory, "ensure_server", ensure)
        _stdin(monkeypatch, {"session_id": "s1", "last_assistant_message": "late reply"})
        n3memory.cmd_save_claude_turn()
        assert [(r["content"], r["turn_id"]) for r in _records()] == [("[claude] late reply", "T1")]


class TestPurifyBeforeChunk:
    def test_text_between_blocks_kept(self, monkeypatch):
        middle = "This explanation between the two blocks must be kept. " * 4
        msg = ("Intro text.\n```python\n" + "print('first')\n" * 10 + "```\n" + middle + "\n```js\n"
               + "console.log('second');\n" * 10 + "```\nOutro.")
        assert len(msg) > 500
        _stdin(monkeypatch, {"session_id": "s", "last_assistant_message": msg})
        n3memory.cmd_save_claude_turn()
        joined = " ".join(_contents())
        assert "must be kept" in joined and "Outro." in joined
        assert "print(" not in joined and "console.log" not in joined

    def test_long_code_block_replaced(self, monkeypatch):
        msg = "See:\n```\n" + "x = 1\n" * 100 + "```\nend"
        _stdin(monkeypatch, {"session_id": "s", "last_assistant_message": msg})
        n3memory.cmd_save_claude_turn()
        assert _contents() == ["[claude] See:\n[code omitted]\nend"]


class TestChunkSentences:
    def test_japanese_split_after_sentence_end(self):
        text = "".join(f"これは{i}番目の文で、内容はとくにありません。" for i in range(40)) + "最後です！本当に？"
        chunks = chunk_text(text)
        assert len(chunks) > 1
        for c in chunks[:-1]:
            assert c[-1] in "。！？"


class TestTimeBounds:
    def test_date_only_since_is_local_midnight(self):
        expected = datetime(2026, 11, 13).astimezone(timezone.utc).isoformat(timespec="microseconds")
        assert n3memory._parse_time_bound("2026-11-13") == expected

    @pytest.mark.skipif(not hasattr(time, "tzset"), reason="the process time zone cannot be changed here")
    def test_date_only_in_a_fixed_zone(self, monkeypatch):
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        try:
            assert n3memory._parse_time_bound("2026-11-13") == "2026-11-12T15:00:00.000000+00:00"
        finally:
            monkeypatch.delenv("TZ")
            time.tzset()

    def test_offset_kept(self):
        assert n3memory._parse_time_bound("2026-11-13T09:00+09:00") == "2026-11-13T00:00:00.000000+00:00"
        assert n3memory._parse_time_bound("2026-11-13T09:00Z") == "2026-11-13T09:00:00.000000+00:00"

    def test_t_and_space_same(self):
        assert n3memory._parse_time_bound("2026-11-13T09:00:00") == n3memory._parse_time_bound("2026-11-13 09:00:00")

    def test_until_end_of_day(self):
        expected = datetime(2026, 11, 13, 23, 59, 59, 999999).astimezone(timezone.utc).isoformat(timespec="microseconds")
        assert n3memory._parse_time_bound("2026-11-13", is_until=True) == expected

    def test_unreadable_exit_2(self, capsys):
        assert n3memory.cmd_search("x", since="yesterday") == 2
        assert "--since: cannot read 'yesterday' (use 2026-11-13 or 2026-11-13T09:00)" in capsys.readouterr().err
        assert n3memory.cmd_search("x", since="2026-13-01") == 2

    def test_since_after_until(self, capsys):
        assert n3memory.cmd_search("x", since="2026-11-14", until="2026-11-13") == 2
        assert "--since is later than --until" in capsys.readouterr().err

    def test_astimezone_oserror(self, monkeypatch):
        def broken(dt):
            raise OSError(22, "Invalid argument")
        monkeypatch.setattr(n3memory, "_astimezone_utc", broken)
        assert n3memory._parse_time_bound("1969-01-01") == "1969-01-01T00:00:00.000000+00:00"
        assert n3memory._parse_time_bound("9999-12-31", is_until=True).startswith("9999-12-31T23:59:59")

    def test_search_endpoint_400(self):
        with _client() as c:
            assert c.post("/search", json={"query": "x", "since": "yesterday"}).status_code == 400


# ---------------------------------------------------------------------------
# DB corruption
# ---------------------------------------------------------------------------

class TestDbCorrupt:
    def _garbage(self, with_sidecars=True):
        with open(n3memory.DB_PATH, "wb") as f:
            f.write(b"this is not a database at all" * 200)
        if with_sidecars:
            for suf in ("-wal", "-shm"):
                with open(n3memory.DB_PATH + suf, "wb") as f:
                    f.write(b"sidecar" + suf.encode())

    def _backups(self):
        return sorted(p for p in os.listdir(n3memory.MEMORY_DIR) if re.match(r"n3memory\.db\.corrupt-\d{14}(-\d+)?\.bak$", p))

    def test_two_corruptions_two_backups(self):
        self._garbage()
        n3memory._connect_checked("quick_check").close()
        self._garbage()
        n3memory._connect_checked("quick_check").close()
        backups = self._backups()
        assert len(backups) == 2
        for b in backups:
            for suf in ("-wal", "-shm"):
                assert os.path.exists(os.path.join(n3memory.MEMORY_DIR, b + suf))
        assert "db_corrupt" in _notices()
        assert not [p for p in os.listdir(n3memory.MEMORY_DIR) if p.endswith(".n3mc-hold")]

    def test_quick_check_once_per_process(self, monkeypatch, cfg):
        seen = []
        real = n3memory._connect_checked
        monkeypatch.setattr(n3memory, "_connect_checked", lambda pragma=None: seen.append(pragma) or real(pragma))
        n3memory._buffer_direct("one", cfg)
        n3memory._buffer_direct("two", cfg)
        assert seen == ["quick_check", None]

    def test_garbage_file_on_direct_save(self, cfg):
        self._garbage(with_sidecars=False)
        assert n3memory._buffer_direct("after corruption", cfg) == "ok"
        assert len(self._backups()) == 1 and "db_corrupt" in _notices()
        assert _contents() == ["after corruption"]

    def test_garbage_file_on_startup(self):
        self._garbage(with_sidecars=False)
        with _client():
            pass
        assert len(self._backups()) == 1 and "db_corrupt" in _notices()

    def test_damaged_first_page(self, conn):
        add(conn, "record")
        conn.close()
        with open(n3memory.DB_PATH, "r+b") as f:
            f.write(b"\x00" * 100)
        n3memory._connect_checked("integrity_check").close()
        assert len(self._backups()) == 1

    def test_not_corruption(self):
        assert not n3memory._is_corruption_error(sqlite3.OperationalError("database is locked"))
        assert not n3memory._is_corruption_error(sqlite3.OperationalError("database disk image is malformed"))
        assert not n3memory._is_corruption_error(RuntimeError("sqlite-vec ... Recovery: pip install sqlite-vec"))
        assert n3memory._is_corruption_error(sqlite3.DatabaseError("file is not a database"))
        assert n3memory._is_corruption_error(sqlite3.DatabaseError("database disk image is malformed"))


# ---------------------------------------------------------------------------
# --init
# ---------------------------------------------------------------------------

class TestInit:
    @pytest.fixture
    def settings(self, isolate):
        p = isolate["home"] / ".claude" / "settings.json"
        p.parent.mkdir()
        return p

    def _write(self, settings, data):
        settings.write_text(json.dumps(data), encoding="utf-8")

    def _cmds(self, settings, event):
        data = json.loads(settings.read_text(encoding="utf-8"))
        return [h.get("command") for e in data["hooks"][event] for h in e["hooks"]]

    def test_pro_hook_kept_and_reported(self, settings, capsys):
        pro = 'python "C:/Users/me/N3MC-Pro/N3MemoryCore/n3mc_hook.py"'
        self._write(settings, {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": pro}]}]}})
        n3memory.register_hooks()
        cmds = self._cmds(settings, "UserPromptSubmit")
        assert pro in cmds and len(cmds) == 2
        assert f"Another N3MC hook is registered for UserPromptSubmit and was left in place: {pro}" in capsys.readouterr().out

    def test_shell_path_quotes_shell_characters(self):
        assert n3memory.shell_path("C:/Users/me/Scripts/n3mc-hook.exe") == "C:/Users/me/Scripts/n3mc-hook.exe"
        assert n3memory.shell_path("C:/Program Files/n3mc-hook.exe") == '"C:/Program Files/n3mc-hook.exe"'
        assert n3memory.shell_path("C:/Users/O'Brien/n3mc-hook.exe") == "\"C:/Users/O'Brien/n3mc-hook.exe\""
        assert n3memory.shell_path("C:/Users/A&B/n3mc-hook.exe") == '"C:/Users/A&B/n3mc-hook.exe"'
        assert n3memory.shell_path("C:/Users/a$b`c/x.exe") == '"C:/Users/a\\$b\\`c/x.exe"'

    def test_own_entries_replaced(self, settings):
        self._write(settings, {"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/x/n3mc-hook.exe"}]},
                                 {"hooks": [{"type": "command", "command": "/y/n3mc-hook"}]},
                                 {"hooks": [{"type": "command", "command": "python -m n3memorycore.n3mc_hook"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "python -m n3memorycore.n3mc_stop_hook"}]}]}})
        n3memory.register_hooks()
        assert len(self._cmds(settings, "UserPromptSubmit")) == 1 and len(self._cmds(settings, "Stop")) == 1

    def test_rerun_writes_nothing(self, settings):
        assert n3memory.register_hooks() is True
        before = settings.read_bytes()
        assert n3memory.register_hooks() is False
        assert settings.read_bytes() == before
        assert len(self._cmds(settings, "UserPromptSubmit")) == 1

    def test_backup_before_change(self, settings):
        self._write(settings, {"autoUpdatesChannel": "latest"})
        n3memory.register_hooks()
        assert list(settings.parent.glob("settings.json.bak-n3mc-*"))
        assert json.loads(settings.read_text(encoding="utf-8"))["autoUpdatesChannel"] == "latest"

    @pytest.mark.parametrize("raw", [b"{ broken", b"[1, 2]", b'{"hooks": []}', b'{"permissions": {"allow": "x"}}'])
    def test_invalid_settings_unchanged(self, settings, raw):
        settings.write_bytes(raw)
        with pytest.raises(SystemExit) as e:
            n3memory.register_hooks()
        assert e.value.code not in (0, None)
        assert settings.read_bytes() == raw

    def test_permission_rules_added(self, settings):
        self._write(settings, {"permissions": {"allow": ["Bash(git status)"]}})
        n3memory.register_hooks()
        allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
        assert allow[0] == "Bash(git status)"
        assert set(n3memory.PERMISSION_RULES) <= set(allow)

    def test_scripts_folder_found_without_path(self, settings, monkeypatch, tmp_path):
        folder = tmp_path / "Scripts"
        folder.mkdir()
        (folder / "n3mc-hook.exe").write_bytes(b"")
        (folder / "n3mc-stop-hook.exe").write_bytes(b"")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(n3memory, "_scripts_folders", lambda: [str(folder)])
        n3memory.register_hooks()
        cmd = self._cmds(settings, "UserPromptSubmit")[0]
        # found in the scripts folder; quoted or not as shell_path decides for that path (the name of
        # the temporary folder can hold shell characters)
        assert cmd == n3memory.shell_path(folder / "n3mc-hook.exe")

    @pytest.mark.parametrize("name,quoted", [("Scripts", False), ("#!sandbox.{x} y", True)])
    def test_found_entry_point_quoted_by_its_path(self, settings, monkeypatch, tmp_path, name, quoted):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "n3mc-hook.exe").write_bytes(b"")
        monkeypatch.setattr(n3memory, "_scripts_folders", lambda: [str(folder)])
        cmd = n3memory.resolve_cmd("n3mc-hook")
        assert cmd == n3memory.shell_path(folder / "n3mc-hook.exe")
        if quoted:
            assert cmd.startswith('"') and cmd.endswith('n3mc-hook.exe"')

    def test_shell_path_plain_and_special_absolute_paths(self):
        assert n3memory.shell_path("C:/Users/me/AppData/Python312/Scripts/n3mc-hook.exe") ==             "C:/Users/me/AppData/Python312/Scripts/n3mc-hook.exe"
        assert n3memory.shell_path("C:/Temp/#!sandbox.{x}/n3mc-hook.exe") == '"C:/Temp/#!sandbox.{x}/n3mc-hook.exe"'

    @pytest.mark.parametrize("exe,quoted", [("C:/Python312/python.exe", False), ("C:/Program Files/Py/python.exe", True)])
    def test_dash_m_quoting(self, monkeypatch, exe, quoted):
        monkeypatch.setattr(n3memory, "_scripts_folders", lambda: [])
        monkeypatch.setattr(n3memory.shutil, "which", lambda name: None)
        monkeypatch.setattr(n3memory.sys, "executable", exe)
        cmd = n3memory.resolve_cmd("n3mc-hook")
        assert cmd.endswith(" -m n3memorycore.n3mc_hook")
        assert cmd.startswith('"') == quoted

    def test_timeout_120(self, settings):
        n3memory.register_hooks()
        data = json.loads(settings.read_text(encoding="utf-8"))
        for event in ("UserPromptSubmit", "Stop"):
            assert [h["timeout"] for e in data["hooks"][event] for h in e["hooks"]] == [120]

    def test_entry_without_timeout_replaced(self, settings):
        self._write(settings, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "n3mc-stop-hook"}]}]}})
        n3memory.register_hooks()
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert [h.get("timeout") for e in data["hooks"]["Stop"] for h in e["hooks"]] == [120]

    def test_null_command_kept(self, settings):
        self._write(settings, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": None}]}]}})
        n3memory.register_hooks()
        assert None in self._cmds(settings, "Stop")

    def test_cp932_settings_unchanged(self, settings):
        raw = json.dumps({"note": "日本語"}, ensure_ascii=False).encode("cp932")
        settings.write_bytes(raw)
        with pytest.raises(SystemExit) as e:
            n3memory.register_hooks()
        assert e.value.code not in (0, None) and settings.read_bytes() == raw

    def test_cmd_init_message(self, settings, capsys):
        assert n3memory.cmd_init() == 0
        assert "Saving will start from the next session." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Smaller regressions
# ---------------------------------------------------------------------------

class TestSaveFailedNotice:
    @pytest.mark.parametrize("status", ["ok", "skipped"])
    def test_server_save_clears(self, monkeypatch, cfg, status):
        n3memory._notify("save_failed", "A record could not be saved (X: y).")
        monkeypatch.setattr(n3memory, "_post", lambda *a, **k: {"status": status, "count": 1})
        n3memory._save_record("text", cfg, True)
        assert "save_failed" not in _notices()

    def test_direct_failure_sets_notice(self, monkeypatch, cfg, capsys):
        def broken(pragma=None):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(n3memory, "_connect_checked", broken)
        assert n3memory._buffer_direct("text", cfg) == "error"
        assert "save_failed" in _notices()
        assert "Physical save failed" in capsys.readouterr().err


class TestHousekeeping:
    def test_old_turn_files_removed(self):
        old = os.path.join(n3memory.MEMORY_DIR, "turn_id_old.txt")
        new = os.path.join(n3memory.MEMORY_DIR, "turn_id_new.txt")
        for p in (old, new):
            with open(p, "w") as f:
                f.write("t")
        t = time.time() - 31 * 86400
        os.utime(old, (t, t))
        n3memory._housekeeping()
        assert not os.path.exists(old) and os.path.exists(new)

    def test_audit_rotation(self, monkeypatch):
        monkeypatch.setattr(n3memory, "AUDIT_ROTATE_BYTES", 10)
        with open(n3memory.AUDIT_LOG, "w") as f:
            f.write("x" * 50)
        with open(n3memory.AUDIT_LOG + ".1", "w") as f:
            f.write("older")
        n3memory._housekeeping()
        assert not os.path.exists(n3memory.AUDIT_LOG)
        assert open(n3memory.AUDIT_LOG + ".1").read() == "x" * 50


class TestModelRetry:
    def test_no_retry_for_60_seconds(self, monkeypatch, capsys):
        calls = []

        def failing(name):
            calls.append(name)
            raise OSError("offline")
        monkeypatch.setattr(processor, "_load_model", failing)
        for _ in range(3):
            with pytest.raises(Exception):
                processor.get_model()
        assert len(calls) == 1
        monkeypatch.setattr(processor, "_model_failed_at", time.monotonic() - 61)
        with pytest.raises(Exception):
            processor.get_model()
        assert len(calls) == 2

    def test_env_model_ignored(self, monkeypatch):
        monkeypatch.setenv("N3MC_EMBED_MODEL", "some/other-model")
        cfg = n3memory._load_config()
        assert processor._configured_model == cfg["embed_model"] == "intfloat/multilingual-e5-base"


class TestPackaging:
    def test_psutil_is_a_dependency(self):
        with open(os.path.join(REPO_ROOT, "pyproject.toml"), encoding="utf-8") as f:
            text = f.read()
        deps = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M).group(1)
        assert re.search(r'"psutil[^"]*"', deps)


class TestRepairLocking:
    def test_busy_timeout(self):
        assert database.BUSY_TIMEOUT_SECONDS == 10

    def test_writer_not_blocked_while_embedding(self, conn, cfg, monkeypatch):
        n3memory.run_repair(conn, cfg)
        for i in range(5):
            conn.execute("INSERT INTO memories (id, content, timestamp, owner_id) VALUES (?, ?, ?, 'o')",
                         (f"r{i}", f"row {i}", ts_days_ago(0)))
            conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (last_insert_rowid(), ?)", (f"row {i}",))
        conn.commit()
        use_model(monkeypatch, FakeModel(delay=0.2))
        c2 = get_connection(n3memory.DB_PATH)
        t = threading.Thread(target=n3memory.run_repair, args=(c2, cfg))
        t.start()
        time.sleep(0.3)
        other = sqlite3.connect(n3memory.DB_PATH, timeout=0.5)
        start = time.monotonic()
        other.execute("BEGIN IMMEDIATE")
        assert time.monotonic() - start < 0.5
        other.rollback()
        other.close()
        t.join()
        c2.close()

    def test_two_repairs_at_once(self, conn, cfg, monkeypatch):
        n3memory.run_repair(conn, cfg)
        for i in range(20):
            conn.execute("INSERT INTO memories (id, content, timestamp, owner_id) VALUES (?, ?, ?, 'o')",
                         (f"r{i}", f"row {i}", ts_days_ago(0)))
        conn.commit()
        use_model(monkeypatch, FakeModel(delay=0.01))
        errors = []

        def work():
            c = get_connection(n3memory.DB_PATH)
            try:
                n3memory.run_repair(c, cfg)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                c.close()
        threads = [threading.Thread(target=work) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        rows = conn.execute("SELECT rowid, COUNT(*) FROM memories_vec GROUP BY rowid").fetchall()
        assert len(rows) == 20 and all(n == 1 for _, n in rows)

    def test_start_server_ignores_foreign(self, monkeypatch, cfg):
        monkeypatch.setattr(n3memory, "_spawn", lambda cmd, **kw: FakeProc())
        _serve_health(monkeypatch, {TEST_PORT: {"status": "ok", "root": "/other", "code_version": "x", "pid": 9}})
        assert n3memory._start_server(cfg) is False
        assert n3memory._FOREIGN_SERVER_ROOT == "/other" and "port_conflict" in _notices()


class TestHookEntryAudit:
    """The audit.log record is written by the hook modules (one per call), before anything else."""

    @pytest.mark.parametrize("module", [n3mc_hook, n3mc_stop_hook])
    def test_one_record_per_call_even_if_the_save_fails(self, monkeypatch, module):
        monkeypatch.setattr(module, "_run", lambda flag, raw=None: 1)
        raw = json.dumps({"session_id": "s", "prompt": "x" * 5000})
        _stdin(monkeypatch, raw)
        module.main()
        lines = _audit_lines()
        assert len(lines) == 1
        assert lines[0]["hook"] == module.HOOK_NAME and len(lines[0]["raw"]) == 4096
        assert lines[0]["payload"]["prompt"] == "x" * 5000


class TestSearchImprovements:
    """Search improvements on by default (CPU only, not in the specification). Each can be turned off in
    config.json; SPEC_SEARCH_SETTINGS gives the search of the specification."""

    def _imp(self, **kw):
        c = dict(n3memory.DEFAULT_CONFIG)
        c.update(kw)
        return c

    def test_defaults(self):
        d = n3memory.DEFAULT_CONFIG
        assert d["vector_min_cos"] == 0.82 and d["fts_stopwords"] is True and d["kw_only_min_terms"] == 3
        assert d["top_matches_max"] == 5 and d["cross_lang_min_cos"] == 0.80
        assert d["top_matches_session_max"] == 2
        assert d["semantic_rescue_min_cos"] == 0.80 and d["semantic_rescue_margin"] == 0.02

    def test_stopwords_dropped_from_terms(self):
        assert database.fts_terms("Who is the CEO of the company?", True) == ["CEO", "company"]
        assert "the" in database.fts_terms("Who is the CEO of the company?", False)
        assert database.fts_terms("who is the", True) == ["who", "the"]  # only function words: kept
        assert database.fts_terms("東京タワーの高さ", True) == database.fts_terms("東京タワーの高さ", False)

    def test_function_words_alone_do_not_match(self, conn):
        add(conn, "[claude] The release is on Friday and the notes are in the wiki.")
        c = self._imp(min_results=0)
        q = "Who is the CEO of the company?"
        assert hybrid_search(conn, q, c, embed_fn=lambda t: basis(5))["results"] == []
        off = dict(c, fts_stopwords=False, kw_only_min_terms=0)
        assert hybrid_search(conn, q, off, embed_fn=lambda t: basis(5))["results"]

    def test_keyword_only_rows_need_enough_terms(self, conn):
        add(conn, "[claude] zebra crossing near the park")
        add(conn, "[claude] zebra crossing station lights timing")
        c = self._imp(relative_min_score_ratio=0, min_score=0)
        q = "zebra crossing station lights"
        r = hybrid_search(conn, q, c, embed_fn=lambda t: basis(5))["results"]
        assert [x["content"] for x in r if not x["weak"]] == ["[claude] zebra crossing station lights timing"]
        assert any(x["weak"] and "near the park" in x["content"] for x in r)
        off = hybrid_search(conn, q, dict(c, kw_only_min_terms=0), embed_fn=lambda t: basis(5))["results"]
        assert len([x for x in off if not x["weak"]]) == 2

    def test_short_question_keyword_rows_reach_top_matches(self, conn):
        # a question of 1-2 terms: a row holding all of them is a Top match (kw_only_min_terms 3
        # alone could never be met)
        add(conn, "[claude] Abraham Lincoln was the 16th president.", turn_id="T1")
        add(conn, "[user] who was the 16th president?", turn_id="T1")
        add(conn, "[claude] Lincoln Park is in Chicago, Abraham.", turn_id="T2")
        add(conn, "[claude] The temperature of Alpha-9 is 21 degrees.", turn_id="T3")
        c = self._imp(min_results=0, relative_min_score_ratio=0, min_score=0)
        e5 = lambda t: basis(5)  # noqa: E731
        for q in ("Lincoln", "Abraham Lincoln"):
            d = hybrid_search(conn, q, c, embed_fn=e5)
            strong = [x["content"] for x in d["results"] if not x["weak"]]
            assert "[claude] Abraham Lincoln was the 16th president." in strong, q
            assert "T1" in d["pairs"], q
        d = hybrid_search(conn, "Alpha-9 temperature", c, embed_fn=e5)
        assert [x["content"] for x in d["results"] if not x["weak"]] ==             ["[claude] The temperature of Alpha-9 is 21 degrees."]
        # two terms: a row holding only one of them stays out of Top matches
        add(conn, "[claude] Abraham wrote a letter.", turn_id="T4")
        d = hybrid_search(conn, "Abraham Lincoln", c, embed_fn=e5)
        assert "[claude] Abraham wrote a letter." not in [x["content"] for x in d["results"] if not x["weak"]]

    def test_n3mc_words_are_not_counted_as_question_terms(self):
        terms = (database.fts_terms("Claude Code settings", True), database.bigram_terms("Claude Code settings", True))
        plain = processor._matched_terms("[user] The printer settings were reset yesterday.", terms)
        marked = processor._matched_terms("[claude 1/3] The printer settings were reset. [code omitted]", terms)
        assert plain == marked == 1

    def test_top_matches_max_caps_strong_rows_only(self, conn):
        for i in range(8):
            add(conn, f"[claude] record {i}", vec_with_cos(0.95, other=1 + i))
        r = hybrid_search(conn, "anything", self._imp(top_matches_max=3), embed_fn=lambda t: basis(0))["results"]
        assert len([x for x in r if not x["weak"]]) == 3
        r = hybrid_search(conn, "anything", self._imp(top_matches_max=0), embed_fn=lambda t: basis(0))["results"]
        assert len([x for x in r if not x["weak"]]) == 8

    def test_current_session_rows_are_capped_in_top_matches(self, conn):
        # the rows of the current session are already in Claude's context: they take at most
        # top_matches_session_max of the Top matches slots, the other sessions fill the rest
        for i in range(5):
            add(conn, f"[claude] this session {i}", vec_with_cos(0.97 - i * 0.001, other=1 + i),
                session_id="S", turn_id=f"S{i}")
        for i in range(5):
            add(conn, f"[claude] other session {i}", vec_with_cos(0.93 - i * 0.001, other=10 + i),
                session_id="O", turn_id=f"O{i}")
        e0 = lambda t: basis(0)  # noqa: E731
        c = self._imp(min_results=0)
        strong = lambda d: [x for x in d["results"] if not x["weak"]]  # noqa: E731
        d = hybrid_search(conn, "anything", c, session_id="S", embed_fn=e0)
        got = strong(d)
        assert len(got) == 5
        assert [x["content"] for x in got if x["session_id"] == "S"] == ["[claude] this session 0",
                                                                         "[claude] this session 1"]
        assert len([x for x in got if x["session_id"] == "O"]) == 3
        assert set(d["pairs"]) & {"O0", "O1", "O2"}
        # 0 = off: the old behaviour (the current session first)
        got = strong(hybrid_search(conn, "anything", dict(c, top_matches_session_max=0), session_id="S", embed_fn=e0))
        assert [x["session_id"] for x in got] == ["S"] * 5
        # no session_id (a manual --search): every row is from another session
        got = strong(hybrid_search(conn, "anything", c, embed_fn=e0))
        assert [x["session_id"] for x in got] == ["S"] * 5
        # the spec settings turn it off
        spec = dict(c, **n3memory.SPEC_SEARCH_SETTINGS)
        assert spec["top_matches_session_max"] == 0

    def test_search_endpoint_without_session_has_no_session_cap(self, conn, cfg, monkeypatch):
        # a manual --search sends no session_id; the server's own session then stands in for the
        # bias, but its rows (saved by --buffer) are not in the caller's context and are not capped
        sid = n3memory._SERVER_STATE["session_id"]
        for i in range(4):
            add(conn, f"[claude] buffered {i}", vec_with_cos(0.97 - i * 0.001, other=1 + i),
                session_id=sid, turn_id=f"B{i}")
        for i in range(4):
            add(conn, f"[claude] hook {i}", vec_with_cos(0.93 - i * 0.001, other=10 + i),
                session_id="H", turn_id=f"H{i}")
        monkeypatch.setattr(processor, "embed_query", lambda t: basis(0))
        n3memory._SERVER_STATE["cfg"] = dict(cfg, min_results=0)
        mine = lambda res: [r for r in res["results"] if not r["weak"] and r["session_id"] == sid]  # noqa: E731
        assert len(mine(n3memory.api_search(n3memory.SearchRequest(query="q")))) == 4
        assert len(mine(n3memory.api_search(n3memory.SearchRequest(query="q", session_id=sid)))) == 2

    def test_free_slots_are_filled_with_current_session_rows(self, conn):
        # too few rows from other sessions: the free slots go back to the current session
        for i in range(6):
            add(conn, f"[claude] this session {i}", vec_with_cos(0.97 - i * 0.001, other=1 + i),
                session_id="S", turn_id=f"S{i}")
        add(conn, "[claude] other session", vec_with_cos(0.93, other=10), session_id="O", turn_id="O0")
        d = hybrid_search(conn, "anything", self._imp(min_results=0), session_id="S", embed_fn=lambda t: basis(0))
        assert [x["content"] for x in d["results"] if not x["weak"]] == [
            "[claude] this session 0", "[claude] this session 1", "[claude] this session 2",
            "[claude] this session 3", "[claude] other session"]

    def test_cap_per_turn_still_applies_to_filled_rows(self):
        rows = [{"turn_id": "S1", "session_id": "S"}, {"turn_id": "S2", "session_id": "S"},
                {"turn_id": "S1", "session_id": "S"}, {"turn_id": "S1", "session_id": "S"},
                {"turn_id": "O1", "session_id": "O"}]
        got = processor._cap_per_turn(rows, 5, session_id="S", session_cap=1)
        assert got == [rows[0], rows[1], rows[2], rows[4]]  # S1 keeps its limit of 2 rows

    def test_is_cjk_text(self):
        assert processor._is_cjk_text("[claude 1/2] ログの保存期間は 3 年です")
        assert not processor._is_cjk_text("[user] How long are logs kept?")
        assert not processor._is_cjk_text("The file 設定.json was renamed to config.json in the repository")
        assert not processor._is_cjk_text("")

    def test_best_other_language_row_is_rescued(self, conn):
        add(conn, "[claude] The backup runs at 03:30 every night.", vec_with_cos(0.81, other=1))
        add(conn, "[claude] The staging host moved last week.", vec_with_cos(0.80, other=2))
        add(conn, "[claude] ログは 3 年残します。", vec_with_cos(0.81, other=3))
        c = self._imp(min_results=0)
        q = "バックアップは何時に動く？"
        e0 = lambda t: basis(0)  # noqa: E731
        got = [x["content"] for x in hybrid_search(conn, q, c, embed_fn=e0)["results"]]
        assert got == ["[claude] The backup runs at 03:30 every night."]  # only the best, only the other script
        assert hybrid_search(conn, q, dict(c, cross_lang_min_cos=0), embed_fn=e0)["results"] == []
        assert hybrid_search(conn, q, dict(c, cross_lang_min_cos=0.82), embed_fn=e0)["results"] == []

    def test_clear_leader_below_the_floor_is_rescued(self, conn):
        e0 = lambda t: basis(0)  # noqa: E731
        add(conn, "[claude] the answer", vec_with_cos(0.815, other=1), turn_id="T1")
        add(conn, "[claude] something else", vec_with_cos(0.79, other=2), turn_id="T2")
        c = self._imp(min_results=0)
        got = [x["content"] for x in hybrid_search(conn, "q", c, embed_fn=e0)["results"]]
        assert got == ["[claude] the answer"]
        assert hybrid_search(conn, "q", dict(c, semantic_rescue_min_cos=0), embed_fn=e0)["results"] == []

    def test_no_rescue_when_the_nearest_lie_close_together(self, conn):
        e0 = lambda t: basis(0)  # noqa: E731
        add(conn, "[claude] one", vec_with_cos(0.815, other=1), turn_id="T1")
        add(conn, "[claude] two", vec_with_cos(0.805, other=2), turn_id="T2")
        add(conn, "[user] same turn as one", vec_with_cos(0.81, other=3), turn_id="T1")
        assert hybrid_search(conn, "q", self._imp(min_results=0), embed_fn=e0)["results"] == []

    def test_spec_settings_restore_the_spec_behaviour(self, conn):
        add(conn, "[claude] zebra here")
        spec = dict(self._imp(min_results=0), **n3memory.SPEC_SEARCH_SETTINGS)
        r = hybrid_search(conn, "zebra crossing station lights", spec, embed_fn=lambda t: basis(5))["results"]
        assert [x["content"] for x in r if not x["weak"]] == ["[claude] zebra here"]

    def test_new_keys_are_type_checked(self, isolate):
        n3memory._load_config()
        with open(n3memory.CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.update({"fts_stopwords": "yes", "kw_only_min_terms": -1, "top_matches_max": 2.5,
                    "cross_lang_min_cos": 1.5, "top_matches_session_max": -1})
        with open(n3memory.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        out = n3memory._load_config()
        assert out["fts_stopwords"] is True and out["kw_only_min_terms"] == 3
        assert out["top_matches_max"] == 5 and out["cross_lang_min_cos"] == 0.80
        assert out["top_matches_session_max"] == 2


class TestServerCheckAndArgs:
    """A stale server another hook already stopped, an exception in the server check, and option
    values that start with '-'."""

    def _stale(self):
        return {"status": "ok", "root": n3memory.HOME_DIR, "code_version": "0" * 16, "pid": 999999}

    def _refused(self, port=None):
        raise urllib.error.URLError("connection refused")

    def test_server_already_stopped_by_another_hook_is_no_server(self, cfg, monkeypatch):
        answers = [self._stale()]
        monkeypatch.setattr(n3memory, "_health", lambda port=None, timeout=5: answers.pop(0) if answers else None)
        monkeypatch.setattr(n3memory, "_request_shutdown", self._refused)
        calls = []
        monkeypatch.setattr(n3memory, "_no_health_path", lambda c: calls.append(c) or True)
        assert n3memory.ensure_server(cfg) is True
        assert calls and "stale_server" not in n3memory._read_notices()

    def test_new_server_already_started_by_another_hook_is_used(self, cfg, monkeypatch):
        ok = dict(self._stale(), code_version=n3memory._client_code_version())
        answers = [self._stale(), ok]
        monkeypatch.setattr(n3memory, "_health", lambda port=None, timeout=5: answers.pop(0) if answers else ok)
        monkeypatch.setattr(n3memory, "_request_shutdown", self._refused)
        monkeypatch.setattr(n3memory, "_no_health_path", lambda c: pytest.fail("no server path"))
        assert n3memory.ensure_server(cfg) is True
        assert "stale_server" not in n3memory._read_notices()

    def test_stale_server_that_cannot_be_stopped_still_gives_the_notice(self, cfg, monkeypatch):
        monkeypatch.setattr(n3memory, "_health", lambda port=None, timeout=5: self._stale())
        monkeypatch.setattr(n3memory, "_request_shutdown", self._refused)
        monkeypatch.setattr(n3memory, "_is_own_server_process", lambda pid, port=None: False)
        assert n3memory.ensure_server(cfg) is True
        assert "stale_server" in n3memory._read_notices()

    def test_reserve_pid_file_other_os_error_is_not_raised(self, isolate, monkeypatch):
        def denied(*a, **k):
            raise PermissionError(13, "Permission denied", n3memory.PID_FILE)
        monkeypatch.setattr(n3memory.os, "open", denied)
        assert n3memory._reserve_pid_file() is False

    def _boom(self, cfg=None):
        raise PermissionError(13, "Permission denied", n3memory.PID_FILE)

    def _contents(self):
        c = database.get_connection(n3memory.DB_PATH)
        database.init_db(c)
        try:
            return [r[0] for r in c.execute("SELECT content FROM memories ORDER BY rowid").fetchall()]
        finally:
            c.close()

    def test_exception_in_server_check_still_saves_the_prompt(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(n3memory, "ensure_server", self._boom)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s", "prompt": "remember the blue door"})))
        assert n3memory.cmd_hook_submit() == 0
        cap = capsys.readouterr()
        assert "memory search unavailable" in cap.out and "PermissionError" in cap.err
        assert self._contents() == ["[user] remember the blue door"]

    def test_exception_in_server_check_still_saves_the_reply(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(n3memory, "ensure_server", self._boom)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s", "last_assistant_message": "the door is blue"})))
        assert n3memory.cmd_save_claude_turn() == 0
        assert "PermissionError" in capsys.readouterr().err
        assert self._contents() == ["[claude] the door is blue"]

    def test_option_values_starting_with_a_dash(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(n3memory, "ensure_server", lambda cfg=None: False)
        assert n3memory.main(["--search", "--force"]) == 0
        assert "memory search unavailable" in capsys.readouterr().out
        assert n3memory.main(["--buffer", "-x"]) == 0
        assert self._contents() == ["-x"]
        assert n3memory._normalize_value_args(["--buffer", "-"]) == ["--buffer", "-"]
        assert n3memory._normalize_value_args(["--search", "--since", "2026-11-01"]) == \
            ["--search", "--since", "2026-11-01"]
        with pytest.raises(SystemExit):
            n3memory.main(["--search", "--since", "2026-11-01"])


# The backup command of the README and the specification ("Data Backup"); kept identical there.
BACKUP_CMD = ("python -c \"import os,shutil,sqlite3; h=os.environ.get('N3MC_HOME') or os.path.expanduser('~/.n3mc'); "
              "s=sqlite3.connect(os.path.join(h,'.memory','n3memory.db')); d=sqlite3.connect('n3memory.db'); "
              "s.backup(d); d.close(); s.close(); shutil.copy2(os.path.join(h,'config.json'),'config.json')\"")


class TestBackupInitRepairReembedNotices:
    """Backup while the DB is open, --init without a DB, a failed /repair batch, --reembed with a server
    that cannot be stopped, notices written at the same time, session ids that clean to one file name."""

    def test_backup_command_is_the_one_in_the_documents(self):
        for name in ("README.md", "README_JP.md", "N3MemoryCore_v1.9.4_Free_EN.md", "N3MemoryCore_v1.9.4_Free_JP.md"):
            with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as f:
                assert BACKUP_CMD in f.read(), name

    def test_backup_keeps_commits_still_in_the_wal(self, cfg, tmp_path):
        c = get_connection(n3memory.DB_PATH)
        init_db(c)
        try:
            for i in range(3):
                add(c, f"[claude] row {i}", basis(i))
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            rid = add(c, "[claude] the newest zebra fact", basis(3))
            new_id = c.execute("SELECT id FROM memories WHERE rowid = ?", (rid,)).fetchone()[0]
            # the connection stays open: the 4th record is only in the WAL, so a plain copy misses it
            naive = tmp_path / "naive.db"
            shutil.copyfile(n3memory.DB_PATH, naive)
            nc = sqlite3.connect(naive)
            try:
                assert nc.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3
            finally:
                nc.close()
            bk = tmp_path / "backup"
            bk.mkdir()
            code = BACKUP_CMD[len('python -c "'):-1]
            subprocess.run([sys.executable, "-c", code], cwd=bk, check=True,
                           env=dict(os.environ, N3MC_HOME=n3memory.HOME_DIR))
        finally:
            c.close()
        b = get_connection(str(bk / "n3memory.db"))
        try:
            assert count_memories(b) == 4
            row = b.execute("SELECT rowid, id, content FROM memories WHERE id = ?", (new_id,)).fetchone()
            assert row[2] == "[claude] the newest zebra fact"
            assert [r["rowid"] for r in search_fts(b, "zebra")] == [row[0]]
            assert database.get_vector(b, row[0]) is not None
        finally:
            b.close()
        with open(bk / "config.json", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["owner_id"] == cfg["owner_id"] and saved["local_id"] == cfg["local_id"]

    def test_init_stops_when_the_db_cannot_be_opened(self, isolate, capsys):
        settings = isolate["home"] / ".claude" / "settings.json"
        os.makedirs(n3memory.DB_PATH)  # a folder where the DB file should be
        assert n3memory.cmd_init() == 1
        cap = capsys.readouterr()
        assert "Saving will start" not in cap.out and "not registered" in cap.err
        assert not settings.exists()
        settings.parent.mkdir(exist_ok=True)
        settings.write_text('{"model": "x"}', encoding="utf-8")
        assert n3memory.cmd_init() == 1
        assert settings.read_text(encoding="utf-8") == '{"model": "x"}'
        os.rmdir(n3memory.DB_PATH)
        capsys.readouterr()
        assert n3memory.cmd_init() == 0
        assert "Saving will start from the next session." in capsys.readouterr().out
        assert "UserPromptSubmit" in json.loads(settings.read_text(encoding="utf-8"))["hooks"]

    def test_repair_reports_a_failed_write(self, conn, cfg, monkeypatch):
        add(conn, "[claude] no vector yet")
        n3memory._SERVER_STATE["cfg"] = cfg
        monkeypatch.setattr(n3memory, "embed_passage", lambda t, **k: [0.1] * 384)  # wrong dimension
        with pytest.raises(n3memory.HTTPException) as e:
            n3memory.api_repair()
        assert e.value.status_code == 500 and "repair stopped after 0 record(s)" in str(e.value.detail)
        assert len(find_unindexed_memories(conn, 10)) == 1
        monkeypatch.setattr(n3memory, "embed_passage", lambda t, **k: basis(0))
        assert n3memory.api_repair() == {"status": "ok", "count": 1}
        assert n3memory.api_repair() == {"status": "ok", "count": 0}  # nothing left: a normal 0

    def test_repair_command_exits_1_on_a_failed_repair(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(n3memory, "ensure_server", lambda cfg=None: True)

        def fail(path, body, **k):
            raise urllib.error.HTTPError("http://127.0.0.1/repair", 500, "Internal Server Error", {},
                                         io.BytesIO(b'{"detail": "repair stopped after 0 record(s): x"}'))
        monkeypatch.setattr(n3memory, "_post", fail)
        assert n3memory.cmd_repair() == 1
        assert "repair stopped after 0 record(s)" in capsys.readouterr().err

    def _reembed_setup(self, conn, monkeypatch, answers, shutdown):
        rid = add(conn, "[claude] keep me", basis(0))
        monkeypatch.setattr(n3memory, "_health", lambda port=None, timeout=5: answers.pop(0) if answers else None)
        monkeypatch.setattr(n3memory, "_request_shutdown", shutdown)
        monkeypatch.setattr(n3memory, "_wait_port_free", lambda port: False)
        monkeypatch.setattr(n3memory, "embed_passage", lambda t, **k: basis(5))
        return rid

    def _own(self):
        return {"status": "ok", "root": n3memory.HOME_DIR, "code_version": "x", "pid": 1}

    def test_reembed_stops_when_the_own_server_cannot_be_stopped(self, conn, cfg, monkeypatch, capsys):
        rid = self._reembed_setup(conn, monkeypatch, [self._own()] * 5, lambda port=None: False)
        assert n3memory.cmd_reembed() == 1
        assert "could not be stopped" in capsys.readouterr().err
        assert database.get_vector(conn, rid)[0] == pytest.approx(1.0)
        assert get_meta(conn, "reembed_model") is None and get_meta(conn, "reembed_cursor") is None

    def test_reembed_stops_when_the_server_outlives_the_wait(self, conn, cfg, monkeypatch):
        rid = self._reembed_setup(conn, monkeypatch, [self._own()] * 5, lambda port=None: True)
        assert n3memory.cmd_reembed() == 1
        assert database.get_vector(conn, rid)[0] == pytest.approx(1.0)

    def test_reembed_goes_on_when_another_call_stopped_the_server(self, conn, cfg, monkeypatch):
        def refused(port=None):
            raise urllib.error.URLError("connection refused")
        rid = self._reembed_setup(conn, monkeypatch, [self._own()], refused)
        assert n3memory.cmd_reembed() == 0
        assert database.get_vector(conn, rid)[5] == pytest.approx(1.0)

    def test_reembed_leaves_another_folders_server_alone(self, conn, cfg, monkeypatch):
        other = dict(self._own(), root=os.path.join(n3memory.HOME_DIR, "elsewhere"))
        rid = self._reembed_setup(conn, monkeypatch, [other] * 5,
                                  lambda port=None: pytest.fail("another folder's server was asked to stop"))
        assert n3memory.cmd_reembed() == 0
        assert database.get_vector(conn, rid)[5] == pytest.approx(1.0)

    def _race(self, monkeypatch, *calls):
        real = n3memory._read_notices
        bar = threading.Barrier(len(calls), timeout=0.5)

        def slow():
            d = real()
            try:
                bar.wait()  # both read before either writes, unless the read-modify-write is locked
            except threading.BrokenBarrierError:
                pass
            return d
        monkeypatch.setattr(n3memory, "_read_notices", slow)
        ts = [threading.Thread(target=f, args=a) for f, a in calls]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        monkeypatch.setattr(n3memory, "_read_notices", real)
        return real()

    def test_two_notices_at_once_are_both_kept(self, isolate, monkeypatch):
        got = self._race(monkeypatch, (n3memory._notify, ("a", "first")), (n3memory._notify, ("b", "second")))
        assert set(got) == {"a", "b"}

    def test_clearing_one_notice_keeps_one_added_at_once(self, isolate, monkeypatch):
        n3memory._notify("a", "first")
        got = self._race(monkeypatch, (n3memory._clear_notice, ("a",)), (n3memory._notify, ("b", "second")))
        assert set(got) == {"b"}

    def test_showing_notices_keeps_one_added_at_once(self, isolate, monkeypatch):
        n3memory._notify("a", "first")
        got = self._race(monkeypatch, (n3memory._take_notices, ()), (n3memory._notify, ("b", "second")))
        assert set(got) == {"a", "b"}

    def test_session_ids_that_clean_to_one_name_get_their_own_files(self, isolate):
        f = n3memory._turn_id_file
        assert f("chat.alpha") != f("chat_alpha")
        assert f("x" * 64 + "1") != f("x" * 64 + "2")
        u = "0b7f2a3c-1d4e-4f5a-9b6c-7d8e9f0a1b2c"
        assert os.path.basename(f(u)) == f"turn_id_{u}.txt"  # a plain session id keeps its plain name
        assert f(None) == n3memory.TURN_ID_FILE and f("") == n3memory.TURN_ID_FILE

    def test_reply_pairs_with_its_own_question_across_such_ids(self, cfg, monkeypatch):
        monkeypatch.setattr(n3memory, "ensure_server", lambda cfg=None: False)
        for sid, prompt in (("chat.alpha", "question A"), ("chat_alpha", "question B")):
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": sid, "prompt": prompt})))
            n3memory.cmd_hook_submit()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "chat.alpha",
                                                                   "last_assistant_message": "answer A"})))
        n3memory.cmd_save_claude_turn()
        c = get_connection(n3memory.DB_PATH)
        try:
            t = dict(c.execute("SELECT content, turn_id FROM memories").fetchall())
        finally:
            c.close()
        assert t["[claude] answer A"] == t["[user] question A"] != t["[user] question B"]

    def test_turn_files_with_the_long_name_are_cleaned_after_30_days(self, isolate):
        p = n3memory._turn_id_file("chat.alpha")
        n3memory._write_turn_id("chat.alpha", "t1")
        old = time.time() - 31 * 86400
        os.utime(p, (old, old))
        n3memory._housekeeping()
        assert not os.path.exists(p)


class TestTranscriptUnicodeLineBreaks:
    """Transcript lines end at LF only: a reply with a raw U+2028 / U+2029 / U+0085 is recovered whole."""

    def _raw(self, tmp_path, objs, newline="\n", prefix=b""):
        p = tmp_path / "raw.jsonl"
        body = newline.join(json.dumps(o, ensure_ascii=False) for o in objs) + newline
        p.write_bytes(prefix + body.encode("utf-8"))
        return str(p)

    @pytest.mark.parametrize("sep", [" ", " ", "\u0085"])
    def test_raw_separator(self, monkeypatch, tmp_path, capsys, sep):
        n3memory._write_turn_id("s1", "T1")
        reply = f"first part{sep}second part"
        tr = self._raw(tmp_path, [_u("q1"), _a(_t(reply)), _u("q2")])
        assert sep.encode("utf-8") in open(tr, "rb").read()
        assert n3memory._read_prev_reply(tr, "q2") == (reply, None)
        hooks = TestHookSubmit()
        hooks._submit(monkeypatch, "q2", tr)
        assert hooks._claude_rows() == [("[claude] " + reply, "T1")]

    def test_lf_and_crlf(self, tmp_path):
        tr = self._raw(tmp_path, [_u("q1"), _a(_t("line 1\nline 2")), _u("q2")])
        assert n3memory._read_prev_reply(tr, "q2") == ("line 1\nline 2", None)
        tr = self._raw(tmp_path, [_u("q1"), _a(_t("crlf reply")), _u("q2")], newline="\r\n")
        assert n3memory._read_prev_reply(tr, "q2") == ("crlf reply", None)

    def test_cut_first_line_and_broken_lines(self, monkeypatch, tmp_path):
        prefix = (b'{"type": "assistant", "message": {"content": [{"type": "text", "text": "cut"}]}}\n'
                  b"not json\n")
        tr = self._raw(tmp_path, [_u("q1"), _a(_t("tail reply")), _u("q2")], prefix=prefix)
        monkeypatch.setattr(n3memory, "TRANSCRIPT_TAIL", os.path.getsize(tr) - 10)
        assert n3memory._read_prev_reply(tr, "q2") == ("tail reply", None)
