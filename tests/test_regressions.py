"""Layer 5: regression tests for v1.4.0 - v1.9.1 fixes. The embedding is faked: no model, a few seconds."""
import io
import json
import os
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from n3memorycore import n3memory as n3m
from n3memorycore.core import database as db
from n3memorycore.core import processor as p

from .conftest import add, fake_embed

NOW = datetime.now(timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def search(conn, cfg, q, **kw):
    return p.hybrid_search(conn, q, cfg, embed_fn=fake_embed, **kw)


class TestFtsQuoting:
    @pytest.mark.parametrize("q", [r'C:\Users\test #tag = @a * $b % AND OR NOT "q"', "NEAR( x )", "a\x00b\x1fc",
                                   "!!! ??? ...", "AND", '"""'])
    def test_symbols_never_raise(self, conn, cfg, q):
        add(conn, cfg, "plain text about symbols and tags")
        db.search_fts(conn, q)
        search(conn, cfg, q)

    def test_symbol_word_hits(self, conn, cfg):
        add(conn, cfg, "path C:/Users/test/#tag here")
        assert db.search_fts(conn, "test/#tag")

    def test_hybrid_survives_fts_exception(self, conn, cfg, monkeypatch):
        add(conn, cfg, "the quick brown fox")
        monkeypatch.setattr(p, "search_fts", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
        assert search(conn, cfg, "the quick brown fox")["results"]

    def test_or_semantics(self, conn, cfg):
        add(conn, cfg, "the quick brown fox", embed=False)
        assert db.search_fts(conn, "what is the quick fox doing in my DB")

    def test_short_terms_dropped(self):
        assert db._build_fts_query("AI 東京") == ""

    def test_punctuation_splits_terms(self, conn, cfg):
        add(conn, cfg, "架空の惑星「アルファ9」の気温設定", embed=False)
        assert db.search_fts(conn, "「アルファ9」の気温は？")

    def test_cjk_terms_split_in_trigrams(self):
        q = db._build_fts_query("アルファ9の気温")
        assert '"アルフ"' in q and '"の気温"' in q and " OR " in q

    def test_max_terms(self):
        assert db._build_fts_query(" ".join(f"term{i:03d}" for i in range(80))).count(" OR ") == 39


class TestVectorFilter:
    def test_since_returns_in_range_records_outside_global_top_k(self, conn, cfg):
        for i in range(8):
            add(conn, cfg, f"fox story number {i}", ts="2020-01-01T00:00:00+00:00")
        add(conn, cfg, "fox story recent", ts="2026-06-01T00:00:00+00:00")
        hits = db.search_vector(conn, fake_embed("fox story"), k=3, since="2026-01-01T00:00:00+00:00")
        assert [h[0]["content"] for h in hits] == ["fox story recent"]

    def test_falls_back_to_exact_scan(self, conn, cfg, monkeypatch):
        add(conn, cfg, "fox story recent", ts="2026-06-01T00:00:00+00:00")
        real = conn.execute

        class Wrap:
            def __init__(self, c):
                self.c = c

            def execute(self, sql, *a):
                if "MATCH" in sql and "rowid IN" in sql:
                    raise sqlite3.OperationalError("unsupported")
                return self.c.execute(sql, *a)

            def __getattr__(self, n):
                return getattr(self.c, n)

        hits = db.search_vector(Wrap(conn), fake_embed("fox story"), k=3, since="2026-01-01T00:00:00+00:00")
        assert hits and hits[0][0]["content"] == "fox story recent"


class TestNoSimilarityDedup:
    def test_similar_chunks_all_saved(self, conn, cfg):
        for i in range(3):
            assert n3m._store_record(conn, cfg, f"[claude {i + 1}/3] 設定の値は{i}です。残りは同じ文章です。", turn_id="T",
                                     embed=False)["status"] == "ok"
        assert db.count_memories(conn) == 3

    def test_exact_scopes(self, conn, cfg):
        n3m._store_record(conn, cfg, "same", turn_id="A", embed=False)
        assert n3m._store_record(conn, cfg, "same", turn_id="A", embed=False)["status"] == "skipped"
        assert n3m._store_record(conn, cfg, "same", turn_id="B", embed=False)["status"] == "ok"
        n3m._store_record(conn, cfg, "manual", embed=False)
        assert n3m._store_record(conn, cfg, "manual", embed=False)["status"] == "skipped"


class TestSearchShaping:
    def test_vector_min_cos_drops_unrelated(self, conn, cfg):
        add(conn, cfg, "alpha beta gamma delta")
        add(conn, cfg, "zzz qqq www xxx")
        res = search(conn, cfg, "alpha beta gamma delta", )["results"]
        assert [r["content"] for r in res if not r["weak"]] == ["alpha beta gamma delta"]

    def test_old_other_session_record_survives_relative_cut(self, conn, cfg):
        base = "unique zebra aardvark sentence about fast river crossing during dawn light today"
        add(conn, cfg, base.replace("today", "now"), session_id="s1")
        add(conn, cfg, base, session_id="s2", ts=ago(30))  # 30 days old, other session, cos ~0.9
        res = [r for r in search(conn, cfg, base.replace("today", "now"), session_id="s1")["results"]
               if not r["weak"]]
        assert len(res) == 2 and res[0]["session_id"] == "s1"

    def test_other_session_keyword_only_hit_survives_min_score(self, conn, cfg):
        add(conn, cfg, "kiwi marmalade ledger", session_id="other", embed=False)
        res = search(conn, cfg, "kiwi marmalade ledger", session_id="mine")["results"]
        assert res and res[0]["cos_sim"] == 0.0 and not res[0]["weak"]

    def test_old_records_are_returned(self, conn, cfg):
        add(conn, cfg, "vintage zebra aardvark record", ts=ago(400))
        res = search(conn, cfg, "vintage zebra aardvark record")["results"]
        assert res and not res[0]["weak"]

    def test_newer_ranks_above_older_same_similarity(self, conn, cfg):
        base = "duplicate topic about zebra stripes seen near the wide river bank today"
        add(conn, cfg, base, ts=ago(400), turn_id="old")
        add(conn, cfg, base.replace("today", "now"), ts=ago(1), turn_id="new")
        res = [r for r in search(conn, cfg, base)["results"] if not r["weak"]]
        assert len(res) == 2 and res[0]["turn_id"] == "new"

    def test_pair_top_n_and_max_chars(self, conn, cfg):
        for t in "ABCDE":
            add(conn, cfg, f"[user] zebra question {t}", turn_id=t)
            add(conn, cfg, f"[claude] " + ("answer " * 100), turn_id=t)
        out = search(conn, cfg, "zebra question")
        assert len(out["pairs"]) <= cfg["pair_top_n"]
        assert sum(len(s["content"]) for v in out["pairs"].values() for s in v) <= cfg["pair_max_chars"]

    def test_pairs_omitted_when_first_row_too_big(self, conn, cfg):
        cfg = {**cfg, "pair_max_chars": 10}
        add(conn, cfg, "[user] zebra question about stripes", turn_id="A")
        out = search(conn, cfg, "zebra question about stripes")
        assert out["pairs"] == {} and out["pairs_omitted"] == {"A": 1}

    def test_version_ordering_newest_first(self, conn, cfg):
        body = "設定の本文 " * 30
        add(conn, cfg, "[user] 設定を作成して", turn_id="old", ts=ago(5))
        add(conn, cfg, "[claude] " + body + "旧", turn_id="old", ts=ago(5))
        add(conn, cfg, "[user] 設定を作成して", turn_id="new", ts=ago(1))
        add(conn, cfg, "[claude] " + body + "新", turn_id="new", ts=ago(1))
        res = search(conn, cfg, "設定を作成して 設定の本文")["results"]
        turns = [r["turn_id"] for r in res if r["turn_id"]]
        assert turns.index("new") < turns.index("old")

    def test_identical_content_collapsed_keeping_newest(self, conn, cfg):
        add(conn, cfg, "same words everywhere here", turn_id="a", ts=ago(9))
        add(conn, cfg, "same words everywhere here", turn_id="b", ts=ago(1))
        res = [r for r in search(conn, cfg, "same words everywhere here")["results"]]
        assert len(res) == 1 and res[0]["turn_id"] == "b"

    def test_at_most_two_chunks_per_turn(self, conn, cfg):
        for i in range(5):
            add(conn, cfg, f"[claude {i + 1}/5] zebra stripes chunk {i}", turn_id="T")
        res = search(conn, cfg, "zebra stripes chunk")["results"]
        assert len([r for r in res if r["turn_id"] == "T"]) <= 2


class TestRender:
    def _res(self, **kw):
        base = {"id": "1", "content": "hello", "score": 0.5, "timestamp": "2026-11-13T01:34:56.123456+00:00",
                "turn_id": "T", "agent_name": "claude-code", "weak": False}
        return {**base, **kw}

    def test_saved_is_local_time_with_offset(self):
        out = p.render_memory_context("q", [self._res()])
        assert "saved=" in out and "(UTC" in out and "newest `saved` time" in out

    def test_omitted_line_names_show_turn(self):
        out = p.render_memory_context("q", [self._res()], {"T": [{"id": "1", "content": "x", "timestamp":
                                      "2026-11-13T01:34:56+00:00"}]}, {"T": 3})
        assert "3 more chunk(s) of this exchange omitted" in out and "--show-turn T" in out

    def test_notice_comes_first(self):
        out = p.render_memory_context("q", [self._res()], notices=["fix me"])
        assert out.index("N3MC notice: fix me") < out.index("## Top matches")

    def test_weak_after_top_and_numbering_continues(self):
        out = p.render_memory_context("q", [self._res(), self._res(weak=True, content="weakone")])
        assert out.index("## Top matches") < out.index("## Possibly related") < out.index("weakone")
        assert "## [1]" in out and "## [2]" in out

    def test_only_weak(self):
        out = p.render_memory_context("q", [self._res(weak=True)])
        assert "_No relevant memories found._" in out and "## [1]" in out and "## Top matches" not in out

    def test_no_weak_no_heading(self):
        assert "Possibly related" not in p.render_memory_context("q", [self._res()])


class TestNoContextFile:
    def test_search_does_not_create_memory_context(self, conn, capsys):
        n3m.cmd_search("anything")
        assert not os.path.exists(n3m.CONTEXT_FILE)

    def _claude_md(self, tmp_path, monkeypatch, text):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".claude").mkdir(exist_ok=True)
        f = tmp_path / ".claude" / "CLAUDE.md"
        f.write_bytes(text.encode("utf-8"))
        return f

    def test_removes_own_import_with_comment_crlf_and_backup(self, tmp_path, monkeypatch):
        own = n3m.CONTEXT_FILE
        text = (f"# Mine\r\n\r\n# N3MemoryCore — memory\r\n# Path resolves to x\r\n# If you have not yet run it\r\n\r\n"
                f"@{own}\r\nkeep this\r\n")
        f = self._claude_md(tmp_path, monkeypatch, text)
        n3m._retire_memory_context()
        out = f.read_bytes().decode("utf-8")
        assert "memory_context" not in out and "keep this\r\n" in out and "# Mine" in out
        assert list((tmp_path / ".claude").glob("CLAUDE.md.bak-n3mc-*"))

    def test_slash_variant_and_spaces(self, tmp_path, monkeypatch):
        own = n3m.CONTEXT_FILE.replace("\\", "/")
        f = self._claude_md(tmp_path, monkeypatch, f"@{own}\nrest\n")
        n3m._retire_memory_context()
        assert f.read_text(encoding="utf-8") == "rest\n"

    def test_other_imports_kept(self, tmp_path, monkeypatch):
        text = "@../.memory/memory_context.md\n@C:/other/.memory/memory_context.md\n@docs/notes_about_memory_context.md\n"
        f = self._claude_md(tmp_path, monkeypatch, text)
        n3m._retire_memory_context()
        assert f.read_text(encoding="utf-8") == text
        assert not list((tmp_path / ".claude").glob("*.bak-n3mc-*"))

    def test_old_file_deleted_and_marker_created(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        open(n3m.CONTEXT_FILE, "w").write("old")
        n3m._retire_memory_context()
        assert not os.path.exists(n3m.CONTEXT_FILE) and os.path.exists(n3m.CONTEXT_RETIRED_MARKER)


class TestBehaviorMd:
    def _p(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        return tmp_path / ".claude" / "rules" / "n3mc-behavior.md"

    def test_current_untouched(self, tmp_path, monkeypatch):
        f = self._p(tmp_path, monkeypatch)
        n3m._ensure_behavior_md()
        before = f.read_bytes()
        assert n3m._ensure_behavior_md() == "current" and f.read_bytes() == before

    def test_known_old_template_replaced_with_backup(self, tmp_path, monkeypatch):
        import hashlib
        f = self._p(tmp_path, monkeypatch)
        f.parent.mkdir(parents=True)
        old = "OLD TEMPLATE\n"
        f.write_text(old, encoding="utf-8")
        monkeypatch.setattr(n3m, "_KNOWN_BEHAVIOR_MD_HASHES", {hashlib.sha256(old.encode()).hexdigest()[:16]})
        assert n3m._ensure_behavior_md() == "updated"
        assert f.read_text(encoding="utf-8") == n3m._BEHAVIOR_MD_CONTENT
        assert list(f.parent.glob("*.bak-n3mc-*"))

    def test_hand_edited_is_not_replaced_and_notices_per_file(self, tmp_path, monkeypatch):
        f = self._p(tmp_path, monkeypatch)
        f.parent.mkdir(parents=True)
        f.write_text("my own rules\n", encoding="utf-8")
        assert n3m._ensure_behavior_md() == "edited" and f.read_text(encoding="utf-8") == "my own rules\n"
        keys = [k for k in n3m._notices_read() if k.startswith("behavior_md_edited:")]
        assert len(keys) == 1 and len(keys[0].split(":")[1]) == 12
        assert str(f) in n3m._notices_read()[keys[0]]["message"]
        f.write_text(n3m._BEHAVIOR_MD_CONTENT, encoding="utf-8")
        n3m._ensure_behavior_md()
        assert not [k for k in n3m._notices_read() if k.startswith("behavior_md_edited:")]

    def test_template_has_marker_and_weak_matches(self):
        t = n3m._BEHAVIOR_MD_CONTENT
        assert t.startswith("<!-- n3mc-behavior v3 -->\n") and "## Weak Matches" in t and t.endswith("\n")


class TestServerDecision:
    def test_decisions(self, monkeypatch):
        cv = n3m.compute_code_version()
        assert n3m._health_decision(None) == "none"
        assert n3m._health_decision({"status": "ok", "root": n3m.HOME_DIR, "code_version": cv}) == "ok"
        assert n3m._health_decision({"status": "ok", "root": n3m.HOME_DIR, "code_version": "x"}) == "stale"
        assert n3m._health_decision({"status": "ok", "root": "/elsewhere", "code_version": cv}) == "foreign"
        assert n3m._health_decision({"status": "ok"}) == "legacy"

    def test_code_version_changes_with_config_content_only(self):
        n3m._load_config()
        a = n3m.compute_code_version()
        cfg = json.load(open(n3m.CONFIG_FILE))
        open(n3m.CONFIG_FILE, "w").write(json.dumps(cfg, indent=8, sort_keys=True))
        assert n3m.compute_code_version() == a
        cfg["min_score"] = 0.3
        open(n3m.CONFIG_FILE, "w").write(json.dumps(cfg))
        assert n3m.compute_code_version() != a

    def test_stale_server_stopped_with_shutdown_first(self, monkeypatch):
        calls = []
        monkeypatch.setattr(n3m, "_get_health", lambda port=None: {"status": "ok", "root": n3m.HOME_DIR,
                                                                    "code_version": "old", "pid": 1})
        monkeypatch.setattr(n3m, "_request_shutdown", lambda *a, **k: calls.append("shutdown") or True)
        monkeypatch.setattr(n3m, "_wait_port_free", lambda *a, **k: True)
        monkeypatch.setattr(n3m, "_launch", lambda: calls.append("launch") or True)
        assert n3m.ensure_server() is True and calls == ["shutdown", "launch"]

    def test_foreign_server_not_used_or_stopped(self, monkeypatch):
        monkeypatch.setattr(n3m, "_get_health", lambda port=None: {"status": "ok", "root": "/other/home",
                                                                    "code_version": "x", "pid": 1})
        called = []
        monkeypatch.setattr(n3m, "_request_shutdown", lambda *a, **k: called.append(1))
        assert n3m.ensure_server() is False and n3m._FOREIGN_SERVER_ROOT == "/other/home" and not called
        assert "port_conflict" in n3m._notices_read()

    def test_dead_pid_file_is_removed_without_waiting(self, monkeypatch):
        open(n3m.PID_FILE, "w").write("999999")
        monkeypatch.setattr(n3m, "_get_health", lambda port=None: None)
        monkeypatch.setattr(n3m, "_launch", lambda: True)
        t = time.time()
        assert n3m.ensure_server() is True and time.time() - t < 5 and not os.path.exists(n3m.PID_FILE)

    def test_shutdown_endpoint_checks_root(self, use_fake_embed, monkeypatch):
        monkeypatch.setattr(n3m, "_exit_server", lambda: None)
        c = TestClient(n3m.create_app(), base_url="http://127.0.0.1:18520")
        assert c.post("/shutdown", json={"root": "/wrong"}).status_code == 403
        assert c.post("/shutdown", json={"root": n3m.HOME_DIR}).json() == {"status": "ok"}

    def test_host_check_uses_the_servers_own_port(self, use_fake_embed, monkeypatch):
        monkeypatch.setattr(n3m, "_SERVER_PORT", 19999)
        c = TestClient(n3m.create_app(), base_url="http://127.0.0.1:19999")
        assert c.get("/health").status_code == 200  # config.json says 18520, the server listens on 19999


class TestServerStart:
    def test_early_exit_is_reported_with_server_log(self, monkeypatch):
        class Proc:
            pid, returncode = 4242, 3

            def poll(self):
                return 3

        monkeypatch.setattr(n3m, "_start_server", _real_start_server)
        monkeypatch.setattr(n3m.subprocess, "Popen", lambda *a, **k: Proc())
        monkeypatch.setattr(n3m, "_get_health", lambda port=None: None)
        assert n3m._start_server() is False
        assert "server_start_failed" in n3m._notices_read() and n3m.SERVER_LOG in n3m._notices_read()[
            "server_start_failed"]["message"]
        assert os.path.exists(n3m.SERVER_START_FAILED_FILE)
        monkeypatch.setattr(n3m, "_start_server", lambda: (_ for _ in ()).throw(AssertionError("retried")))
        assert n3m._launch() is False  # within 5 minutes: not tried again

    def test_popen_gets_detach_flags_and_server_log(self, monkeypatch):
        seen = {}

        class Proc:
            pid, returncode = 4243, None

            def poll(self):
                return None

        def fake_popen(cmd, **kw):
            seen.update(cmd=cmd, **kw)
            return Proc()

        monkeypatch.setattr(n3m, "_start_server", _real_start_server)
        monkeypatch.setattr(n3m.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(n3m, "SERVER_WAIT_SECONDS", 0)
        n3m._start_server()
        assert seen["cmd"][-3:] == ["-m", "n3memorycore.n3memory", "--run-server"] or seen["cmd"][-1] == "--run-server"
        if os.name == "nt":
            assert seen["creationflags"] & n3m.subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            assert seen["start_new_session"] is True
        assert seen["stderr"].name == n3m.SERVER_LOG


_real_start_server = n3m._start_server


class TestServerApi:
    @pytest.fixture
    def app(self, use_fake_embed):
        return n3m.create_app()

    def test_bad_host_403_everywhere(self, app):
        c = TestClient(app, base_url="http://evil.example")
        for m, path in (("get", "/health"), ("post", "/buffer"), ("post", "/search"), ("post", "/repair"),
                        ("get", "/list")):
            assert getattr(c, m)(path).status_code == 403

    def test_localhost_host_allowed(self, app):
        assert TestClient(app, base_url="http://localhost:18520").get("/health").status_code == 200

    def test_origin_header_403_even_with_good_host(self, app):
        c = TestClient(app, base_url="http://127.0.0.1:18520")
        assert c.get("/health", headers={"Origin": "http://evil.example"}).status_code == 403
        assert c.get("/health", headers={"Origin": "null"}).status_code == 403
        assert c.get("/health").status_code == 200

    def test_endpoints_are_plain_def(self, app):
        import inspect
        for r in app.routes:
            if getattr(r, "path", "") in ("/buffer", "/search", "/repair", "/list"):
                assert not inspect.iscoroutinefunction(r.endpoint)

    def test_repair_max_records(self, app, conn, cfg):
        for i in range(5):
            add(conn, cfg, f"unindexed {i}", embed=False)
        c = TestClient(app, base_url="http://127.0.0.1:18520")
        assert c.post("/repair", json={"max_records": 2}).json()["count"] == 2
        assert c.post("/repair", json={}).json()["count"] == 3

    def test_client_does_not_use_proxy_and_sends_no_origin(self, monkeypatch):
        import http.server
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

            def log_message(self, *a):
                pass

        real_get = n3m._http  # not the patched _get
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        proxy = http.server.HTTPServer(("127.0.0.1", 0), H)
        for s in (srv, proxy):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
        assert real_get("GET", "/health", port=srv.server_port) == {"status": "ok"}
        srv.shutdown()
        proxy.shutdown()
        assert len(seen) == 1 and "Origin" not in seen[0]


class TestForeignServer:
    def test_saves_go_to_own_db_directly_and_search_is_off(self, conn, monkeypatch, capsys):
        monkeypatch.setattr(n3m, "_SERVER_OK", False)
        monkeypatch.setattr(n3m, "_FOREIGN_SERVER_ROOT", "/other")
        assert n3m._save_record("[user] direct save")["status"] == "ok"
        assert db.count_memories(conn) == 1 and conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 0
        n3m.cmd_search("direct save")
        assert "memory search unavailable: another N3MC folder's server is using port" in capsys.readouterr().out

    def test_repair_does_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(n3m, "_SERVER_OK", False)
        monkeypatch.setattr(n3m, "_FOREIGN_SERVER_ROOT", "/other")
        assert n3m.cmd_repair() == 0 and "skipped" in capsys.readouterr().err


class TestConfig:
    def test_unreadable_config_is_not_overwritten(self):
        os.makedirs(n3m.HOME_DIR, exist_ok=True)
        open(n3m.CONFIG_FILE, "w").write("{ not json")
        os.utime(n3m.CONFIG_FILE, (1, 1))
        cfg = n3m._load_config()
        assert open(n3m.CONFIG_FILE).read() == "{ not json" and cfg["server_port"] == 18520
        assert "config_unreadable" in n3m._notices_read()

    def test_bad_values_ignored_individually(self):
        n3m._load_config()
        cfg = json.load(open(n3m.CONFIG_FILE))
        cfg.update({"vector_min_cos": "0.83", "search_query_max_chars": 2000.0, "half_life_days": 0,
                    "server_port": 70000, "min_score": 1.5, "min_results": -1, "search_result_limit": 1366,
                    "pair_top_n": 5})
        open(n3m.CONFIG_FILE, "w").write(json.dumps(cfg))
        out = n3m._load_config()
        assert out["vector_min_cos"] == n3m.DEFAULT_CONFIG["vector_min_cos"] and out["search_query_max_chars"] == 2000 and out["half_life_days"] == 90
        assert out["server_port"] == 18520 and out["min_score"] == 0.2 and out["min_results"] == 3
        assert out["search_result_limit"] == 20 and out["pair_top_n"] == 5
        assert json.load(open(n3m.CONFIG_FILE))["vector_min_cos"] == "0.83"  # file not rewritten
        msg = n3m._notices_read()["config_type"]["message"]
        assert all(k in msg for k in ("vector_min_cos", "half_life_days", "search_result_limit"))

    def test_accepts_boundaries_and_fix_clears_notice(self):
        n3m._load_config()
        cfg = json.load(open(n3m.CONFIG_FILE))
        cfg.update({"min_results": 0, "search_result_limit": 1365})
        open(n3m.CONFIG_FILE, "w").write(json.dumps(cfg))
        out = n3m._load_config()
        assert out["min_results"] == 0 and out["search_result_limit"] == 1365
        assert "config_type" not in n3m._notices_read()

    def test_k_4095_vector_search_works(self, conn, cfg):
        add(conn, cfg, "a")
        assert db.search_vector(conn, fake_embed("a"), k=4095)

    def test_exclusive_create_keeps_the_winners_ids(self, monkeypatch):
        n3m._load_config()
        first = json.load(open(n3m.CONFIG_FILE))["owner_id"]
        monkeypatch.setattr(n3m, "_CFG_CACHE", None)
        assert n3m._load_config()["owner_id"] == first

    def test_ids_recovered_from_db(self, conn, cfg):
        os.unlink(n3m.CONFIG_FILE)
        add(conn, cfg, "x")
        assert n3m._load_config()["owner_id"] == cfg["owner_id"]

    def test_update_goes_through_os_replace(self, monkeypatch):
        n3m._load_config()
        cfg = json.load(open(n3m.CONFIG_FILE))
        del cfg["min_results"]
        open(n3m.CONFIG_FILE, "w").write(json.dumps(cfg))
        seen = []
        real = os.replace
        monkeypatch.setattr(os, "replace", lambda a, b: seen.append(b) or real(a, b))
        n3m._load_config()
        assert n3m.CONFIG_FILE in seen and json.load(open(n3m.CONFIG_FILE))["min_results"] == 3


def _transcript(tmp_path, rows):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return str(f)


def U(text, **kw):
    return {"type": "user", "message": {"role": "user", "content": text}, **kw}


def A(*blocks, **kw):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        b if isinstance(b, dict) else {"type": "text", "text": b} for b in blocks]}, **kw}


class TestHookSubmit:
    def test_reads_last_text_block_only(self, tmp_path):
        t = _transcript(tmp_path, [U("q1"), A("first", {"type": "tool_use"}, "last"), U("q2")])
        assert n3m._last_assistant_text(t, "q2") == "last"

    def test_skips_tool_results_meta_commands_thinking_sidechain(self, tmp_path):
        rows = [U("q1"), A("the answer"),
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
                U("meta", isMeta=True), U("<command-name>/x</command-name>"), U("<local-command-stdout>o"),
                A({"type": "thinking", "thinking": "hmm"}), A("side answer", isSidechain=True), U("q2")]
        assert n3m._last_assistant_text(_transcript(tmp_path, rows), "q2") == "the answer"

    def test_interrupted_reply_is_found(self, tmp_path):
        rows = [U("q1"), A("partial reply"), U("[Request interrupted by user]"), U("q2")]
        assert n3m._last_assistant_text(_transcript(tmp_path, rows), "q2") == "partial reply"

    def test_only_the_trailing_identical_prompt_is_skipped(self, tmp_path):
        rows = [U("A reply exists"), A("reply to the earlier one"), U("same question"), U("same question")]
        # the stopped question was sent again: the earlier (stopped) one has no reply
        assert n3m._last_assistant_text(_transcript(tmp_path, rows), "same question") is None

    def test_missing_or_broken_transcript_is_audited_and_hook_still_saves(self, conn, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(
            {"session_id": "s", "prompt": "hello there", "transcript_path": "/no/such/file"}).encode()), encoding="utf-8"))
        assert n3m.cmd_hook_submit() == 0
        assert "# Recalled Memory Context" in capsys.readouterr().out
        assert [r[0] for r in conn.execute("SELECT content FROM memories")] == ["[user] hello there"]
        assert "transcript_unreadable" in open(n3m.AUDIT_LOG, encoding="utf-8").read()

    def test_previous_reply_saved_with_previous_turn_id_and_not_doubled(self, conn, tmp_path, monkeypatch, capsys):
        n3m._write_turn_id("s", "TURN-1")
        t = _transcript(tmp_path, [U("q1"), A("the previous answer"), U("q2")])

        def run():
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(
                {"session_id": "s", "prompt": "q2", "transcript_path": t}).encode()), encoding="utf-8"))
            n3m.cmd_hook_submit()

        n3m._write_turn_id("s", "TURN-1")
        run()
        rows = conn.execute("SELECT content, turn_id FROM memories ORDER BY rowid").fetchall()
        assert (rows[0][0], rows[0][1]) == ("[claude] the previous answer", "TURN-1")
        assert rows[1][0] == "[user] q2" and rows[1][1] != "TURN-1"
        n3m._write_turn_id("s", "TURN-1")  # Stop had already saved it: same chunks, same turn -> skipped
        run()
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content='[claude] the previous answer'").fetchone()[0] == 1

    def test_system_prompts_are_skipped_and_turn_id_kept(self, conn, monkeypatch, capsys):
        n3m._write_turn_id("s", "KEEP")
        for pre in ("<task-notification>x", "<agent-message from=\"a\">", "<system-reminder>x"):
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(
                {"session_id": "s", "prompt": pre}).encode()), encoding="utf-8"))
            n3m.cmd_hook_submit()
        assert db.count_memories(conn) == 0 and n3m._read_turn_id("s") == "KEEP"
        assert '"reason": "system_prompt"' in open(n3m.AUDIT_LOG, encoding="utf-8").read()

    def test_stop_save_does_not_remove_turn_id(self, conn, monkeypatch):
        n3m._write_turn_id("s", "T9")
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(
            {"session_id": "s", "last_assistant_message": "the reply"}).encode()), encoding="utf-8"))
        assert n3m.cmd_save_claude_turn() == 0
        assert n3m._read_turn_id("s") == "T9"
        assert conn.execute("SELECT turn_id FROM memories").fetchone()[0] == "T9"

    def test_image_only_prompt_writes_a_new_turn_id(self, conn, monkeypatch, capsys):
        n3m._write_turn_id("s", "OLD")
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(
            {"session_id": "s", "prompt": [{"type": "image", "source": {}}]}).encode()), encoding="utf-8"))
        n3m.cmd_hook_submit()
        assert n3m._read_turn_id("s") not in (None, "OLD") and db.count_memories(conn) == 0

    def test_turn_id_filename_is_sanitised(self):
        assert os.path.dirname(n3m._turn_id_path("../../evil/x")) == n3m.MEMORY_DIR
        assert os.path.basename(n3m._turn_id_path("a" * 100)) == "turn_id_" + "a" * 64 + ".txt"


class TestPurifyBeforeChunk:
    def test_text_between_two_code_blocks_survives(self, conn):
        text = "intro " * 20 + "\n```\n" + "code1\n" * 20 + "```\n" + "middle sentence stays. " * 12 + \
               "\n```\n" + "code2\n" * 20 + "```\n" + "outro"
        assert len(text) > 500
        n3m._save_text("user", text, "T")
        stored = " ".join(r[0] for r in conn.execute("SELECT content FROM memories"))
        assert "middle sentence stays." in stored and "code1" not in stored and "code2" not in stored

    def test_long_code_block_not_stored(self, conn):
        n3m._save_text("user", "before\n```\n" + "x = 1\n" * 200 + "```\nafter", "T")
        stored = " ".join(r[0] for r in conn.execute("SELECT content FROM memories"))
        assert "x = 1" not in stored and "[code omitted]" in stored


class TestChunkSentences:
    def test_japanese_text_splits_after_sentence_ends(self):
        text = "".join(f"これは{i}番目の文章です。" for i in range(80))
        chunks = p.chunk_text(text)
        assert len(chunks) > 1 and all(len(c) <= 400 for c in chunks)
        assert chunks[0].endswith("。")  # cut after a sentence end, not in the middle of a sentence


class TestTimeBounds:
    def test_date_only_since_is_local_midnight_in_utc(self):
        want = datetime(2026, 11, 13).astimezone(timezone.utc).isoformat()
        assert n3m._parse_time_bound("2026-11-13") == want

    def test_until_is_end_of_day(self):
        want = datetime(2026, 11, 13, 23, 59, 59, 999999).astimezone(timezone.utc).isoformat()
        assert n3m._parse_time_bound("2026-11-13", upper=True) == want

    def test_offset_is_kept(self):
        assert n3m._parse_time_bound("2026-11-13T09:00+09:00") == "2026-11-13T00:00:00+00:00"
        assert n3m._parse_time_bound("2026-11-13T00:00:00Z") == "2026-11-13T00:00:00+00:00"

    def test_t_and_space_same(self):
        assert n3m._parse_time_bound("2026-11-13T09:00") == n3m._parse_time_bound("2026-11-13 09:00")

    def test_garbage_exits_2_with_one_line(self, capsys):
        assert n3m.cmd_search("x", since="yesterday") == 2
        assert "--since: cannot read 'yesterday' (use 2026-11-13 or 2026-11-13T09:00)" in capsys.readouterr().err
        assert n3m.cmd_list(until="2026-13-01") == 2
        assert "--until: cannot read '2026-13-01'" in capsys.readouterr().err
        assert n3m.cmd_list(since="2026-12-01", until="2026-11-01") == 2


class TestDbCorrupt:
    def _make_db(self):
        c = n3m._open_db()
        c.execute("INSERT INTO memories(id, content, timestamp, owner_id) VALUES ('1','x','t','o')")
        c.commit()
        c.close()

    def test_garbage_file_is_set_aside_and_noticed(self):
        open(n3m.DB_PATH, "wb").write(b"this is not a sqlite database" * 100)
        c = n3m._open_db("integrity_check")
        assert db.count_memories(c) == 0
        c.close()
        baks = [f for f in os.listdir(n3m.MEMORY_DIR) if ".corrupt-" in f and f.endswith(".bak")]
        assert len(baks) == 1 and "db_corrupt" in n3m._notices_read()

    def test_second_corruption_keeps_both_backups_and_wal_moves(self):
        for _ in range(2):
            for ext, data in (("", b"junk"), ("-wal", b"wal"), ("-shm", b"shm")):
                open(n3m.DB_PATH + ext, "wb").write(data)
            n3m._quarantine_db()
        names = os.listdir(n3m.MEMORY_DIR)
        baks = sorted(f for f in names if ".corrupt-" in f and f.endswith(".bak"))
        assert len(baks) == 2 and baks[0].endswith("-2.bak") or baks[1].endswith("-2.bak")
        assert len([f for f in names if f.endswith(".bak-wal")]) == 2 and len([f for f in names if f.endswith(".bak-shm")]) == 2
        assert not os.path.exists(n3m.DB_PATH + "-wal")

    def test_quick_check_once_per_process(self, monkeypatch):
        seen = []
        real = n3m._connect_checked
        monkeypatch.setattr(n3m, "_connect_checked", lambda pragma=None: seen.append(pragma) or real(pragma))
        for _ in range(3):
            n3m._open_db_direct().close()
        assert seen == ["quick_check", None, None]

    def test_operational_error_is_not_corruption(self):
        assert not n3m._is_corruption_error(sqlite3.OperationalError("database is locked"))
        assert not n3m._is_corruption_error(RuntimeError("sqlite-vec ... Recovery: pip install sqlite-vec"))
        assert n3m._is_corruption_error(sqlite3.DatabaseError("file is not a database"))
        assert n3m._is_corruption_error(sqlite3.DatabaseError("database disk image is malformed"))

    def test_direct_save_after_corruption_works(self):
        open(n3m.DB_PATH, "wb").write(b"junk" * 1000)
        assert n3m._buffer_direct("[user] after corruption")["status"] == "ok"


class TestSaveFailedNotice:
    """A save through the server that ends ok / skipped clears save_failed (2026-11-22, F2)."""

    def _keys(self):
        if not os.path.exists(n3m.NOTICES_FILE):
            return set()
        return set(json.load(open(n3m.NOTICES_FILE, encoding="utf-8")))

    @pytest.mark.parametrize("status", ["ok", "skipped"])
    def test_server_save_clears_save_failed(self, monkeypatch, status):
        n3m._notify("save_failed", "A record could not be saved (test).")
        assert "save_failed" in self._keys()
        monkeypatch.setattr(n3m, "_use_server", lambda: True)
        monkeypatch.setattr(n3m, "_post", lambda path, body, *a, **k: {"status": status, "count": 1})
        assert n3m._save_record("[user] hello", turn_id="T1")["status"] == status
        assert "save_failed" not in self._keys()


class TestInit:
    def _settings(self):
        import pathlib
        return pathlib.Path(os.path.expanduser("~/.claude/settings.json"))

    def _own(self, s, event):
        return [h for e in s["hooks"][event] for h in e["hooks"]]

    def test_hooks_carry_timeout_120(self):
        """Both entries carry "timeout": 120 and a re-run adds nothing (2026-11-22, F1)."""
        n3m.cmd_init()
        n3m.cmd_init()
        s = json.loads(self._settings().read_text(encoding="utf-8"))
        for event in ("UserPromptSubmit", "Stop"):
            hs = self._own(s, event)
            assert len(hs) == 1 and hs[0]["timeout"] == 120

    def test_old_entry_without_timeout_is_replaced(self):
        sp = self._settings()
        sp.parent.mkdir(parents=True)
        sp.write_text(json.dumps({"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "C:/Tools/n3mc-hook.exe"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "python -m n3memorycore.n3mc_stop_hook"}]}]}}))
        n3m.cmd_init()
        s = json.loads(sp.read_text(encoding="utf-8"))
        for event in ("UserPromptSubmit", "Stop"):
            hs = self._own(s, event)
            assert len(hs) == 1 and hs[0]["timeout"] == 120

    def test_fresh_install_registers_hooks_and_rules(self, capsys):
        assert n3m.cmd_init() == 0
        s = json.loads(self._settings().read_text(encoding="utf-8"))
        cmds = [h["command"] for e in s["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
        assert len(cmds) == 1 and "n3mc" in cmds[0].lower() and "\\" not in cmds[0]
        assert all(r in s["permissions"]["allow"] for r in n3m.PERMISSION_RULES)
        assert "次のセッションから保存できます。" in capsys.readouterr().out

    def test_rerun_is_a_noop_and_backup_taken_on_change(self):
        n3m.cmd_init()
        before = self._settings().read_bytes()
        n3m.cmd_init()
        assert self._settings().read_bytes() == before
        assert not list(self._settings().parent.glob("*.bak-n3mc-*"))

    def test_pro_style_hook_is_kept_and_reported(self, capsys):
        sp = self._settings()
        sp.parent.mkdir(parents=True)
        pro = 'python "C:/x/N3MC-Pro_Arnolf/N3MemoryCore/n3mc_hook.py"'
        sp.write_text(json.dumps({"autoUpdatesChannel": "stable", "permissions": {"allow": ["Bash(ls)"]},
                                  "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": pro}]}]}}))
        n3m.cmd_init()
        s = json.loads(sp.read_text())
        cmds = [h["command"] for e in s["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
        assert pro in cmds and len(cmds) == 2 and s["autoUpdatesChannel"] == "stable"
        assert "Bash(ls)" in s["permissions"]["allow"]
        assert "Another N3MC hook is registered for UserPromptSubmit and was left in place" in capsys.readouterr().out
        assert list(sp.parent.glob("settings.json.bak-n3mc-*"))

    def test_own_entries_replaced_not_duplicated(self):
        n3m.cmd_init()
        sp = self._settings()
        s = json.loads(sp.read_text())
        s["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "C:\\Tools\\n3mc-stop-hook.exe"}]})
        s["hooks"]["UserPromptSubmit"].append({"hooks": [{"type": "command",
                                                          "command": "python -m n3memorycore.n3mc_hook"}]})
        sp.write_text(json.dumps(s))
        n3m.cmd_init()
        s = json.loads(sp.read_text())
        for ev in ("Stop", "UserPromptSubmit"):
            assert len([h for e in s["hooks"][ev] for h in e["hooks"]]) == 1

    def test_invalid_settings_untouched_and_exit_1(self):
        sp = self._settings()
        sp.parent.mkdir(parents=True)
        sp.write_text("{ nope")
        assert n3m.cmd_init() == 1 and sp.read_text() == "{ nope"

    def test_entry_point_found_in_scripts_folder_with_empty_path(self, monkeypatch, tmp_path):
        import sysconfig
        sc = tmp_path / "Scripts"
        sc.mkdir()
        (sc / "n3mc-hook.exe").write_text("")
        monkeypatch.setattr(sysconfig, "get_path", lambda name, scheme=None: str(sc))
        monkeypatch.setenv("PATH", "")
        c = n3m._resolve_cmd("n3mc-hook")
        assert c == (sc / "n3mc-hook.exe").as_posix() and not c.startswith('"')

    def test_python_dash_m_fallback_quoting(self, monkeypatch, tmp_path):
        import sysconfig
        monkeypatch.setattr(sysconfig, "get_path", lambda *a, **k: str(tmp_path / "nothing"))
        monkeypatch.setattr(n3m.shutil, "which", lambda n: None)
        monkeypatch.setattr(sys, "executable", "C:/Program Files/Python/python.exe")
        assert n3m._resolve_cmd("n3mc-hook").startswith('"C:/Program Files/Python/python.exe" -m ')
        monkeypatch.setattr(sys, "executable", "C:/Python/python.exe")
        assert n3m._resolve_cmd("n3mc-hook").startswith("C:/Python/python.exe -m ")

    def test_windows_backslash_hook_is_recognised(self):
        assert n3m.is_own_hook(r"C:\Tools\n3mc-hook.exe", "UserPromptSubmit")
        assert n3m.is_own_hook(r"C:\Tools\n3mc-stop-hook.exe", "Stop")
        assert not n3m.is_own_hook('python ".../N3MemoryCore/n3mc_hook.py"', "UserPromptSubmit")


class TestHousekeeping:
    def test_old_turn_id_files_removed(self):
        old, new = os.path.join(n3m.MEMORY_DIR, "turn_id_old.txt"), os.path.join(n3m.MEMORY_DIR, "turn_id_new.txt")
        for f in (old, new):
            open(f, "w").write("x")
        t = time.time() - 31 * 86400
        os.utime(old, (t, t))
        n3m._housekeeping()
        assert not os.path.exists(old) and os.path.exists(new)

    def test_audit_rotates(self, monkeypatch):
        monkeypatch.setattr(n3m, "AUDIT_ROTATE_BYTES", 10)
        open(n3m.AUDIT_LOG, "w").write("x" * 100)
        open(n3m.AUDIT_LOG + ".1", "w").write("older")
        n3m._housekeeping()
        assert not os.path.exists(n3m.AUDIT_LOG) and open(n3m.AUDIT_LOG + ".1").read() == "x" * 100


class TestModelRetry:
    def test_no_retry_for_60_seconds_after_a_failure(self, monkeypatch):
        monkeypatch.setattr(p, "_model", None)
        monkeypatch.setattr(p, "_model_failed_at", None)
        calls = []
        monkeypatch.setattr(p, "_load_model", lambda name: calls.append(name) or (_ for _ in ()).throw(OSError("offline")))
        for _ in range(3):
            with pytest.raises(Exception):
                p.get_model("some/model")
        assert len(calls) == 1

    def test_env_var_has_no_effect(self, monkeypatch):
        monkeypatch.setenv("N3MC_EMBED_MODEL", "other/model")
        assert n3m.get_config()["embed_model"] == "intfloat/multilingual-e5-base"


class TestPackaging:
    def test_psutil_is_a_dependency(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        deps = tomllib.load(open(os.path.join(root, "pyproject.toml"), "rb"))["project"]["dependencies"]
        assert any(d.startswith("psutil") for d in deps)


class TestWeakMatches:
    def test_fills_up_to_min_results_in_filter_score_order(self, conn, cfg):
        add(conn, cfg, "zebra stripes quagga")
        add(conn, cfg, "zebra mammals africa savanna")
        add(conn, cfg, "zebra crossing street sign")
        add(conn, cfg, "unrelated pottery wheel clay")
        res = search(conn, cfg, "zebra stripes quagga")["results"]
        normal = [r for r in res if not r["weak"]]
        weak = [r for r in res if r["weak"]]
        assert len(normal) == 1 and len(res) == 3 and res.index(weak[0]) > res.index(normal[-1])
        assert weak == sorted(weak, key=lambda r: r["filter_score"], reverse=True)

    def test_min_results_zero_adds_nothing(self, conn, cfg):
        add(conn, cfg, "zebra stripes quagga")
        add(conn, cfg, "zebra mammals africa savanna")
        res = search(conn, {**cfg, "min_results": 0}, "zebra stripes quagga")["results"]
        assert all(not r["weak"] for r in res) and len(res) == 1

    def test_limit_smaller_than_min_results(self, conn, cfg):
        for i in range(4):
            add(conn, cfg, f"zebra variant text {i} with words")
        res = search(conn, {**cfg, "search_result_limit": 2}, "zebra variant text 0 with words")["results"]
        assert len(res) <= 2

    def test_same_content_skipped(self, conn, cfg):
        add(conn, cfg, "zebra stripes quagga", turn_id="a", ts=ago(2))
        add(conn, cfg, "zebra stripes quagga", turn_id="b", ts=ago(1))
        res = search(conn, cfg, "zebra stripes quagga")["results"]
        assert len([r for r in res if r["content"] == "zebra stripes quagga"]) == 1

    def test_per_turn_cap_applies_to_weak(self, conn, cfg):
        add(conn, cfg, "zebra stripes quagga", turn_id="T")
        for i in range(3):
            add(conn, cfg, f"zebra mammals africa savanna {i}", turn_id="T")
        res = search(conn, cfg, "zebra stripes quagga")["results"]
        assert len([r for r in res if r["turn_id"] == "T"]) <= 2

    def test_orthogonal_vectors_not_added(self, conn, cfg):
        add(conn, cfg, "pottery wheel clay")
        res = search(conn, cfg, "completely different astronomy words")["results"]
        assert all(r["filter_score"] >= 1e-6 for r in res)

    def test_weak_do_not_feed_pairs(self, conn, cfg):
        add(conn, cfg, "[user] pottery wheel clay", turn_id="W")
        res = search(conn, cfg, "pottery")
        assert "W" not in res["pairs"] or not any(r["weak"] and r["turn_id"] == "W" for r in res["results"])

    def test_empty_query_returns_nothing(self, conn, cfg):
        add(conn, cfg, "something")
        assert search(conn, cfg, "")["results"] == []


class TestPidFileRemoval:
    def test_own_pid_file_is_deleted_and_a_foreign_one_is_kept(self):
        open(n3m.PID_FILE, "w").write(str(os.getpid()))
        n3m._remove_own_pid_file()
        assert not os.path.exists(n3m.PID_FILE)  # (an open handle would make this fail on Windows)
        open(n3m.PID_FILE, "w").write("999999")
        n3m._remove_own_pid_file()
        assert os.path.exists(n3m.PID_FILE)

    def test_exit_server_removes_the_pid_file(self, monkeypatch):
        open(n3m.PID_FILE, "w").write(str(os.getpid()))
        monkeypatch.setattr(n3m._os, "_exit", lambda code: None)
        n3m._exit_server()
        assert not os.path.exists(n3m.PID_FILE)


def _vec_at(cos, axis=1):
    """A unit vector whose cosine with e0 is `cos` (the rest goes on another axis)."""
    v = [0.0] * 768
    v[0] = cos
    v[axis] = (1 - cos * cos) ** 0.5
    return v


def _e0(_text):
    v = [0.0] * 768
    v[0] = 1.0
    return v


def _add_vec(conn, cfg, content, cos, axis=1, turn_id=None):
    rid = n3m._new_id()
    db.insert_memory(conn, rid, content, NOW.isoformat(), cfg["owner_id"], cfg["local_id"], "claude-code",
                     None, turn_id=turn_id, embedding=_vec_at(cos, axis))
    return rid


class TestSearchImprovements2026_11_22:
    """Search improvements of 2026-11-22 (not in the spec). Each one can be turned off in config.json."""

    def test_defaults(self):
        d = n3m.DEFAULT_CONFIG
        assert d["vector_min_cos"] == 0.82 and d["fts_stopwords"] is True and d["kw_only_min_terms"] == 3
        assert d["top_matches_max"] == 5 and d["cross_lang_min_cos"] == 0.80

    def test_stopwords_dropped_from_terms(self):
        assert db.fts_terms("Who is the CEO of the company?", True) == ["CEO", "company"]
        assert "the" in db.fts_terms("Who is the CEO of the company?", False)
        assert db.bigram_terms("What is the CEO doing", True) == ["CEO", "doing"]
        assert db.fts_terms("who is the", True) == ["who", "the"]  # only function words: kept as typed
        assert db.fts_terms("東京タワーの高さ", True) == db.fts_terms("東京タワーの高さ", False)

    def test_function_words_alone_do_not_match(self, conn, cfg):
        add(conn, cfg, "The release is on Friday and the notes are in the wiki.", embed=False)
        c = {**cfg, "min_results": 0}
        assert search(conn, c, "Who is the CEO of the company?")["results"] == []
        off = {**c, "fts_stopwords": False, "kw_only_min_terms": 0}
        assert search(conn, off, "Who is the CEO of the company?")["results"]

    def test_keyword_only_rows_need_enough_terms(self, conn, cfg):
        add(conn, cfg, "zebra crossing near the park", embed=False)
        add(conn, cfg, "zebra crossing station lights timing", embed=False)
        c = {**cfg, "relative_min_score_ratio": 0, "min_score": 0}
        r = search(conn, c, "zebra crossing station lights")["results"]
        strong = [x["content"] for x in r if not x["weak"]]
        assert strong == ["zebra crossing station lights timing"]
        assert any(x["weak"] and x["content"].startswith("zebra crossing near") for x in r)
        off = search(conn, {**c, "kw_only_min_terms": 0}, "zebra crossing station lights")["results"]
        assert len([x for x in off if not x["weak"]]) == 2

    def test_top_matches_max_caps_strong_rows_only(self, conn, cfg):
        for i in range(8):
            _add_vec(conn, cfg, f"record {i}", 0.95, axis=1 + i)
        c = {**cfg, "top_matches_max": 3}
        r = p.hybrid_search(conn, "anything", c, embed_fn=_e0)["results"]
        assert len([x for x in r if not x["weak"]]) == 3
        r = p.hybrid_search(conn, "anything", {**c, "top_matches_max": 0}, embed_fn=_e0)["results"]
        assert len([x for x in r if not x["weak"]]) == 8

    def test_is_cjk_text(self):
        assert p._is_cjk_text("[claude 1/2] ログの保存期間は 3 年です")
        assert not p._is_cjk_text("[user] How long are logs kept?")
        assert not p._is_cjk_text("The file 設定.json was renamed to config.json in the repository")
        assert not p._is_cjk_text("")

    def test_best_other_language_row_is_rescued(self, conn, cfg):
        _add_vec(conn, cfg, "[claude] The backup runs at 03:30 every night.", 0.81, axis=1)
        _add_vec(conn, cfg, "[claude] The staging host moved last week.", 0.80, axis=2)
        _add_vec(conn, cfg, "[claude] ログは 3 年残します。", 0.81, axis=3)
        c = {**cfg, "min_results": 0}
        strong = [x["content"] for x in p.hybrid_search(conn, "バックアップは何時に動く？", c, embed_fn=_e0)["results"]]
        assert strong == ["[claude] The backup runs at 03:30 every night."]  # only the best, only the other script
        assert p.hybrid_search(conn, "バックアップは何時に動く？", {**c, "cross_lang_min_cos": 0},
                               embed_fn=_e0)["results"] == []
        assert p.hybrid_search(conn, "バックアップは何時に動く？", {**c, "cross_lang_min_cos": 0.82},
                               embed_fn=_e0)["results"] == []

    def test_spec_settings_restore_the_spec_behaviour(self, conn, cfg):
        add(conn, cfg, "zebra here", embed=False)
        spec = {**cfg, "vector_min_cos": 0.85, "fts_stopwords": False, "kw_only_min_terms": 0,
                "top_matches_max": 0, "cross_lang_min_cos": 0, "min_results": 0}
        r = search(conn, spec, "zebra crossing station lights")["results"]
        assert [x["content"] for x in r if not x["weak"]] == ["zebra here"]

    def test_new_keys_are_type_checked(self):
        n3m._load_config()
        cfg = json.load(open(n3m.CONFIG_FILE))
        cfg.update({"fts_stopwords": "yes", "kw_only_min_terms": -1, "top_matches_max": 2.5,
                    "cross_lang_min_cos": 1.5})
        open(n3m.CONFIG_FILE, "w").write(json.dumps(cfg))
        out = n3m._load_config()
        assert out["fts_stopwords"] is True and out["kw_only_min_terms"] == 3
        assert out["top_matches_max"] == 5 and out["cross_lang_min_cos"] == 0.80
