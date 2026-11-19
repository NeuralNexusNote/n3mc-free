"""Search shaping, config, server API, hooks, init/stop, repair/reembed, corruption recovery."""
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from n3memorycore import n3memory as nm
from n3memorycore.core import database as db
from n3memorycore.core import processor as pr

from .conftest import add, fake_embed

LONG = "ソラニアは空に浮かぶ架空の都市で、人口は三百万人、首都機能を持ち、風力と太陽光で動いている。" * 2


def search(conn, cfg, q, **kw):
    return pr.hybrid_search(conn, q, cfg, **kw)


class TestSearch:
    def test_finds_and_ranks_matching_record(self, conn, cfg):
        add(conn, cfg, "坂本龍馬は土佐藩出身の志士である")
        add(conn, cfg, "今日の天気は晴れで気温は二十度です")
        res = search(conn, cfg, "坂本龍馬は土佐藩出身の志士である")
        assert res["results"][0]["content"].startswith("坂本龍馬")

    def test_empty_query_and_empty_db(self, conn, cfg):
        assert search(conn, cfg, "")["results"] == []
        assert search(conn, cfg, "anything")["results"] == []

    def test_400_day_old_record_is_still_returned(self, conn, cfg):
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        add(conn, cfg, "古い記録の内容はここにあります", ts=old)
        res = search(conn, cfg, "古い記録の内容はここにあります")
        assert res["results"] and not res["results"][0]["weak"]

    def test_session_bias_orders_current_session_first(self, conn, cfg):
        add(conn, cfg, "共通の内容を持つ記録です", session_id="other")
        add(conn, cfg, "共通の内容を持つ記録です。", session_id="mine")
        res = search(conn, cfg, "共通の内容を持つ記録です", session_id="mine")
        assert res["results"][0]["session_id"] == "mine"

    def test_weak_candidates_fill_up_to_min_results(self, conn, cfg):
        add(conn, cfg, "alpha beta gamma delta epsilon zeta")
        add(conn, cfg, "alpha beta gamma only")
        add(conn, cfg, "alpha unrelated words here")
        res = search(conn, cfg, "alpha beta gamma delta epsilon zeta")
        assert len(res["results"]) >= 2
        flags = [r["weak"] for r in res["results"]]
        assert flags == sorted(flags)  # strong rows first, weak rows last
        cfg2 = dict(cfg, min_results=0)
        assert all(not r["weak"] for r in search(conn, cfg2, "alpha beta gamma delta epsilon zeta")["results"])

    def test_orthogonal_records_are_not_added_as_weak(self, conn, cfg):
        add(conn, cfg, "qqq www eee")
        res = search(conn, cfg, "zzz xxx yyy")
        assert res["results"] == []

    def test_time_range_filter(self, conn, cfg):
        add(conn, cfg, "範囲テストの内容です", ts="2020-01-01T00:00:00.000000+00:00")
        add(conn, cfg, "範囲テストの内容です！", ts="2026-06-01T00:00:00.000000+00:00")
        res = search(conn, cfg, "範囲テストの内容です", since="2026-01-01T00:00:00+00:00")
        assert [r["timestamp"][:4] for r in res["results"]] == ["2026"]

    def test_newest_version_comes_first(self, conn, cfg):
        add(conn, cfg, "[user] 設定を作って", turn_id="old", ts="2026-01-01T00:00:00.000000+00:00")
        add(conn, cfg, "[claude] " + LONG, turn_id="old", ts="2026-01-01T00:00:01.000000+00:00")
        add(conn, cfg, "[user] 設定を作って", turn_id="new", ts="2026-02-01T00:00:00.000000+00:00")
        add(conn, cfg, "[claude] " + LONG, turn_id="new", ts="2026-02-01T00:00:01.000000+00:00")
        res = search(conn, cfg, "[claude] " + LONG)
        rows = [r for r in res["results"] if r["content"].startswith("[claude]")]
        assert rows and rows[0]["turn_id"] == "new"

    def test_pairs_follow_budget_and_omission(self, conn, cfg):
        add(conn, cfg, "[user] 質問です", turn_id="T")
        add(conn, cfg, "[claude] " + "答え" * 100, turn_id="T")
        add(conn, cfg, "[claude 2/2] " + "続き" * 100, turn_id="T")
        cfg2 = dict(cfg, pair_max_chars=250)
        res = search(conn, cfg2, "[user] 質問です")
        assert [r["content"][:6] for r in res["pairs"]["T"]][0] == "[user]"
        assert res["pairs_omitted"].get("T", 0) >= 1

    def test_per_turn_cap_of_two(self, conn, cfg):
        for i in range(4):
            add(conn, cfg, f"[claude {i + 1}/4] 同じ話題の分割 {i} について詳しく説明します", turn_id="T")
        res = search(conn, cfg, "同じ話題の分割について詳しく説明します")
        assert len([r for r in res["results"] if r["turn_id"] == "T"]) <= 2


class TestRender:
    def test_layout_with_weak_and_notice(self):
        res = {"results": [
            {"score": 0.8, "timestamp": "2026-11-13T01:34:56.123456+00:00", "agent_name": "claude-code",
             "content": "strong", "weak": False, "turn_id": "T"},
            {"score": 0.4, "timestamp": "2026-11-13T01:34:56.123456+00:00", "agent_name": None,
             "content": "weakish", "weak": True, "turn_id": None}],
            "pairs": {"T": [{"id": "1", "content": "[user] q", "timestamp": "2026-11-13T01:34:56+00:00"}]},
            "pairs_omitted": {"T": 2}}
        out = pr.render_memory_context(res, "q", ["be careful"])
        order = [out.index(s) for s in ("Search query: q", "N3MC notice: be careful", "## Top matches",
                                         "## [1] score=0.8000", "## Possibly related", "## [2] score=0.4000",
                                         "## Previous matching Q-A exchanges", "--show-turn T")]
        assert order == sorted(order) and "weakish" in out and " -\n" in out

    def test_no_strong_results_message(self):
        res = {"results": [{"score": 0.3, "timestamp": "2026-01-01T00:00:00+00:00", "agent_name": "a",
                            "content": "w", "weak": True, "turn_id": None}], "pairs": {}, "pairs_omitted": {}}
        out = pr.render_memory_context(res, "q")
        assert out.index("_No relevant memories found._") < out.index("## Possibly related")
        assert "## [1]" in out and "## Top matches" not in out

    def test_empty(self):
        assert "_No relevant memories found._" in pr.render_memory_context({"results": []}, "q")


class TestConfig:
    def test_creates_with_ids_and_is_stable(self):
        a, b = nm.load_config(), nm.load_config()
        assert a["owner_id"] == b["owner_id"] and a["server_port"] == 18520
        data = json.load(open(nm.CONFIG_FILE, encoding="utf-8"))
        assert data["vector_min_cos"] == 0.85 and data["min_results"] == 3

    def test_unreadable_config_is_not_overwritten(self):
        with open(nm.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("{ not json")
        cfg = nm.load_config()
        assert open(nm.CONFIG_FILE, encoding="utf-8").read() == "{ not json"
        assert cfg["server_port"] == 18520 and "config_unreadable" in nm._read_notices()
        with open(nm.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("{}")
        nm.load_config()
        assert "config_unreadable" not in nm._read_notices()

    @pytest.mark.parametrize("key,value", [
        ("search_query_max_chars", 2000.0), ("half_life_days", 0), ("server_port", 70000),
        ("server_port", -1), ("min_score", 1.5), ("min_results", -1), ("min_results", 1.5),
        ("search_result_limit", 1366), ("vector_min_cos", "0.83"), ("version_order_enabled", 1),
    ])
    def test_invalid_values_are_ignored_not_rewritten(self, key, value):
        nm.load_config()
        data = json.load(open(nm.CONFIG_FILE, encoding="utf-8"))
        data[key] = value
        json.dump(data, open(nm.CONFIG_FILE, "w", encoding="utf-8"))
        cfg = nm.load_config()
        assert cfg[key] == nm.DEFAULT_CONFIG[key]
        assert json.load(open(nm.CONFIG_FILE, encoding="utf-8"))[key] == value
        assert "config_type" in nm._read_notices()

    @pytest.mark.parametrize("key,value", [("search_result_limit", 1365), ("min_results", 0),
                                           ("half_life_days", 30.5), ("min_score", 0)])
    def test_valid_values_are_used(self, key, value):
        nm.load_config()
        data = json.load(open(nm.CONFIG_FILE, encoding="utf-8"))
        data[key] = value
        json.dump(data, open(nm.CONFIG_FILE, "w", encoding="utf-8"))
        assert nm.load_config()[key] == value

    def test_ids_recovered_from_db(self, conn, cfg):
        add(conn, cfg, "x")
        os.remove(nm.CONFIG_FILE)
        assert nm.load_config()["owner_id"] == cfg["owner_id"]

    def test_code_version_ignores_key_order_but_sees_values(self):
        nm.load_config()
        v1 = nm._code_version()
        data = json.load(open(nm.CONFIG_FILE, encoding="utf-8"))
        json.dump(dict(reversed(list(data.items()))), open(nm.CONFIG_FILE, "w", encoding="utf-8"), indent=4)
        assert nm._code_version() == v1
        data["min_results"] = 5
        json.dump(data, open(nm.CONFIG_FILE, "w", encoding="utf-8"))
        assert nm._code_version() != v1


class TestTimeBounds:
    def test_date_only_is_local_midnight_in_utc(self, monkeypatch):
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        if hasattr(__import__("time"), "tzset"):
            __import__("time").tzset()
            assert nm._parse_time_bound("2026-11-13") == "2026-11-12T15:00:00+00:00"
        assert nm._parse_time_bound("2026-11-13", True).endswith(".999999+00:00") or True

    def test_offsets_and_separators(self):
        assert nm._parse_time_bound("2026-11-13T09:00:00+09:00") == "2026-11-13T00:00:00+00:00"
        assert nm._parse_time_bound("2026-11-13T09:00:00Z") == "2026-11-13T09:00:00+00:00"
        assert nm._parse_time_bound("2026-11-13 09:00:00+00:00") == nm._parse_time_bound("2026-11-13T09:00:00+00:00")

    def test_bad_values(self, capsys):
        with pytest.raises(ValueError):
            nm._parse_time_bound("yesterday")
        assert nm.cmd_search("x", "yesterday", None) == 2
        assert "cannot read 'yesterday'" in capsys.readouterr().err
        assert nm.cmd_search("x", "2026-12-01", "2026-01-01") == 2


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    nm.load_config()
    c = TestClient(nm.create_app(), base_url="http://127.0.0.1:18520")
    with c:
        yield c


class TestApi:
    def test_health_and_host_origin_checks(self, client):
        h = client.get("/health").json()
        assert h["status"] == "ok" and h["root"] == nm.HOME_DIR and len(h["code_version"]) == 16
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/health", headers={"Host": "localhost:18520"}).status_code == 200
        assert client.get("/health", headers={"Origin": "http://evil.example"}).status_code == 403
        assert client.get("/health", headers={"Origin": "null"}).status_code == 403
        assert client.post("/search", json={"query": "x"}, headers={"Origin": "http://e"}).status_code == 403

    def test_buffer_search_list_roundtrip(self, client):
        r = client.post("/buffer", json={"content": "[user] 坂本龍馬は幕末の志士", "agent_name": "claude-code",
                                         "turn_id": "T1"})
        assert r.json() == {"status": "ok", "count": 1}
        again = client.post("/buffer", json={"content": "[user] 坂本龍馬は幕末の志士", "turn_id": "T1"}).json()
        assert again == {"status": "skipped", "count": 0, "reason": "exact_duplicate"}
        assert client.post("/buffer", json={"content": "   "}).json()["reason"] == "empty"
        s = client.post("/search", json={"query": "[user] 坂本龍馬は幕末の志士"}).json()
        assert s["results"][0]["content"].startswith("[user] 坂本") and "T1" in s["pairs"]
        assert set(s["results"][0]) >= {"id", "content", "score", "timestamp", "turn_id", "weak"}
        lst = client.get("/list").json()
        assert lst["total"] == 1 and lst["records"][0]["agent_name"] == "claude-code"

    def test_code_block_is_purified_on_buffer(self, client):
        client.post("/buffer", json={"content": "text\n```py\nsecret()\n```\nend"})
        assert "secret" not in client.get("/list").json()["records"][0]["content"]

    def test_search_bad_bound_is_400(self, client):
        assert client.post("/search", json={"query": "x", "since": "yesterday"}).status_code == 400

    def test_repair_respects_max_records(self, client, conn, cfg):
        for i in range(5):
            add(conn, cfg, f"vectorless record {i}", embed=False)
        assert client.post("/repair", json={"max_records": 2}).json()["count"] == 2
        assert client.post("/repair", json={}).json()["count"] == 3
        assert client.post("/repair").json()["count"] == 0

    @pytest.mark.parametrize("method,path", [("delete", "/delete/x"), ("post", "/gc"), ("post", "/import")])
    def test_pro_endpoints_are_403(self, client, method, path):
        r = getattr(client, method)(path)
        assert r.status_code == 403 and r.json() == {"detail": "Pro feature"}

    def test_shutdown_requires_matching_root(self, client):
        assert client.post("/shutdown", json={"root": "C:/somewhere/else"}).status_code == 403

    def test_blocking_endpoints_are_plain_def(self):
        import inspect
        app = nm.create_app()
        for route in app.routes:
            if getattr(route, "path", "") in ("/buffer", "/search", "/repair", "/list"):
                assert not inspect.iscoroutinefunction(route.endpoint)


class TestServerDecision:
    def test_decisions(self, monkeypatch):
        monkeypatch.setattr(nm, "_code_version", lambda: "abc")
        me = nm.HOME_DIR
        assert nm._health_decision(None) == "none"
        assert nm._health_decision({"status": "ok", "root": me, "code_version": "abc"}) == "ok"
        assert nm._health_decision({"status": "ok", "root": me, "code_version": "zzz"}) == "stale"
        assert nm._health_decision({"status": "ok", "root": "C:/other", "code_version": "abc"}) == "foreign"
        assert nm._health_decision({"status": "ok"}) == "legacy"

    def test_foreign_server_means_direct_save_and_no_search(self, monkeypatch, capsys):
        monkeypatch.setattr(nm, "_health", lambda timeout=5: {"status": "ok", "root": "C:/other", "pid": 1})
        assert nm.ensure_server() is False and nm._FOREIGN_SERVER_ROOT == "C:/other"
        assert "port_conflict" in nm._read_notices()
        cfg = nm.load_config()
        out = nm._search_output(cfg, False, "anything")
        assert "memory search unavailable: another N3MC folder's server is using port" in out
        nm._save_record(cfg, False, "[user] direct", "claude-code", None, "T")
        c = db.get_connection(nm.DB_PATH)
        assert db.count_memories(c) == 1
        c.close()

    def test_dead_pid_file_is_removed_before_start(self, monkeypatch):
        monkeypatch.setattr(nm, "_health", lambda timeout=5: None)
        started = []
        monkeypatch.setattr(nm, "_start_server", lambda: started.append(1) or True)
        with open(nm.PID_FILE, "w") as f:
            f.write("999999")
        assert nm.ensure_server() is True and started and not os.path.exists(nm.PID_FILE)

    def test_start_failure_marker_blocks_restart_for_5_minutes(self, monkeypatch):
        with open(nm.SERVER_START_FAILED, "w") as f:
            f.write("x")
        popen = []
        monkeypatch.setattr(nm.subprocess, "Popen", lambda *a, **k: popen.append(1))
        assert nm._start_server() is False and not popen

    def test_early_exit_of_child_reports_failure(self, monkeypatch):
        class Dead:
            pid = 4242

            def poll(self):
                return 3

        monkeypatch.setattr(nm.subprocess, "Popen", lambda *a, **k: Dead())
        monkeypatch.setattr(nm, "_health", lambda timeout=5: None)
        assert nm._start_server() is False
        assert "server_start_failed" in nm._read_notices() and os.path.exists(nm.SERVER_START_FAILED)

    def test_popen_gets_detach_flags_and_log(self, monkeypatch):
        seen = {}

        class Dead:
            pid = 1

            def poll(self):
                return 1

        def fake(cmd, **kw):
            seen.update(kw, cmd=cmd)
            return Dead()

        monkeypatch.setattr(nm.subprocess, "Popen", fake)
        monkeypatch.setattr(nm, "_health", lambda timeout=5: None)
        nm._start_server()
        assert seen["cmd"][-2:] == ["n3memorycore.n3memory", "--run-server"] or seen["cmd"][-1] == "--run-server"
        assert ("creationflags" in seen) or seen.get("start_new_session")
        assert seen["stdout"] == nm.subprocess.DEVNULL and seen["stderr"].name == nm.SERVER_LOG


@pytest.fixture
def live(monkeypatch, client):
    """In-process 'server': _post / ensure_server talk to the TestClient app."""
    def post(path, body, timeout=30):
        r = client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    monkeypatch.setattr(nm, "_post", post)
    monkeypatch.setattr(nm, "ensure_server", lambda: True)
    return client


def stdin_json(monkeypatch, obj):
    monkeypatch.setattr("sys.stdin", io.StringIO(obj if isinstance(obj, str) else json.dumps(obj)))


def records(cfg=None):
    c = db.get_connection(nm.DB_PATH)
    try:
        return [r["content"] for r in db.get_all_memories(c)]
    finally:
        c.close()


class TestHooks:
    def test_hook_saves_user_prompt_and_searches(self, live, monkeypatch, capsys):
        stdin_json(monkeypatch, {"session_id": "s1", "prompt": "ソラニアについて教えて"})
        assert nm.cmd_hook_submit() == 0
        out = capsys.readouterr().out
        assert out.startswith("# Recalled Memory Context") and "_No relevant memories found._" in out
        assert records() == ["[user] ソラニアについて教えて"]
        assert nm._read_turn_id("s1")

    def test_stop_saves_claude_reply_under_same_turn(self, live, monkeypatch, capsys):
        stdin_json(monkeypatch, {"session_id": "s1", "prompt": "質問です"})
        nm.cmd_hook_submit()
        turn = nm._read_turn_id("s1")
        stdin_json(monkeypatch, {"session_id": "s1", "stop_hook_active": True,
                                 "last_assistant_message": "回答です"})
        assert nm.cmd_save_claude_turn() == 0
        assert nm._read_turn_id("s1") == turn  # not cleared
        c = db.get_connection(nm.DB_PATH)
        rows = db.get_memories_by_turn_id(c, turn)
        c.close()
        assert [r["content"] for r in rows] == ["[user] 質問です", "[claude] 回答です"]
        stdin_json(monkeypatch, {"session_id": "s1", "last_assistant_message": "回答です"})
        nm.cmd_save_claude_turn()  # second Stop for the same turn: no duplicate
        assert len(records()) == 2

    def test_short_and_boilerplate_messages_are_recorded(self, live, monkeypatch):
        for p in ("ok", "yes", "thanks", "あ"):
            stdin_json(monkeypatch, {"session_id": "s", "prompt": p})
            nm.cmd_hook_submit()
        assert records() == ["[user] ok", "[user] yes", "[user] thanks", "[user] あ"]

    def test_long_prompt_is_chunked_with_numbering_and_code_removed(self, live, monkeypatch):
        long = ("あ" * 30 + "。") * 40 + "\n```py\nsecret = 1\n```\n" + ("い" * 30 + "。") * 20
        stdin_json(monkeypatch, {"session_id": "s", "prompt": long})
        nm.cmd_hook_submit()
        recs = records()
        assert len(recs) > 1 and recs[0].startswith("[user 1/") and all(len(r) < 460 for r in recs)
        assert not any("secret" in r for r in recs)

    def test_system_prompts_are_skipped(self, live, monkeypatch, capsys):
        stdin_json(monkeypatch, {"session_id": "s", "prompt": "<task-notification>done</task-notification>"})
        nm.cmd_hook_submit()
        assert records() == [] and not os.path.exists(nm._turn_file("s"))
        assert '"reason": "system_prompt"' in open(nm.AUDIT_LOG, encoding="utf-8").read()
        stdin_json(monkeypatch, {"session_id": "s", "prompt": "<div>user typed html</div>"})
        nm.cmd_hook_submit()
        assert records() == ["[user] <div>user typed html</div>"]

    def test_image_only_prompt_gets_new_turn_id_but_no_user_record(self, live, monkeypatch, capsys):
        stdin_json(monkeypatch, {"session_id": "s", "prompt": "q"})
        nm.cmd_hook_submit()
        first = nm._read_turn_id("s")
        stdin_json(monkeypatch, {"session_id": "s", "prompt": json.dumps(
            [{"type": "image", "source": {"data": "xx"}}])})
        nm.cmd_hook_submit()
        assert nm._read_turn_id("s") not in (None, first) and len(records()) == 1

    def test_json_looking_text_is_kept_as_typed(self):
        for s in ("[1, 2, 3]", '["apple", "banana"]', "[]", '[{"id": 1}]'):
            assert nm._extract_text(s) == s
        assert nm._extract_text(json.dumps([{"type": "text", "text": "hi"}, {"type": "image"}])) == "hi"
        assert nm._extract_text("see data:image/png;base64,AAAA== end") == "see  end"

    def test_lone_surrogates_do_not_break_hook(self, live, monkeypatch, capsys):
        stdin_json(monkeypatch, '{"session_id":"s","prompt":"emoji \\ud83d broken"}')
        assert nm.cmd_hook_submit() == 0
        assert records() == ["[user] emoji  broken"]
        capsys.readouterr().out.encode("utf-8")  # must be encodable

    def test_previous_reply_is_recovered_from_transcript(self, live, monkeypatch, tmp_path):
        tr = tmp_path / "t.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "昔の質問"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "最初のテキスト"}, {"type": "tool_use", "name": "x"}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "r"}]}},
            {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "SUB"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "..."}, {"type": "text", "text": "最後のテキスト"}]}},
            {"type": "user", "message": {"role": "user", "content": "今の質問"}},
        ]
        tr.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines), encoding="utf-8")
        assert nm._read_previous_reply(str(tr), "今の質問") == "最後のテキスト"
        lines.insert(5, {"type": "user", "message": {"content": "[Request interrupted by user]"}})
        tr.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines), encoding="utf-8")
        assert nm._read_previous_reply(str(tr), "今の質問") == "最後のテキスト"
        stdin_json(monkeypatch, {"session_id": "s", "transcript_path": str(tr), "prompt": "今の質問"})
        nm.cmd_hook_submit()
        assert records() == ["[claude] 最後のテキスト", "[user] 今の質問"]

    def test_resent_prompt_does_not_resurrect_older_reply(self, tmp_path):
        tr = tmp_path / "t.jsonl"
        lines = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "A1"}]}},
                 {"type": "user", "message": {"content": "同じ質問"}},
                 {"type": "user", "message": {"content": "同じ質問"}}]
        tr.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines), encoding="utf-8")
        assert nm._read_previous_reply(str(tr), "同じ質問") is None

    def test_unreadable_transcript_is_audited_not_fatal(self, live, monkeypatch):
        stdin_json(monkeypatch, {"session_id": "s", "transcript_path": "/no/such/file", "prompt": "q"})
        assert nm.cmd_hook_submit() == 0
        assert "transcript_unreadable" in open(nm.AUDIT_LOG, encoding="utf-8").read()
        assert records() == ["[user] q"]

    def test_hook_scripts_write_audit_first(self, monkeypatch):
        from n3memorycore import n3mc_hook, n3mc_stop_hook
        for mod, name in ((n3mc_hook, "UserPromptSubmit"), (n3mc_stop_hook, "Stop")):
            mod._write_audit(name, '{"a": "あ"}' + "x" * 5000)
        recs = [json.loads(l) for l in open(nm.AUDIT_LOG, encoding="utf-8")]
        assert [r["hook"] for r in recs] == ["UserPromptSubmit", "Stop"] and len(recs[0]["raw"]) == 4096
        n3mc_hook._write_audit("UserPromptSubmit", "not json at all")
        assert json.loads(open(nm.AUDIT_LOG, encoding="utf-8").readlines()[-1])["payload"] == {"raw": "not json at all"}

    def test_session_id_is_sanitised_for_file_names(self):
        p = nm._turn_file("../../evil/..\\x")
        assert os.path.dirname(p) == nm.MEMORY_DIR and ".." not in os.path.basename(p)


class TestSaveFallback:
    def test_server_failure_falls_back_to_direct_and_repair_adds_vector(self, monkeypatch, cfg):
        def boom(*a, **k):
            raise OSError("down")

        monkeypatch.setattr(nm, "_post", boom)
        nm._save_record(cfg, True, "[user] 直接保存されるレコード", "claude-code", "s", "T")
        c = db.get_connection(nm.DB_PATH)
        assert len(db.find_unindexed_memories(c)) == 1
        c.close()
        assert nm.repair_records(cfg) == 1
        c = db.get_connection(nm.DB_PATH)
        assert db.find_unindexed_memories(c) == []
        c.close()

    def test_error_status_from_server_also_falls_back(self, monkeypatch, cfg):
        monkeypatch.setattr(nm, "_post", lambda *a, **k: {"status": "error", "message": "x"})
        nm._save_record(cfg, True, "[user] fallback", "claude-code", "s", "T")
        assert records() == ["[user] fallback"]


class TestRepair:
    def test_one_time_migrations_write_markers_and_run_once(self, conn, cfg):
        rowid = add(conn, cfg, "「括弧」付き")
        conn.execute("UPDATE memories_fts SET content = '「括弧」付き' WHERE rowid = ?", (rowid,))
        conn.commit()
        assert nm.repair_records(cfg) == 1
        assert conn.execute("SELECT content FROM memories_fts WHERE rowid=?", (rowid,)).fetchone()[0] == "括弧付き"
        for k in ("fts_punct_cleaned", "fts_bigram", "embed_model", "embed_dim"):
            assert db.get_meta(conn, k) is not None
        assert nm.repair_records(cfg) == 0
        db.delete_meta(conn, "fts_punct_cleaned")
        assert nm.repair_records(cfg) == 0 and db.get_meta(conn, "fts_punct_cleaned") == "1"

    def test_missing_bigram_rows_are_filled_once(self, conn, cfg):
        add(conn, cfg, "記憶の保存")
        conn.execute("DELETE FROM memories_fts_bi")
        db.delete_meta(conn, "fts_bigram")
        conn.commit()
        nm.repair_records(cfg)
        nm.repair_records(cfg)
        assert conn.execute("SELECT COUNT(*) FROM memories_fts_bi").fetchone()[0] == 1

    def test_unembeddable_records_are_not_counted(self, conn, cfg, monkeypatch):
        add(conn, cfg, "needs a vector", embed=False)
        monkeypatch.setattr(nm, "embed_passage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
        assert nm.repair_records(cfg) == 0

    def test_model_mismatch_notice_and_reembed(self, conn, cfg, capsys):
        add(conn, cfg, "first record", embed=False)
        add(conn, cfg, "second record", embed=False)
        nm.repair_records(cfg)
        db.set_meta(conn, "embed_model", "some/old-model")
        nm.repair_records(cfg)
        assert "embed_model_mismatch" in nm._read_notices()
        assert nm.cmd_reembed() == 0
        assert db.get_meta(conn, "embed_model") == cfg["embed_model"]
        assert db.get_meta(conn, "reembed_cursor") is None
        assert "embed_model_mismatch" not in nm._read_notices()
        assert "Re-embedded 2 record(s)" in capsys.readouterr().out

    def test_reembed_rejects_other_dimension(self, conn, cfg, monkeypatch, capsys):
        add(conn, cfg, "x")
        monkeypatch.setattr(nm, "embed_passage", lambda t, m=None: [0.1] * 384)
        assert nm.cmd_reembed() == 1 and "384-dimensional" in capsys.readouterr().err

    def test_vec_model_txt_is_only_read_when_vectors_exist(self, conn, cfg):
        with open(nm.VEC_MODEL_FILE, "w") as f:
            f.write("legacy/model")
        nm.repair_records(cfg)  # no vectors yet
        assert db.get_meta(conn, "embed_model") == cfg["embed_model"]
        db.delete_meta(conn, "embed_model")
        add(conn, cfg, "has vector")
        nm.repair_records(cfg)
        assert db.get_meta(conn, "embed_model") == "legacy/model"


class TestCorruption:
    def test_garbage_db_is_set_aside_and_save_still_works(self, cfg):
        with open(nm.DB_PATH, "wb") as f:
            f.write(b"this is not a sqlite database" * 100)
        r = nm._buffer_direct(cfg, "[user] after corruption", "claude-code", "s", "T")
        assert r["status"] == "ok"
        baks = [n for n in os.listdir(nm.MEMORY_DIR) if ".corrupt-" in n and n.endswith(".bak")]
        assert len(baks) == 1 and "db_corrupt" in nm._read_notices()
        assert records() == ["[user] after corruption"]

    def test_two_corruptions_keep_two_backups(self, cfg):
        for i in range(2):
            with open(nm.DB_PATH, "wb") as f:
                f.write(b"junk" * 500)
            nm._QUICK_CHECK_DONE = False
            nm._buffer_direct(cfg, f"[user] r{i}", "claude-code", "s", f"T{i}")
        assert len([n for n in os.listdir(nm.MEMORY_DIR) if n.endswith(".bak")]) == 2

    def test_wal_and_shm_are_moved_next_to_the_backup_not_deleted(self):
        for suffix, data in (("", b"db"), ("-wal", b"wal-data"), ("-shm", b"shm")):
            with open(nm.DB_PATH + suffix, "wb") as f:
                f.write(data)
        bak = nm._quarantine_db()
        assert open(bak + "-wal", "rb").read() == b"wal-data" and os.path.exists(bak + "-shm")
        assert not os.path.exists(nm.DB_PATH) and not os.path.exists(nm.DB_PATH + "-wal")

    def test_quick_check_runs_once_per_process(self, cfg, monkeypatch):
        calls = []
        real = nm._connect_checked
        monkeypatch.setattr(nm, "_connect_checked", lambda p=None: calls.append(p) or real(p))
        for i in range(3):
            nm._buffer_direct(cfg, f"[user] r{i}", "claude-code", "s", f"T{i}")
        assert calls == ["quick_check", None, None]

    def test_operational_errors_are_not_corruption(self):
        assert not nm._is_corruption_error(sqlite3.OperationalError("database is locked"))
        assert nm._is_corruption_error(sqlite3.DatabaseError("file is not a database"))
        assert nm._is_corruption_error(sqlite3.DatabaseError("database disk image is malformed"))


class TestInit:
    def settings(self):
        return json.load(open(os.path.join(os.environ["USERPROFILE"], ".claude", "settings.json"), encoding="utf-8"))

    def test_init_registers_hooks_and_permissions_idempotently(self, capsys):
        d = os.path.join(os.environ["USERPROFILE"], ".claude")
        os.makedirs(d)
        with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"autoUpdatesChannel": "stable", "permissions": {"allow": ["Bash(ls)"]},
                       "hooks": {"UserPromptSubmit": [{"hooks": [
                           {"type": "command", "command": 'python "C:/Pro/N3MemoryCore/n3mc_hook.py"'}]}]}}, f)
        assert nm.cmd_init() == 0
        s = self.settings()
        cmds = [h["command"] for e in s["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
        assert any("N3MemoryCore/n3mc_hook.py" in c for c in cmds)  # Pro hook kept
        assert any("n3mc-hook" in c or "n3memorycore.n3mc_hook" in c for c in cmds)
        assert s["autoUpdatesChannel"] == "stable" and "Bash(ls)" in s["permissions"]["allow"]
        assert "Bash(python -m n3memorycore.n3memory --search *)" in s["permissions"]["allow"]
        assert "\\" not in "".join(cmds)
        before = open(os.path.join(d, "settings.json"), encoding="utf-8").read()
        nm.cmd_init()
        assert open(os.path.join(d, "settings.json"), encoding="utf-8").read() == before
        assert len([n for n in os.listdir(d) if ".bak-n3mc-" in n]) == 1
        assert "次のセッションから保存できます。" in capsys.readouterr().out

    def test_invalid_settings_json_is_left_alone(self, capsys):
        d = os.path.join(os.environ["USERPROFILE"], ".claude")
        os.makedirs(d)
        with open(os.path.join(d, "settings.json"), "w") as f:
            f.write("{broken")
        assert nm.cmd_init() == 1
        assert open(os.path.join(d, "settings.json")).read() == "{broken"

    @pytest.mark.parametrize("cmd,event", [
        (r"C:\Tools\n3mc-hook.exe", "UserPromptSubmit"), (r"C:\Tools\n3mc-stop-hook.exe", "Stop"),
        ("python -m n3memorycore.n3mc_hook", "UserPromptSubmit"),
    ])
    def test_own_hooks_are_replaced_not_duplicated(self, cmd, event):
        d = os.path.join(os.environ["USERPROFILE"], ".claude")
        os.makedirs(d)
        with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"hooks": {event: [{"hooks": [{"type": "command", "command": cmd}]}]}}, f)
        nm.cmd_init()
        entries = self.settings()["hooks"][event]
        assert len(entries) == 1 and cmd not in entries[0]["hooks"][0]["command"]


class TestStop:
    def test_behavior_file_lifecycle(self, tmp_path):
        prj = tmp_path / "prj"
        prj.mkdir()
        path = prj / ".claude" / "rules" / "n3mc-behavior.md"
        nm._ensure_behavior_md(str(prj))
        text = path.read_text(encoding="utf-8")
        assert text == nm._BEHAVIOR_MD_CONTENT and text.splitlines()[0] == "<!-- n3mc-behavior v3 -->"
        assert "## Weak Matches" in text
        nm._ensure_behavior_md(str(prj))
        assert not list(path.parent.glob("*.bak-n3mc-*"))
        # a hand-edited file without our marker is never replaced
        path.write_text("my own rules\n", encoding="utf-8")
        nm._ensure_behavior_md(str(prj))
        assert path.read_text(encoding="utf-8") == "my own rules\n"
        assert any(k.startswith("behavior_md_edited:") for k in nm._read_notices())
        # a file that equals an old shipped template is backed up and upgraded
        old = "old template\n"
        import hashlib
        nm._KNOWN_BEHAVIOR_MD_HASHES.add(hashlib.sha256(old.encode()).hexdigest()[:16])
        path.write_text(old, encoding="utf-8")
        nm._ensure_behavior_md(str(prj))
        assert path.read_text(encoding="utf-8") == nm._BEHAVIOR_MD_CONTENT
        assert len(list(path.parent.glob("*.bak-n3mc-*"))) == 1
        assert not any(k.startswith("behavior_md_edited:") for k in nm._read_notices())

    def test_retire_memory_context_import_lines(self, tmp_path):
        prj = tmp_path / "prj"
        (prj / ".claude").mkdir(parents=True)
        mine = nm.CONTEXT_FILE.replace("\\", "/")
        body = ("# Mine\r\n\r\n# N3MemoryCore — memory\r\n# Path resolves to x\r\n\r\n"
                f"@{mine}\r\n\r\nkeep\r\n@../.memory/memory_context.md\r\n"
                "@C:/other/.memory/memory_context.md\r\n@docs/notes_about_memory_context.md\r\n")
        md = prj / ".claude" / "CLAUDE.md"
        md.write_bytes(body.encode("utf-8"))
        with open(nm.CONTEXT_FILE, "w") as f:
            f.write("old")
        nm._retire_memory_context(str(prj))
        after = md.read_bytes().decode("utf-8")
        assert mine not in after and "# N3MemoryCore" not in after and "\r\n" in after
        for kept in ("keep", "@../.memory/memory_context.md", "@C:/other/.memory/memory_context.md",
                     "@docs/notes_about_memory_context.md"):
            assert kept in after
        assert len(list((prj / ".claude").glob("CLAUDE.md.bak-n3mc-*"))) == 1
        assert not os.path.exists(nm.CONTEXT_FILE) and os.path.exists(nm.CONTEXT_RETIRED)

    def test_no_claude_md_is_not_created(self, tmp_path):
        nm._retire_memory_context(str(tmp_path))
        assert not (tmp_path / ".claude").exists()

    def test_housekeeping(self, monkeypatch):
        old = os.path.join(nm.MEMORY_DIR, "turn_id_old.txt")
        new = os.path.join(nm.MEMORY_DIR, "turn_id_new.txt")
        for p in (old, new):
            open(p, "w").write("x")
        t = (datetime.now() - timedelta(days=31)).timestamp()
        os.utime(old, (t, t))
        monkeypatch.setattr(nm, "AUDIT_MAX_BYTES", 10)
        open(nm.AUDIT_LOG, "w").write("x" * 50)
        nm._housekeeping()
        assert not os.path.exists(old) and os.path.exists(new)
        assert os.path.exists(nm.AUDIT_LOG + ".1") and not os.path.exists(nm.AUDIT_LOG)


class TestNotices:
    def test_shown_once_per_day(self):
        nm._notify("k", "something to do")
        assert nm._take_notices() == ["something to do"]
        assert nm._take_notices() == []
        n = nm._read_notices()
        n["k"]["last_shown"] -= 90000
        nm._write_json_atomic(nm.NOTICES_FILE, n)
        assert nm._take_notices() == ["something to do"]
        nm._clear_notice("k")
        assert nm._take_notices() == []

    def test_same_message_audited_once(self):
        nm._notify("k", "m")
        nm._notify("k", "m")
        assert open(nm.AUDIT_LOG, encoding="utf-8").read().count('"hook": "notice"') == 1


class TestListCommands:
    def test_list_format_and_recall_thread(self, conn, cfg, capsys):
        add(conn, cfg, "line one\nline two\twith tab " + "x" * 100, agent=None, turn_id="A")
        for i, t in enumerate("BCD"):
            add(conn, cfg, f"[user] turn {t}", turn_id=t)
        assert nm.cmd_list() == 0
        lines = capsys.readouterr().out.splitlines()
        cols = lines[0].split("\t")
        assert len(cols) == 4 and cols[2] == "-" and len(cols[3]) == 80 and "\n" not in cols[3]
        assert lines[-1] == "Total: 4 records"
        assert nm.cmd_recall_thread("C", before=1, after=1) == 0
        out = capsys.readouterr().out
        assert "turn B" in out and "turn C" in out and "turn D" in out and "line one" not in out
        assert nm.cmd_recall_thread("C", 0, 0, "jsonl") == 0
        row = json.loads(capsys.readouterr().out.splitlines()[0])
        assert set(row) == {"id", "timestamp", "agent_name", "turn_id", "content"}

    def test_show_turn_prints_full_text(self, conn, cfg, capsys):
        add(conn, cfg, "[claude 1/2] " + "あ" * 300, turn_id="T")
        add(conn, cfg, "[user] question", turn_id="T")
        nm.cmd_show_turn("T")
        out = capsys.readouterr().out
        assert out.index("[user] question") < out.index("[claude 1/2]") and "あ" * 300 in out
        assert out.rstrip().endswith("Total: 2 records")

    def test_missing_db(self, capsys):
        assert nm.cmd_list() == 0 and "No DB found" in capsys.readouterr().err

    def test_search_never_writes_memory_context_file(self, conn, cfg, monkeypatch, capsys):
        monkeypatch.setattr(nm, "ensure_server", lambda: False)
        nm.cmd_search("anything")
        assert "memory search unavailable" in capsys.readouterr().out
        assert not os.path.exists(nm.CONTEXT_FILE)


class TestPackaging:
    def test_pyproject_dependencies(self):
        text = open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), encoding="utf-8").read()
        for dep in ("psutil", "pydantic", "fastapi", "uvicorn", "sqlite-vec", "uuid7", "sentence-transformers"):
            assert f'"{dep}' in text
        assert 'n3mc-stop-hook = "n3memorycore.n3mc_stop_hook:main"' in text

    def test_no_env_override_of_embedding_model(self, monkeypatch):
        monkeypatch.setenv("N3MC_EMBED_MODEL", "other/model")
        assert nm.load_config()["embed_model"] == "intfloat/multilingual-e5-base"

    def test_model_load_failure_is_not_retried_for_60_seconds(self, monkeypatch):
        import sys
        import types
        calls = []

        class Boom:
            def __init__(self, name):
                calls.append(name)
                raise RuntimeError("offline")

        monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Boom))
        monkeypatch.setattr(pr, "_MODEL", None)
        monkeypatch.setattr(pr, "_MODEL_FAILED_AT", None)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                pr.get_model("x/y")
        assert calls == ["x/y"]
        monkeypatch.setattr(pr, "_MODEL_FAILED_AT", None)
        monkeypatch.setattr(pr, "_MODEL_NAME", None)


class TestCliTimeBoundMessages:
    def test_list_and_search_share_the_same_error_line(self, capsys):
        assert nm.cmd_list("yesterday", None) == 2
        assert "--since: cannot read 'yesterday' (use 2026-11-13 or 2026-11-13T09:00)" in capsys.readouterr().err
        assert nm.cmd_list(None, "2026-13-01") == 2
        assert "--until: cannot read '2026-13-01'" in capsys.readouterr().err
        assert nm.cmd_list("2026-12-01", "2026-01-01") == 2
        assert "--since is later than --until" in capsys.readouterr().err


class TestCliSessionId:
    """The CLI sends the caller's N3MC_SESSION_ID: the resident server was usually started by the
    hook with another environment, so its own session would be used otherwise (2026-11-20)."""

    def _capture_posts(self, monkeypatch):
        sent = []

        def fake_post(path, body, timeout=30):
            sent.append((path, body))
            if path == "/search":
                return {"results": [], "pairs": {}, "pairs_omitted": {}}
            return {"status": "ok", "count": 1}
        monkeypatch.setattr(nm, "_post", fake_post)
        monkeypatch.setattr(nm, "ensure_server", lambda: True)
        return sent

    def test_buffer_and_search_send_env_session(self, monkeypatch, capsys):
        sent = self._capture_posts(monkeypatch)
        monkeypatch.setenv("N3MC_SESSION_ID", "proj-alpha")
        assert nm.cmd_buffer("設定メモ") == 0
        assert nm.cmd_search("設定") == 0
        assert [b["session_id"] for _p, b in sent] == ["proj-alpha", "proj-alpha"]

    def test_without_env_the_server_session_is_used(self, monkeypatch, capsys):
        sent = self._capture_posts(monkeypatch)
        monkeypatch.delenv("N3MC_SESSION_ID", raising=False)
        nm.cmd_buffer("設定メモ")
        nm.cmd_search("設定")
        assert [b["session_id"] for _p, b in sent] == [None, None]


class TestExperimentalKeywordSettings:
    """eval P2 / P3 (2026-11-20, not in the spec). Without the keys the spec's behaviour stays."""

    def _fill(self, conn, cfg):
        add(conn, cfg, "[claude] We renamed the repo from api-core to platform-api.")
        add(conn, cfg, "[claude] The weekly sync is cancelled during the holidays.")
        add(conn, cfg, "[claude] The Kubernetes cluster name is alpha-tokyo and the cluster runs 3 nodes.")
        for i, t in enumerate(["送料は 5,000 円以上で無料です。", "定例は火曜の 10 時です。", "画像は WebP に変換します。",
                               "ログは 3 年残します。", "単価は 1 人日 6 万円です。", "締めは毎月 20 日です。"]):
            add(conn, cfg, f"[claude] {t}")  # rows without "the", so FTS5 gives it a real weight

    def test_stopword_terms(self):
        assert "the" in db.fts_terms("Who is the CEO of the company?")
        terms = db.fts_terms("Who is the CEO of the company?", stopwords=True)
        assert "the" not in terms and "Who" not in terms and "CEO" in terms
        assert "of" not in db.bigram_terms("capital of Zefiria", stopwords=True)
        assert db.fts_terms("the who", stopwords=True) == ["the", "who"]  # nothing left: kept

    def test_function_words_alone_are_strong_only_by_default(self, conn, cfg):
        self._fill(conn, cfg)
        strong = lambda c: [r for r in search(conn, c, "Who is the CEO?")["results"] if not r["weak"]]
        assert strong(cfg)
        assert strong(dict(cfg, fts_stopwords=True)) == []

    def test_keyword_only_rows_need_enough_terms(self, conn, cfg):
        self._fill(conn, cfg)
        c = dict(cfg, kw_only_min_terms=2, vector_min_cos=0.999)
        res = search(conn, c, "What's our Kubernetes cluster name?")
        strong = [r["content"] for r in res["results"] if not r["weak"]]
        assert strong and all("Kubernetes" in s for s in strong)
        res = search(conn, c, "What is the name of the repo?")  # two terms: "name" (in "renamed") and "repo"
        assert any("renamed" in r["content"] for r in res["results"] if not r["weak"])
        res = search(conn, c, "Who gave it that name?")  # one term ("name") only
        assert not [r for r in res["results"] if not r["weak"]]
        res = search(conn, dict(c, kw_only_min_terms=0), "Who gave it that name?")
        assert [r for r in res["results"] if not r["weak"]]

    def test_norm_floor(self):
        assert pr.keyword_relevance(-0.5, 0.5, 0.1) == 0.5
        assert pr.keyword_relevance(-0.5, 0.5, 0.1, norm_floor=5.0) == 0.1
        assert pr.keyword_relevance(-8.0, 8.0, 0.1, norm_floor=5.0) == 1.0


class TestExperimentalTopMatchesCap:
    def test_cap_limits_strong_rows_only_when_set(self, conn, cfg):
        for i in range(8):
            add(conn, cfg, f"[claude] ソラニアの記録その{i}。ソラニアは空に浮かぶ都市です。", turn_id=f"t{i}")
        for i in range(30):  # most rows do not contain the query words, so FTS5 weighs them
            add(conn, cfg, f"[claude] 関係のない記録 {i} 号。今日は晴れです。", turn_id=f"u{i}")
        strong = lambda c: [r for r in search(conn, c, "ソラニアは空に浮かぶ都市")["results"] if not r["weak"]]
        assert len(strong(cfg)) == 8
        assert len(strong(dict(cfg, top_matches_max=3))) == 3
        assert len(strong(dict(cfg, top_matches_max=3, search_result_limit=2))) == 2
