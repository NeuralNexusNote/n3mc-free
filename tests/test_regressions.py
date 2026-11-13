"""Layer 5: regression tests for the v1.4.0 / v1.5.0 fixes (spec §7).

No embedding model: vectors are deterministic fakes. Real data and real
servers are never touched (conftest._isolate_real_data).
"""
import io
import json
import math
import os
import random
import sqlite3
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from n3memorycore import n3memory
from n3memorycore.core import database as db
from n3memorycore.core import processor as proc

DIM = 768


# --------------------------------------------------------------------------- helpers
def fake_embed(text: str) -> list:
    rnd = random.Random(zlib.crc32(text.encode('utf-8')))
    v = [rnd.gauss(0, 1) for _ in range(DIM)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def unit(i: int) -> list:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def at_cos(c: float, axis: int = 1) -> list:
    """Unit vector whose cosine with unit(0) is c."""
    v = [0.0] * DIM
    v[0] = c
    v[axis] = math.sqrt(max(0.0, 1.0 - c * c))
    return v


def mix(a: list, b: list, wa: float, wb: float) -> list:
    v = [wa * x + wb * y for x, y in zip(a, b)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def new_db(tmp_path, name='t.db') -> str:
    path = str(tmp_path / name)
    conn = db.get_connection(path)
    db.init_db(conn)
    conn.close()
    return path


def add(path, content, emb=None, ts=None, turn_id=None, session_id=None):
    conn = db.get_connection(path)
    try:
        rid = str(uuid.uuid4())
        db.insert_memory(
            conn, rid, content,
            ts or datetime.now(timezone.utc).isoformat(),
            owner_id='o', embedding=emb, turn_id=turn_id, session_id=session_id,
        )
        return rid
    finally:
        conn.close()


def fts_builder():
    return getattr(db, '_build_fts_query', None) or getattr(db, 'build_fts_query')


BASE_CFG = {
    'half_life_days': 90, 'bm25_min_threshold': 0.1, 'search_result_limit': 20,
    'min_score': 0.0, 'search_query_max_chars': 2000, 'vector_min_cos': 0.85,
    'relative_min_score_ratio': 0.6, 'pair_top_n': 3, 'pair_max_chars': 3000,
    'version_order_enabled': True, 'version_sim_threshold': 0.98,
    'version_min_chars': 100,
}


def cfg(**kw):
    c = dict(BASE_CFG)
    c.update(kw)
    return c


@pytest.fixture
def query_vec(monkeypatch):
    """embed_query always returns unit(0)."""
    monkeypatch.setattr(proc, 'embed_query', lambda text: unit(0))
    return unit(0)


# --------------------------------------------------------------------------- FTS
class TestFtsQuoting:
    QUERIES = [
        r'C:\Users\test #tag = @a * $b % 100',
        'AND OR NOT NEAR',
        'say "hello" to him',
        'tab\there\x00nul\x1fend\x7f',
        '/ \\ # = @ * $ % + ^ ~ |',
    ]

    @pytest.mark.parametrize('q', QUERIES)
    def test_built_query_is_valid_fts5(self, tmp_path, q):
        path = new_db(tmp_path)
        add(path, 'some content')
        built = fts_builder()(q)
        conn = db.get_connection(path)
        try:
            if built:
                conn.execute(
                    "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", (built,)
                ).fetchall()  # must not raise "fts5: syntax error"
        finally:
            conn.close()

    def test_symbol_term_hits(self, tmp_path):
        path = new_db(tmp_path)
        add(path, 'set retries=5 in the file')
        conn = db.get_connection(path)
        try:
            rows = db.search_fts(conn, 'retries=5')
        finally:
            conn.close()
        assert len(rows) == 1

    def test_operator_words_match_literally(self, tmp_path):
        path = new_db(tmp_path)
        add(path, 'cats AND dogs')
        conn = db.get_connection(path)
        try:
            rows = db.search_fts(conn, 'AND dogs')
        finally:
            conn.close()
        assert len(rows) == 1

    def test_short_query_still_skipped(self):
        assert fts_builder()('ab') == ''

    def test_hybrid_continues_when_fts_raises(self, tmp_path, query_vec, monkeypatch):
        path = new_db(tmp_path)
        add(path, 'vector only record', emb=unit(0))

        def boom(*a, **k):
            raise sqlite3.OperationalError('fts5: syntax error near "\\"')
        monkeypatch.setattr(proc, 'search_fts', boom)
        out = proc.hybrid_search(path, 'anything', cfg())
        assert [r['content'] for r in out['results']] == ['vector only record']


# --------------------------------------------------------------------------- vector filter
class TestVectorFilter:
    def test_in_range_records_found_beyond_global_top_k(self, tmp_path):
        path = new_db(tmp_path)
        for i in range(30):   # newer and closer to the query
            add(path, f'new {i}', emb=at_cos(0.999), ts='2026-06-01T00:00:00+00:00')
        for i in range(3):    # older, a little farther
            add(path, f'old {i}', emb=at_cos(0.95, axis=2), ts='2025-01-01T00:00:00+00:00')
        conn = db.get_connection(path)
        try:
            rows = db.search_vector_with_filter(conn, unit(0), k=3,
                                                until='2025-12-31T23:59:59')
        finally:
            conn.close()
        assert sorted(r['content'] for r in rows) == ['old 0', 'old 1', 'old 2']

    def test_exact_fallback_matches_knn(self, tmp_path, monkeypatch):
        path = new_db(tmp_path)
        for i, c in enumerate([0.99, 0.9, 0.95, 0.5]):
            add(path, f'r{i}', emb=at_cos(c), ts='2025-01-01T00:00:00+00:00')
        conn = db.get_connection(path)
        try:
            knn = db.search_vector_with_filter(conn, unit(0), k=3, since='2024-01-01')
        finally:
            conn.close()

        class FailingConn:
            """Wraps a connection; the rowid-IN KNN raises OperationalError."""
            def __init__(self, c):
                self._c = c

            def execute(self, sql, *a):
                if 'MATCH' in sql and 'rowid IN' in sql and 'vec_distance_l2' not in sql:
                    raise sqlite3.OperationalError('rowid IN not supported')
                return self._c.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._c, name)

        conn = db.get_connection(path)
        try:
            exact = db.search_vector_with_filter(FailingConn(conn), unit(0), k=3,
                                                 since='2024-01-01')
        finally:
            conn.close()
        assert [r['content'] for r in knn] == ['r0', 'r2', 'r1']
        assert [r['content'] for r in exact] == ['r0', 'r2', 'r1']


# --------------------------------------------------------------------------- dedup
@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    path = new_db(tmp_path, 'api.db')
    monkeypatch.setattr(n3memory, 'DB_PATH', path)
    monkeypatch.setattr(n3memory, '_srv_cfg', {'owner_id': 'o', 'local_id': 'l'})
    monkeypatch.setattr(proc, 'embed_passage', lambda text: unit(0))  # all identical
    return TestClient(n3memory.app), path


def count(path):
    conn = db.get_connection(path)
    try:
        return db.count_memories(conn)
    finally:
        conn.close()


class TestNoSimilarityDedup:
    def test_similar_chunks_all_saved(self, api):
        client, path = api
        tid = str(uuid.uuid4())
        for i in range(6):
            r = client.post('/buffer', json={'content': f'[claude {i+1}/6] item {i:02d}: same shape',
                                             'turn_id': tid})
            assert r.json()['status'] == 'ok'
        assert count(path) == 6

    def test_same_turn_exact_skipped(self, api):
        client, path = api
        tid = str(uuid.uuid4())
        client.post('/buffer', json={'content': '[claude] answer', 'turn_id': tid})
        r = client.post('/buffer', json={'content': '[claude] answer', 'turn_id': tid})
        assert r.json()['reason'] == 'exact_duplicate'
        assert count(path) == 1

    def test_other_turn_exact_saved(self, api):
        client, path = api
        client.post('/buffer', json={'content': '[claude 3/12] unchanged item', 'turn_id': 'a'})
        r = client.post('/buffer', json={'content': '[claude 3/12] unchanged item', 'turn_id': 'b'})
        assert r.json()['status'] == 'ok'
        assert count(path) == 2

    def test_no_turn_exact_skipped_globally(self, api):
        client, path = api
        client.post('/buffer', json={'content': 'manual note'})
        r = client.post('/buffer', json={'content': 'manual note'})
        assert r.json()['status'] == 'skipped'
        assert count(path) == 1

    def test_direct_write_uses_same_rule(self, tmp_path, monkeypatch):
        path = new_db(tmp_path, 'direct.db')
        monkeypatch.setattr(n3memory, 'DB_PATH', path)
        c = {'owner_id': 'o', 'local_id': 'l'}
        n3memory._buffer_direct('[claude] x', c, turn_id='a')
        n3memory._buffer_direct('[claude] x', c, turn_id='a')
        n3memory._buffer_direct('[claude] x', c, turn_id='b')
        assert count(path) == 2


# --------------------------------------------------------------------------- search shaping
LONG = 'x' * 120


class TestSearchShaping:
    def test_vector_min_cos_drops_candidates(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        add(path, 'close one', emb=at_cos(0.90))
        add(path, 'far one', emb=at_cos(0.84, axis=2))
        out = proc.hybrid_search(path, 'zzz', cfg(relative_min_score_ratio=0.0))
        assert [r['content'] for r in out['results']] == ['close one']

    def test_relative_cut(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        add(path, 'top', emb=at_cos(0.99))
        add(path, 'weak', emb=at_cos(0.40, axis=2))
        out = proc.hybrid_search(path, 'zzz', cfg(vector_min_cos=0.0))
        assert [r['content'] for r in out['results']] == ['top']

    def _two_versions(self, path, sim_new):
        old_t, new_t = 'turn-old', 'turn-new'
        rep_old = unit(0)
        rep_new = mix(unit(0), unit(3), sim_new, math.sqrt(1 - sim_new ** 2))
        add(path, '[user] make the setting', emb=unit(5),
            ts='2026-01-01T00:00:00+00:00', turn_id=old_t)
        add(path, '[claude 1/2] setting v1 ' + LONG, emb=rep_old,
            ts='2026-01-01T00:00:01+00:00', turn_id=old_t)
        add(path, '[user] make the setting again', emb=unit(6),
            ts='2026-01-02T00:00:00+00:00', turn_id=new_t)
        add(path, '[claude 1/2] setting v2 ' + LONG, emb=rep_new,
            ts='2026-01-02T00:00:01+00:00', turn_id=new_t)
        return old_t, new_t

    def test_newest_version_first(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        old_t, new_t = self._two_versions(path, 0.99)
        out = proc.hybrid_search(path, 'zzz', cfg(half_life_days=1e9))
        res = out['results']
        assert res[0]['turn_id'] == new_t
        assert res[1]['turn_id'] == old_t
        assert res[0]['score'] >= res[1]['score']
        assert res[0].get('_version_group') and res[1].get('_version_group')
        assert list(out['pairs'])[0] == new_t

    def test_different_content_not_reordered(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        old_t, new_t = self._two_versions(path, 0.90)
        out = proc.hybrid_search(path, 'zzz', cfg(half_life_days=1e9))
        assert out['results'][0]['turn_id'] == old_t
        assert not out['results'][0].get('_version_group')

    def test_version_rows_survive_relative_cut(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        old_t, new_t = self._two_versions(path, 0.99)
        out = proc.hybrid_search(path, 'zzz',
                                 cfg(half_life_days=1e9, relative_min_score_ratio=0.9999))
        assert {r['turn_id'] for r in out['results']} == {old_t, new_t}

    def test_identical_content_collapsed_to_newest(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        add(path, 'same text', emb=at_cos(0.99), ts='2026-01-01T00:00:00+00:00', turn_id='a')
        newest = add(path, 'same text', emb=at_cos(0.99), ts='2026-02-01T00:00:00+00:00',
                     turn_id='b')
        out = proc.hybrid_search(path, 'zzz', cfg(half_life_days=1e9))
        assert len(out['results']) == 1
        assert out['results'][0]['id'] == newest

    def test_pair_limits_and_omitted(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        for t in range(4):
            tid = f't{t}'
            add(path, f'[user] question {t}', emb=at_cos(0.99 - t * 0.001), turn_id=tid,
                ts=f'2026-01-0{t+1}T00:00:00+00:00')
            for i in range(5):
                add(path, f'[claude {i+1}/5] {t}-{i} ' + 'y' * 380, emb=unit(7 + t * 10 + i),
                    turn_id=tid, ts=f'2026-01-0{t+1}T00:00:0{i+1}+00:00')
        out = proc.hybrid_search(path, 'zzz', cfg(half_life_days=1e9, pair_top_n=2,
                                                  pair_max_chars=1500))
        pairs, omitted = out['pairs'], out['pairs_omitted']
        assert len(pairs) <= 2
        total = sum(len(s['content']) for sib in pairs.values() for s in sib)
        assert total <= 1500
        first = list(pairs)[0]
        assert omitted.get(first) == 6 - len(pairs[first])


# --------------------------------------------------------------------------- render
class TestRender:
    def test_saved_note_omitted_and_notice(self):
        ts = '2026-01-01T00:00:00+00:00'
        local = datetime.fromisoformat(ts).astimezone()
        md = proc.render_memory_context(
            {'results': [{'id': '1', 'content': 'c', 'score': 0.9, 'timestamp': ts,
                          'agent_name': 'claude-code', 'turn_id': 't'}],
             'pairs': {'t': [{'id': '1', 'content': '[user] q', 'timestamp': ts}]},
             'pairs_omitted': {'t': 2}},
            'q', notices=['port conflict'])
        assert f"saved={local:%Y-%m-%d %H:%M} (UTC{local:%z})" in md
        assert 'newest `saved`' in md
        assert '--show-turn t' in md and '2 more chunk' in md
        assert md.index('N3MC notice: port conflict') < md.index('## Top matches')


# --------------------------------------------------------------------------- memory_context retirement
def stop_in(tmp_path, monkeypatch):
    claude_dir = tmp_path / 'proj' / '.claude'
    (claude_dir / 'rules').mkdir(parents=True)
    paths = {
        'CLAUDE_DIR':  str(claude_dir),
        'CLAUDE_MD':   str(claude_dir / 'CLAUDE.md'),
        'RULES_DIR':   str(claude_dir / 'rules'),
        'BEHAVIOR_MD': str(claude_dir / 'rules' / 'n3mc-behavior.md'),
    }
    monkeypatch.setattr(n3memory, 'claude_paths', lambda cwd=None: paths)
    return claude_dir


class TestNoContextFile:
    def test_search_writes_stdout_only(self, monkeypatch, capsys):
        monkeypatch.setattr(n3memory, 'ensure_server', lambda c: True)
        monkeypatch.setattr(n3memory, '_post',
                            lambda *a, **k: {'results': [], 'pairs': {}, 'pairs_omitted': {}})
        n3memory.cmd_search('hello', {'server_port': 1})
        assert '# Recalled Memory Context' in capsys.readouterr().out
        assert not os.path.exists(n3memory.CONTEXT_FILE)

    def test_stop_removes_import_keeps_crlf(self, tmp_path, monkeypatch):
        claude_dir = stop_in(tmp_path, monkeypatch)
        # v1.5.0: only this installation's own CONTEXT_FILE (a path with a space)
        own = tmp_path / 'my user' / '.n3mc' / '.memory' / 'memory_context.md'
        monkeypatch.setattr(n3memory, 'CONTEXT_FILE', str(own))
        md = claude_dir / 'CLAUDE.md'
        original = ('# CLAUDE.md\r\n\r\nMy rules\r\n\r\n'
                    '# N3MemoryCore — recalled context for the current turn\r\n'
                    '@' + own.as_posix() + '\r\n')
        md.write_bytes(original.encode('utf-8'))
        n3memory.cmd_stop({})
        data = md.read_bytes().decode('utf-8')
        assert 'memory_context' not in data
        assert 'My rules' in data
        assert '\r\n' in data and '\n' not in data.replace('\r\n', '')
        backups = [p for p in os.listdir(claude_dir) if p.startswith('CLAUDE.md.bak-n3mc-')]
        assert len(backups) == 1

    def test_stop_leaves_clean_file_alone(self, tmp_path, monkeypatch):
        claude_dir = stop_in(tmp_path, monkeypatch)
        md = claude_dir / 'CLAUDE.md'
        md.write_text('# Mine\n', encoding='utf-8')
        n3memory.cmd_stop({})
        assert md.read_text(encoding='utf-8') == '# Mine\n'
        assert not [p for p in os.listdir(claude_dir) if '.bak-n3mc-' in p]

    def test_stop_does_not_create_claude_md(self, tmp_path, monkeypatch):
        claude_dir = stop_in(tmp_path, monkeypatch)
        n3memory.cmd_stop({})
        assert not (claude_dir / 'CLAUDE.md').exists()

    def test_leftover_file_deleted_once(self, tmp_path, monkeypatch):
        stop_in(tmp_path, monkeypatch)
        with open(n3memory.CONTEXT_FILE, 'w', encoding='utf-8') as f:
            f.write('old')
        n3memory.cmd_stop({})
        assert not os.path.exists(n3memory.CONTEXT_FILE)
        assert os.path.exists(n3memory.CONTEXT_RETIRED_MARKER)


# --------------------------------------------------------------------------- behavior md
OLD_TEMPLATE_V1 = """# N3MemoryCore Behavioral Guidelines

These guidelines apply to every Claude Code session in this project. They are
loaded automatically from `.claude/rules/` at session start.

## Fully Automatic Saving
All conversations are saved by hooks (UserPromptSubmit + Stop). Do NOT call
`--buffer` manually. Every non-empty user message and Claude response is
recorded character-for-character; there is no length filter and no skip-pattern
filter. Make NO acknowledgement when a save succeeds — silence is correct.

## Active RAG
When prior context would help, run a search proactively. **Always invoke via
`python -m n3memorycore.n3memory --search "<keywords>"`**, NOT the bare `n3mc`
console script. Claude Code spawns a bash subprocess whose `PATH` does not
reliably include the Python `Scripts/` directory where the `n3mc` entry-point
lives, so `n3mc --search ...` works in interactive shells but fails in
production with `command not found`. `python -m` resolves through the Python
interpreter (which is on `PATH`) and is portable across venvs and platforms.
Both invocation forms are auto-allowed via `permissions.allow`.

## Recall Acknowledgment
When `--search` results actually shape your reply (you are recalling something
saved earlier), open the reply with a short acknowledgment in the user's
language — e.g. "Pulling this from earlier memory in this session." or
「前回の回答がメモリに保存されています。」 If no relevant memory was found,
or it did not influence your answer, do NOT announce anything.

## Fatal-Failure Warning
If a save fails physically, surface the warning prominently:
> ⚠️ Physical save failed. Current memories may be lost.
"""


def notices():
    try:
        with open(n3memory.NOTICES_FILE, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


class TestBehaviorMd:
    def test_old_template_hash_is_known(self):
        assert n3memory._behavior_md_hash(OLD_TEMPLATE_V1) in n3memory._KNOWN_BEHAVIOR_MD_HASHES

    def test_known_old_template_replaced_with_backup(self, tmp_path, monkeypatch):
        claude_dir = stop_in(tmp_path, monkeypatch)
        rule = claude_dir / 'rules' / 'n3mc-behavior.md'
        rule.write_bytes(OLD_TEMPLATE_V1.replace('\n', '\r\n').encode('utf-8'))
        n3memory.cmd_stop({})
        text = rule.read_text(encoding='utf-8')
        assert text.splitlines()[0].strip() == n3memory._BEHAVIOR_MD_MARKER
        assert 'Cut Exchanges' in text and '--show-turn' in text
        assert [p for p in os.listdir(rule.parent) if '.bak-n3mc-' in p]

    def test_current_marker_untouched(self, tmp_path, monkeypatch):
        claude_dir = stop_in(tmp_path, monkeypatch)
        rule = claude_dir / 'rules' / 'n3mc-behavior.md'
        rule.write_text(n3memory._BEHAVIOR_MD_MARKER + '\n# customised\n', encoding='utf-8')
        n3memory.cmd_stop({})
        assert '# customised' in rule.read_text(encoding='utf-8')

    def test_hand_edited_not_replaced_and_notified(self, tmp_path, monkeypatch):
        claude_dir = stop_in(tmp_path, monkeypatch)
        rule = claude_dir / 'rules' / 'n3mc-behavior.md'
        rule.write_text(OLD_TEMPLATE_V1 + '\n## My own section\n', encoding='utf-8')
        n3memory.cmd_stop({})
        assert '## My own section' in rule.read_text(encoding='utf-8')
        keys = [k for k in notices() if k.startswith('behavior_md_edited')]
        assert keys == [n3memory._behavior_notice_key(str(rule))]


# --------------------------------------------------------------------------- config
def write_cfg(text):
    with open(n3memory.CONFIG_FILE, 'w', encoding='utf-8') as f:
        f.write(text)


def read_cfg():
    with open(n3memory.CONFIG_FILE, encoding='utf-8') as f:
        return f.read()


IDS = '"owner_id": "11111111-1111-4111-8111-111111111111", "local_id": "22222222-2222-4222-8222-222222222222"'


class TestConfig:
    def test_unreadable_not_overwritten(self):
        write_cfg('{"server_port": 18599,,}')
        c = n3memory._load_config()
        assert read_cfg() == '{"server_port": 18599,,}'
        assert c['server_port'] == 18520
        assert 'config_unreadable' in notices()

    def test_non_object_not_overwritten(self):
        write_cfg('[1, 2]')
        n3memory._load_config()
        assert read_cfg() == '[1, 2]'
        assert 'config_unreadable' in notices()

    def test_wrong_type_item_ignored_others_used(self):
        text = '{' + IDS + ', "server_port": 18599, "vector_min_cos": "0.83"}'
        write_cfg(text)
        c = n3memory._load_config()
        assert c['server_port'] == 18599
        assert c['vector_min_cos'] == 0.85
        assert read_cfg() == text
        assert 'vector_min_cos' in notices()['config_type']['message']

    def test_bool_is_not_a_number(self):
        assert not n3memory._config_value_ok('pair_top_n', True)
        assert n3memory._config_value_ok('vector_min_cos', 1)
        assert not n3memory._config_value_ok('version_order_enabled', 1)

    def test_fixed_config_clears_notice(self):
        write_cfg('{oops')
        n3memory._load_config()
        write_cfg('{' + IDS + '}')
        n3memory._load_config()
        assert 'config_unreadable' not in notices()

    def test_code_version_tracks_config_content(self):
        write_cfg('{' + IDS + ', "vector_min_cos": 0.85}')
        v1 = n3memory._code_version()
        write_cfg('{\n  "vector_min_cos": 0.85,\n  ' + IDS.replace(', ', ',\n  ') + '\n}')
        assert n3memory._code_version() == v1
        write_cfg('{' + IDS + ', "vector_min_cos": 0.83}')
        assert n3memory._code_version() != v1


# --------------------------------------------------------------------------- hooks
class TestHookSubmit:
    def test_task_notification_skipped(self, monkeypatch, capsys):
        def no_server(c):
            raise AssertionError('ensure_server must not be called')
        monkeypatch.setattr(n3memory, 'ensure_server', no_server)
        n3memory._write_turn_id('keep-me', 'sess')
        payload = {'prompt': '<task-notification>done</task-notification>', 'session_id': 'sess'}
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
        n3memory.cmd_hook_submit({'server_port': 1, 'search_query_max_chars': 2000})
        assert n3memory._read_turn_id('sess') == 'keep-me'
        assert '# Recalled Memory Context' in capsys.readouterr().out
        with open(n3memory.AUDIT_LOG, encoding='utf-8') as f:
            lines = [json.loads(l) for l in f]
        assert any(l.get('stage') == 'skipped' for l in lines)
        assert not os.path.exists(n3memory.DB_PATH)

    def test_stop_keeps_turn_id(self, monkeypatch):
        saved = []
        monkeypatch.setattr(n3memory, 'ensure_server', lambda c: True)
        monkeypatch.setattr(n3memory, '_do_buffer',
                            lambda text, c, **k: saved.append((text, k.get('turn_id'))))
        n3memory._write_turn_id('t-1', 'sess')
        payload = {'last_assistant_message': 'answer', 'session_id': 'sess'}
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
        n3memory.cmd_save_claude_turn({'server_port': 1})
        assert saved == [('[claude] answer', 't-1')]
        assert n3memory._read_turn_id('sess') == 't-1'


# --------------------------------------------------------------------------- server decision
def me():
    return os.path.abspath(n3memory.HOME_DIR)


class TestServerDecision:
    def test_decisions(self):
        cv = n3memory._code_version()
        assert n3memory._health_decision(None) == 'none'
        assert n3memory._health_decision({'status': 'ok'}) == 'legacy'
        assert n3memory._health_decision({'status': 'ok', 'root': me(), 'code_version': cv}) == 'ok'
        assert n3memory._health_decision({'status': 'ok', 'root': me(), 'code_version': 'old'}) == 'stale'
        assert n3memory._health_decision({'status': 'ok', 'root': '/elsewhere/n3mc',
                                          'code_version': cv}) == 'foreign'

    def test_stale_uses_shutdown_first(self, monkeypatch):
        calls = []
        monkeypatch.setattr(n3memory, '_request_shutdown', lambda c: calls.append('shutdown') or True)
        monkeypatch.setattr(n3memory, '_wait_port_free', lambda c, *a, **k: True)

        def no_kill(pid):
            raise AssertionError('psutil path must not be used when /shutdown works')
        monkeypatch.setattr(n3memory, '_is_own_server_process', no_kill)
        assert n3memory._stop_stale_server({'server_port': 1}, {'pid': 4242}) is True
        assert calls == ['shutdown']

    def test_unverifiable_old_server_not_stopped(self, monkeypatch):
        monkeypatch.setattr(n3memory, '_is_own_server_process', lambda pid: False)
        assert n3memory._stop_stale_server({'server_port': 1}, {'pid': 4242}) is False
        assert 'stale_server' in notices()

    def test_legacy_unverifiable_is_treated_as_foreign(self, monkeypatch):
        monkeypatch.setattr(n3memory, '_get_health', lambda c: {'status': 'ok'})
        monkeypatch.setattr(n3memory, '_is_own_server_process', lambda pid: False)
        monkeypatch.setattr(n3memory, '_listener_pid', lambda port: 4242)

        def no_start(c):
            raise AssertionError('must not start a server on an occupied port')
        monkeypatch.setattr(n3memory, '_start_server', no_start)
        assert n3memory.ensure_server({'server_port': 1}) is False
        assert n3memory._FOREIGN_SERVER_ROOT

    def test_dead_pid_file_does_not_wait(self, monkeypatch):
        monkeypatch.setattr(n3memory, '_get_health', lambda c: None)
        with open(n3memory.PID_FILE, 'w') as f:
            f.write('999999')
        monkeypatch.setattr(n3memory, '_wait_for_server',
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError('waited')))
        started = []
        monkeypatch.setattr(n3memory, '_start_server', lambda c: started.append(1) or True)
        assert n3memory.ensure_server({'server_port': 1}) is True
        assert started == [1]
        assert not os.path.exists(n3memory.PID_FILE)

    def test_shutdown_endpoint_checks_root(self, monkeypatch):
        from fastapi.testclient import TestClient
        exits = []
        monkeypatch.setattr(n3memory, '_exit_server_process', lambda: exits.append(1))
        client = TestClient(n3memory.app)
        assert client.post('/shutdown', json={'root': '/elsewhere/n3mc'}).status_code == 403
        assert client.post('/shutdown', json={'root': me()}).json()['status'] == 'ok'

    def test_health_reports_identity(self):
        from fastapi.testclient import TestClient
        h = TestClient(n3memory.app).get('/health').json()
        assert h['status'] == 'ok' and h['pid'] == os.getpid()
        assert os.path.normcase(h['root']) == os.path.normcase(me())
        assert len(h['code_version']) == 16


class TestForeignServer:
    @pytest.fixture
    def foreign(self, tmp_path, monkeypatch):
        path = new_db(tmp_path, 'own.db')
        monkeypatch.setattr(n3memory, 'DB_PATH', path)
        monkeypatch.setattr(n3memory, '_get_health',
                            lambda c: {'status': 'ok', 'root': '/elsewhere/n3mc',
                                       'code_version': 'x', 'pid': 1})
        sent = []
        monkeypatch.setattr(n3memory, '_post', lambda c, p, *a, **k: sent.append(p) or {})
        return path, sent

    def test_save_goes_to_own_db(self, foreign):
        path, sent = foreign
        n3memory.cmd_buffer('note for this folder', {'server_port': 1, 'owner_id': 'o'})
        assert sent == []
        assert count(path) == 1

    def test_search_skipped(self, foreign, capsys):
        path, sent = foreign
        n3memory.cmd_search('anything', {'server_port': 1})
        assert sent == []
        assert 'memory search unavailable' in capsys.readouterr().out
        assert 'port_conflict' in notices()

    def test_repair_skipped(self, foreign):
        path, sent = foreign
        n3memory.cmd_repair({'server_port': 1})
        assert sent == []


# =========================================================================== v1.5.0
# References captured at import time (before conftest replaces them per test).
REAL_SPAWN_SERVER = n3memory._spawn_server
REAL_CHECK_HOST = n3memory._check_host
REAL_SETTINGS_PATH = n3memory._settings_path
REAL_POST = n3memory._post
REAL_GET = n3memory._get


def audit_lines():
    try:
        with open(n3memory.AUDIT_LOG, encoding='utf-8') as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- FTS (v1.5.0)
class TestFtsQuoting(TestFtsQuoting):  # v1.5.0 additions
    def test_terms_joined_with_or(self, tmp_path):
        path = new_db(tmp_path)
        add(path, 'the quick brown fox jumps')
        assert ' OR ' in fts_builder()('what quick fox doing')
        conn = db.get_connection(path)
        try:
            rows = db.search_fts(conn, 'what is the quick fox doing in my DB')
        finally:
            conn.close()
        assert len(rows) == 1

    def test_short_terms_dropped(self):
        assert fts_builder()('AI 東京') == ''
        assert fts_builder()('AI 東京 tokyo') == '"tokyo"'

    def test_max_30_terms(self):
        q = ' '.join(f'term{i:02d}' for i in range(50))
        assert fts_builder()(q).count(' OR ') == 29

    def test_punctuation_splits_terms(self, tmp_path):
        path = new_db(tmp_path)
        add(path, '架空の惑星「アルファ9」の気温設定')
        built = fts_builder()('「アルファ9」の気温は？')
        assert built == '"アルファ9" OR "の気温は"'
        conn = db.get_connection(path)
        try:
            rows = db.search_fts(conn, '「アルファ9」の気温は？')
        finally:
            conn.close()
        assert len(rows) == 1


# --------------------------------------------------------------------------- filter_score (v1.5.0)
class TestSearchShaping(TestSearchShaping):  # v1.5.0 additions
    def test_old_other_session_survives_relative_cut(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        add(path, 'current session record', emb=at_cos(0.86), session_id='cur')
        add(path, 'old record from another session', emb=at_cos(0.99, axis=2),
            ts=old, session_id='other')
        out = proc.hybrid_search(path, 'zzz', cfg(min_score=0.2), current_session_id='cur')
        contents = [r['content'] for r in out['results']]
        # the order is still by the final score: the current session first
        assert contents == ['current session record', 'old record from another session']
        assert out['results'][0]['score'] > out['results'][1]['score']

    def test_keyword_only_other_session_survives_min_score(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        for i in range(40):
            add(path, f'filler record number {i}')
        add(path, 'a note about the zebrafish tank', session_id='other')
        out = proc.hybrid_search(path, 'zebrafish', cfg(min_score=0.2),
                                 current_session_id='cur')
        res = out['results']
        assert [r['content'] for r in res] == ['a note about the zebrafish tank']
        assert res[0]['score'] < 0.2 <= res[0]['filter_score']

    def test_filter_score_moves_with_version_position(self, tmp_path, query_vec):
        path = new_db(tmp_path)
        rep = unit(0)
        add(path, '[user] make it', emb=unit(5), ts='2026-01-01T00:00:00+00:00', turn_id='old')
        add(path, '[claude 1/2] v1 ' + LONG, emb=rep, ts='2026-01-01T00:00:01+00:00',
            turn_id='old')
        add(path, '[user] make it again', emb=unit(6), ts='2026-01-02T00:00:00+00:00',
            turn_id='new')
        add(path, '[claude 1/2] v2 ' + LONG, emb=mix(rep, unit(3), 0.99, math.sqrt(1 - 0.99 ** 2)),
            ts='2026-01-02T00:00:01+00:00', turn_id='new')
        out = proc.hybrid_search(path, 'zzz', cfg(half_life_days=1e9))
        res = out['results']
        assert res[0]['turn_id'] == 'new'
        assert res[0]['filter_score'] >= res[1]['filter_score']


# --------------------------------------------------------------------------- @import retirement (v1.5.0)
class TestNoContextFile(TestNoContextFile):  # v1.5.0 additions
    def _run(self, tmp_path, monkeypatch, lines):
        claude_dir = stop_in(tmp_path, monkeypatch)
        own = tmp_path / 'home' / '.n3mc' / '.memory' / 'memory_context.md'
        monkeypatch.setattr(n3memory, 'CONTEXT_FILE', str(own))
        md = claude_dir / 'CLAUDE.md'
        md.write_text('\n'.join(lines(own)) + '\n', encoding='utf-8')
        n3memory.cmd_stop({})
        return md.read_text(encoding='utf-8'), claude_dir

    def test_foreign_lines_kept(self, tmp_path, monkeypatch):
        keep = ['@../.memory/memory_context.md',
                '@C:/other/.memory/memory_context.md',
                '@docs/notes_about_memory_context.md']
        text, claude_dir = self._run(tmp_path, monkeypatch, lambda own: ['# Mine'] + keep)
        for line in keep:
            assert line in text
        assert not [p for p in os.listdir(claude_dir) if '.bak-n3mc-' in p]

    def test_backslash_form_of_own_path_removed(self, tmp_path, monkeypatch):
        text, _ = self._run(
            tmp_path, monkeypatch,
            lambda own: ['# Mine', '@' + str(own).replace('/', '\\'),
                         '@../.memory/memory_context.md'])
        assert '# Mine' in text
        assert '@../.memory/memory_context.md' in text
        assert str(tmp_path).replace('/', '\\') not in text


# --------------------------------------------------------------------------- behavior md (v1.5.0)
class TestBehaviorMd(TestBehaviorMd):  # v1.5.0 additions
    def test_two_files_two_notices_with_paths(self, tmp_path):
        paths = []
        for name in ('a', 'b'):
            rule = tmp_path / name / '.claude' / 'rules' / 'n3mc-behavior.md'
            rule.parent.mkdir(parents=True)
            rule.write_text('# my own rules\n', encoding='utf-8')
            n3memory._ensure_behavior_md(str(rule))
            paths.append(rule)
        ns = notices()
        for rule in paths:
            key = n3memory._behavior_notice_key(str(rule))
            assert key in ns
            assert len(key) == len('behavior_md_edited:') + 12
            assert os.path.abspath(str(rule)) in ns[key]['message']

    def test_notice_cleared_when_marker_present(self, tmp_path):
        rule = tmp_path / '.claude' / 'rules' / 'n3mc-behavior.md'
        rule.parent.mkdir(parents=True)
        rule.write_text('# my own rules\n', encoding='utf-8')
        n3memory._ensure_behavior_md(str(rule))
        key = n3memory._behavior_notice_key(str(rule))
        assert key in notices()
        rule.write_text(n3memory._BEHAVIOR_MD_MARKER + '\n# merged\n', encoding='utf-8')
        n3memory._ensure_behavior_md(str(rule))
        assert key not in notices()


# --------------------------------------------------------------------------- server start (v1.5.0)
class FakeProc:
    def __init__(self, code=None, pid=4321):
        self.code = code
        self.pid = pid

    def poll(self):
        return self.code


class TestServerStart:
    def test_child_exit_stops_wait_and_notifies(self, monkeypatch):
        monkeypatch.setattr(n3memory, '_get_health', lambda c: None)
        monkeypatch.setattr(n3memory, '_spawn_server', lambda: FakeProc(code=3))
        t0 = time.time()
        assert n3memory.ensure_server({'server_port': 1}) is False
        assert time.time() - t0 < 5
        n = notices()['server_start_failed']['message']
        assert 'exit code 3' in n and n3memory.SERVER_LOG in n
        assert os.path.exists(n3memory.SERVER_START_FAILED_MARKER)
        assert not os.path.exists(n3memory.PID_FILE)

    def test_no_retry_within_five_minutes(self, monkeypatch, capsys):
        monkeypatch.setattr(n3memory, '_get_health', lambda c: None)
        with open(n3memory.SERVER_START_FAILED_MARKER, 'w') as f:
            f.write(str(time.time() - 60))

        def no_spawn():
            raise AssertionError('must not start within 5 minutes of a failure')
        monkeypatch.setattr(n3memory, '_spawn_server', no_spawn)
        assert n3memory.ensure_server({'server_port': 1}) is False
        n3memory._do_search('hello', {'server_port': 1})
        assert 'memory search unavailable' in capsys.readouterr().out

    def test_retry_after_five_minutes_and_success_clears(self, monkeypatch):
        with open(n3memory.SERVER_START_FAILED_MARKER, 'w') as f:
            f.write(str(time.time() - 301))
        n3memory._notify('server_start_failed', 'old failure')
        state = {'spawned': False}

        def health(c):
            if not state['spawned']:
                return None
            return {'status': 'ok', 'root': me(), 'code_version': n3memory._code_version()}

        def spawn():
            state['spawned'] = True
            return FakeProc(code=None)
        monkeypatch.setattr(n3memory, '_get_health', health)
        monkeypatch.setattr(n3memory, '_spawn_server', spawn)
        assert n3memory.ensure_server({'server_port': 1}) is True
        assert 'server_start_failed' not in notices()
        assert not os.path.exists(n3memory.SERVER_START_FAILED_MARKER)

    def test_still_starting_after_60s_not_replaced(self, monkeypatch, capsys):
        monkeypatch.setattr(n3memory, '_get_health', lambda c: None)
        with open(n3memory.PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
        monkeypatch.setattr(n3memory, '_process_alive', lambda pid: True)
        monkeypatch.setattr(n3memory, '_is_own_server_process', lambda pid: True)
        monkeypatch.setattr(n3memory, '_wait_for_server', lambda *a, **k: False)

        def no_start(c):
            raise AssertionError('must not start a second server')
        monkeypatch.setattr(n3memory, '_start_server', no_start)
        assert n3memory.ensure_server({'server_port': 1}) is False
        assert os.path.exists(n3memory.PID_FILE)
        n3memory._do_search('hello', {'server_port': 1})
        assert 'the memory server is still starting' in capsys.readouterr().out

    def test_still_starting_saves_directly(self, monkeypatch):
        monkeypatch.setattr(n3memory, '_NO_SERVER_REASON', n3memory._STILL_STARTING)

        def no_post(*a, **k):
            raise AssertionError('must not post while the server is starting')
        monkeypatch.setattr(n3memory, '_post', no_post)
        n3memory._do_buffer('note', {'server_port': 1, 'owner_id': 'o'})
        assert count(n3memory.DB_PATH) == 1

    def test_without_psutil_young_pid_file_is_starting(self, monkeypatch):
        monkeypatch.setattr(n3memory, '_psutil_available', lambda: False)
        with open(n3memory.PID_FILE, 'w') as f:
            f.write('999999')
        assert n3memory._pid_file_process_starting(999999) is True
        old = time.time() - 120
        os.utime(n3memory.PID_FILE, (old, old))
        assert n3memory._pid_file_process_starting(999999) is False

    def test_popen_detached_and_stderr_to_server_log(self, monkeypatch):
        monkeypatch.setattr(n3memory, '_spawn_server', REAL_SPAWN_SERVER)
        with open(n3memory.SERVER_LOG, 'wb') as f:
            f.write(b'x' * (n3memory._SERVER_LOG_MAX_BYTES + 10))
        calls = []

        def fake_popen(args, **kwargs):
            calls.append((args, kwargs))
            return FakeProc()
        monkeypatch.setattr(n3memory.subprocess, 'Popen', fake_popen)
        n3memory._spawn_server()
        args, kw = calls[0]
        assert args[1:] == ['-m', 'n3memorycore.n3memory', '--run-server']
        assert os.path.normcase(kw['stderr'].name) == os.path.normcase(n3memory.SERVER_LOG)
        assert kw['stdout'] == n3memory.subprocess.DEVNULL
        assert kw['stdin'] == n3memory.subprocess.DEVNULL
        if os.name == 'nt':
            flags = kw['creationflags']
            assert flags & n3memory.subprocess.CREATE_NEW_PROCESS_GROUP
            assert flags & n3memory.subprocess.CREATE_NO_WINDOW
        else:
            assert kw['start_new_session'] is True
        assert os.path.getsize(n3memory.SERVER_LOG) == 0   # truncated (> 1 MB)


# --------------------------------------------------------------------------- server API (v1.5.0)
class TestServerApi:
    PORT = 18999

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        path = new_db(tmp_path, 'api.db')
        monkeypatch.setattr(n3memory, 'DB_PATH', path)
        monkeypatch.setattr(n3memory, '_srv_cfg', {'owner_id': 'o', 'local_id': 'l',
                                                   'server_port': self.PORT})
        monkeypatch.setattr(n3memory, '_host_ok', REAL_CHECK_HOST)
        monkeypatch.setattr(proc, 'embed_passage', fake_embed)
        return TestClient(n3memory.app), path

    ENDPOINTS = [('get', '/health', None), ('post', '/buffer', {'content': 'x'}),
                 ('post', '/search', {'query': 'x'}), ('post', '/repair', {}),
                 ('get', '/list', None), ('post', '/shutdown', {'root': '/x'}),
                 ('delete', '/delete/abc', None), ('post', '/gc', {}),
                 ('post', '/import', {})]

    @pytest.mark.parametrize('method,url,body', ENDPOINTS)
    def test_foreign_host_rejected(self, client, method, url, body):
        c, _ = client
        kw = {'headers': {'Host': 'evil.example'}}
        if body is not None:
            kw['json'] = body
        assert getattr(c, method)(url, **kw).status_code == 403

    @pytest.mark.parametrize('host', ['127.0.0.1:%d' % PORT, 'localhost:%d' % PORT])
    def test_local_hosts_pass(self, client, host):
        c, _ = client
        assert c.get('/health', headers={'Host': host}).status_code == 200
        assert c.get('/list', headers={'Host': host}).status_code == 200

    def test_wrong_port_rejected(self, client):
        c, _ = client
        assert c.get('/health', headers={'Host': '127.0.0.1:1'}).status_code == 403

    def test_db_endpoints_are_plain_def(self):
        import inspect
        eps = {r.path: r.endpoint for r in n3memory.app.routes if hasattr(r, 'endpoint')}
        for p in ('/buffer', '/search', '/repair', '/list'):
            assert not inspect.iscoroutinefunction(eps[p]), p

    def test_repair_max_records(self, client):
        c, path = client
        for i in range(250):
            add(path, f'unindexed record {i}')       # no vector
        h = {'Host': f'127.0.0.1:{self.PORT}'}
        r = c.post('/repair', json={'max_records': 200}, headers=h)
        assert r.json()['count'] == 200
        conn = db.get_connection(path)
        try:
            assert len(db.find_unindexed_memories(conn, limit=1000)) == 50
        finally:
            conn.close()
        assert c.post('/repair', json={}, headers=h).json()['count'] == 50

    def test_search_unreadable_since_is_400(self, client):
        c, _ = client
        r = c.post('/search', json={'query': 'x', 'since': 'yesterday'},
                   headers={'Host': f'127.0.0.1:{self.PORT}'})
        assert r.status_code == 400

    def test_hook_submit_sends_repair_limit(self, monkeypatch):
        sent = []

        def fake_post(c, p, payload, *a, **k):
            sent.append((p, payload))
            return {'results': [], 'pairs': {}, 'pairs_omitted': {}} if p == '/search' else {}
        monkeypatch.setattr(n3memory, 'ensure_server', lambda c: True)
        monkeypatch.setattr(n3memory, '_post', fake_post)
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'prompt': 'hello there'})))
        n3memory.cmd_hook_submit({'server_port': 1, 'search_query_max_chars': 2000})
        assert ('/repair', {'max_records': 200}) in sent

    def test_cli_sends_local_host_header(self, monkeypatch):
        import urllib.request
        seen = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{}'

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_header('Host'))
            return Resp()
        monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
        REAL_POST({'server_port': 18777}, '/x', {})
        REAL_GET({'server_port': 18777}, '/x')
        assert seen == ['127.0.0.1:18777', '127.0.0.1:18777']


# --------------------------------------------------------------------------- config (v1.5.0)
class TestConfig(TestConfig):  # v1.5.0 additions
    @pytest.mark.parametrize('key,value', [
        ('search_query_max_chars', '2000.0'), ('half_life_days', '0'),
        ('server_port', '70000'), ('server_port', '-1'), ('min_score', '1.5'),
    ])
    def test_out_of_range_ignored(self, key, value):
        write_cfg('{' + IDS + f', "{key}": {value}' + '}')
        c = n3memory._load_config()
        assert c[key] == n3memory._DEFAULT_CONFIG[key]
        assert key in notices()['config_type']['message']

    def test_valid_values_accepted(self):
        assert n3memory._config_value_ok('half_life_days', 0.5)
        assert n3memory._config_value_ok('server_port', 18600)
        assert n3memory._config_value_ok('min_score', 0)
        assert not n3memory._config_value_ok('pair_top_n', 3.0)

    def test_exclusive_creation_reads_concurrent_file(self, monkeypatch):
        other = ('{"owner_id": "33333333-3333-4333-8333-333333333333", '
                 '"local_id": "44444444-4444-4444-8444-444444444444"}')
        real_fill = n3memory._fill_ids

        def racing_fill(c):
            real_fill(c)
            if not os.path.exists(n3memory.CONFIG_FILE):
                write_cfg(other)          # another hook wins the race
        monkeypatch.setattr(n3memory, '_fill_ids', racing_fill)
        c = n3memory._load_config()
        assert c['owner_id'] == '33333333-3333-4333-8333-333333333333'
        assert json.loads(read_cfg())['owner_id'] == '33333333-3333-4333-8333-333333333333'

    def test_missing_file_created_exclusively(self, monkeypatch):
        import builtins
        modes = []
        real_open = builtins.open

        def spy(file, mode='r', *a, **k):
            if os.path.normcase(str(file)) == os.path.normcase(n3memory.CONFIG_FILE):
                modes.append(mode)
            return real_open(file, mode, *a, **k)
        monkeypatch.setattr(builtins, 'open', spy)
        n3memory._load_config()
        assert modes and modes[0] == 'x'

    def test_update_uses_replace(self, monkeypatch):
        write_cfg('{' + IDS + '}')
        replaced = []
        real_replace = os.replace

        def spy(src, dst):
            replaced.append((src, dst))
            return real_replace(src, dst)
        monkeypatch.setattr(os, 'replace', spy)
        n3memory._load_config()
        assert any(os.path.normcase(d) == os.path.normcase(n3memory.CONFIG_FILE)
                   and os.path.dirname(s) == os.path.dirname(d) for s, d in replaced)
        assert 'server_port' in json.loads(read_cfg())

    def test_notices_written_atomically(self, monkeypatch):
        replaced = []
        real_replace = os.replace
        monkeypatch.setattr(os, 'replace', lambda s, d: replaced.append(d) or real_replace(s, d))
        n3memory._notify('stale_server', 'msg')
        assert n3memory.NOTICES_FILE in replaced


# --------------------------------------------------------------------------- transcript (v1.5.0)
def tline(typ, content, sidechain=False):
    return json.dumps({'type': typ, 'isSidechain': sidechain,
                       'message': {'role': typ, 'content': content}})


def write_transcript(tmp_path, with_current=True):
    lines = [
        tline('user', 'first question'),
        tline('assistant', [{'type': 'thinking', 'thinking': 'hidden thoughts'}]),
        tline('assistant', [{'type': 'text', 'text': 'Let me check.'},
                            {'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {}}]),
        tline('user', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'file body'}]),
        tline('assistant', [{'type': 'text', 'text': 'SIDECHAIN TEXT'}], sidechain=True),
        'not json at all',
        json.dumps({'type': 'system', 'content': 'meta'}),
        tline('assistant', [{'type': 'text', 'text': 'Final answer here.'}]),
    ]
    if with_current:
        lines.append(tline('user', [{'type': 'text', 'text': 'second question'}]))
    p = tmp_path / 'session.jsonl'
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(p)


# Only the LAST text block — the same text Stop receives as last_assistant_message.
EXPECTED_PREV = 'Final answer here.'


class TestHookSubmit(TestHookSubmit):  # v1.5.0 additions
    @pytest.mark.parametrize('with_current', [True, False])
    def test_reads_previous_response(self, tmp_path, with_current):
        p = write_transcript(tmp_path, with_current)
        assert n3memory._read_previous_response(p, 'second question') == EXPECTED_PREV

    def test_interrupted_reply_is_found(self, tmp_path):
        """An interruption marker, meta lines and slash-command lines between
        the partial reply and the new prompt are not prompts (spec §4.5 D3)."""
        lines = [
            tline('user', 'first question'),
            tline('assistant', [{'type': 'text', 'text': 'Partial reply before the interrupt.'}]),
            tline('user', [{'type': 'text', 'text': '[Request interrupted by user]'}]),
            json.dumps({'type': 'user', 'isMeta': True, 'isSidechain': False,
                        'message': {'role': 'user', 'content': 'caveat text'}}),
            tline('user', '<command-name>/model</command-name>'),
            tline('user', '<local-command-stdout>Set model</local-command-stdout>'),
            tline('user', 'second question'),
        ]
        p = tmp_path / 'interrupted.jsonl'
        p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        assert n3memory._read_previous_response(str(p), 'second question') == \
            'Partial reply before the interrupt.'

    def test_no_previous_reply_returns_empty(self, tmp_path):
        p = tmp_path / 'fresh.jsonl'
        p.write_text(tline('user', 'only question') + '\n', encoding='utf-8')
        assert n3memory._read_previous_response(str(p), 'only question') == ''

    def test_last_text_part_of_the_line(self, tmp_path):
        p = tmp_path / 'multi.jsonl'
        p.write_text('\n'.join([
            tline('user', 'q1'),
            tline('assistant', [{'type': 'text', 'text': 'part one'},
                                {'type': 'text', 'text': 'part two'}]),
            tline('user', 'q2'),
        ]) + '\n', encoding='utf-8')
        assert n3memory._read_previous_response(str(p), 'q2') == 'part two'

    def run_hook(self, monkeypatch, payload, saved=None):
        monkeypatch.setattr(n3memory, 'ensure_server', lambda c: True)

        def fake_post(c, p, *a, **k):
            return {'results': [], 'pairs': {}, 'pairs_omitted': {}} if p == '/search' else {}
        monkeypatch.setattr(n3memory, '_post', fake_post)
        if saved is not None:
            monkeypatch.setattr(n3memory, '_do_buffer',
                                lambda text, c, **k: saved.append((text, k.get('turn_id'))))
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
        n3memory.cmd_hook_submit({'server_port': 1, 'search_query_max_chars': 2000,
                                  'owner_id': 'o'})

    def test_saved_under_previous_turn_id(self, tmp_path, monkeypatch, capsys):
        n3memory._write_turn_id('t-prev', 'sess')
        saved = []
        self.run_hook(monkeypatch, {'session_id': 'sess', 'prompt': 'second question',
                                    'transcript_path': write_transcript(tmp_path)}, saved)
        assert saved[0] == ('[claude] ' + EXPECTED_PREV, 't-prev')
        assert saved[1][0] == '[user] second question'
        assert saved[1][1] not in (None, 't-prev')
        assert n3memory._read_turn_id('sess') == saved[1][1]
        assert '# Recalled Memory Context' in capsys.readouterr().out

    def test_supplied_last_assistant_message_wins(self, tmp_path, monkeypatch):
        n3memory._write_turn_id('t-prev', 'sess')
        saved = []
        self.run_hook(monkeypatch, {'session_id': 'sess', 'prompt': 'q2',
                                    'last_assistant_message': 'given reply',
                                    'transcript_path': write_transcript(tmp_path)}, saved)
        assert saved[0] == ('[claude] given reply', 't-prev')

    def test_no_duplicate_when_stop_saved_it(self, tmp_path, monkeypatch):
        n3memory._write_turn_id('t-prev', 'sess')
        c = {'server_port': 1, 'owner_id': 'o'}
        monkeypatch.setattr(n3memory, 'ensure_server', lambda c: False)
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(
            {'session_id': 'sess', 'last_assistant_message': EXPECTED_PREV})))
        n3memory.cmd_save_claude_turn(c)
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(
            {'session_id': 'sess', 'prompt': 'second question',
             'transcript_path': write_transcript(tmp_path)})))
        n3memory.cmd_hook_submit(dict(c, search_query_max_chars=2000))
        conn = db.get_connection(n3memory.DB_PATH)
        try:
            n = conn.execute("SELECT COUNT(*) FROM memories WHERE content LIKE '[claude%'"
                             ).fetchone()[0]
        finally:
            conn.close()
        assert n == 1

    @pytest.mark.parametrize('kind', ['missing', 'broken'])
    def test_unreadable_transcript_logged(self, tmp_path, monkeypatch, capsys, kind):
        if kind == 'missing':
            tp = str(tmp_path / 'nope.jsonl')
        else:
            bad = tmp_path / 'bad.jsonl'
            bad.write_bytes(b'\x00\x01 garbage {{{\n')
            tp = str(bad)
        saved = []
        self.run_hook(monkeypatch, {'session_id': 's', 'prompt': 'hello world',
                                    'transcript_path': tp}, saved)
        assert any(l.get('stage') == 'transcript_unreadable' for l in audit_lines())
        assert saved and saved[-1][0] == '[user] hello world'
        assert '# Recalled Memory Context' in capsys.readouterr().out

    def test_image_only_prompt_new_turn_id(self, monkeypatch):
        n3memory._write_turn_id('keep', 'sess')
        saved = []
        self.run_hook(monkeypatch, {'session_id': 'sess',
                                    'prompt': [{'type': 'image', 'source': {}}]}, saved)
        assert n3memory._read_turn_id('sess') not in (None, 'keep')
        assert not any(t.startswith('[user') for t, _ in saved)


# --------------------------------------------------------------------------- purify before chunk (v1.5.0)
class TestPurifyBeforeChunk:
    def hook_saved(self, monkeypatch, text):
        saved = []
        monkeypatch.setattr(n3memory, 'ensure_server', lambda c: True)
        monkeypatch.setattr(n3memory, '_post', lambda c, p, *a, **k: (
            {'results': [], 'pairs': {}, 'pairs_omitted': {}} if p == '/search' else {}))
        monkeypatch.setattr(n3memory, '_do_buffer',
                            lambda t, c, **k: saved.append(t))
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'prompt': text})))
        n3memory.cmd_hook_submit({'server_port': 1, 'search_query_max_chars': 2000})
        return '\n'.join(saved)

    def test_prose_between_code_blocks_kept(self, monkeypatch):
        text = ('Intro prose sentence number one. ' * 5 +
                '\n```\nCODE_ONE_' + 'a' * 120 + '\n```\n' +
                'MIDDLE PROSE stays here. ' * 4 +
                '\n```\nCODE_TWO_' + 'b' * 120 + '\n```\n' +
                'Closing prose sentence. ' * 3)
        assert len(text) > 500
        joined = self.hook_saved(monkeypatch, text)
        assert 'MIDDLE PROSE stays here.' in joined
        assert 'CODE_ONE_' not in joined and 'CODE_TWO_' not in joined
        assert '[code omitted]' in joined

    def test_long_code_block_replaced(self, monkeypatch):
        text = 'Look at this:\n```\n' + 'LONGCODE line\n' * 60 + '```\nThanks.'
        joined = self.hook_saved(monkeypatch, text)
        assert 'LONGCODE' not in joined
        assert '[code omitted]' in joined and 'Thanks.' in joined


# --------------------------------------------------------------------------- sentences (v1.5.0)
class TestChunkSentences:
    def test_japanese_split_after_sentence_marks(self):
        sents = ['これは長い日本語の文章の一部であり、空行なしで続いていきます' + str(i) + m
                 for i, m in zip(range(18), '。！？' * 6)]
        text = ''.join(sents)
        assert len(text) > 400 and '\n' not in text
        chunks = proc.chunk_text(text)
        assert len(chunks) > 1
        for c in chunks:
            assert c[-1] in '。！？'


# --------------------------------------------------------------------------- time bounds (v1.5.0)
JST = timezone(timedelta(hours=9))


class TestTimeBounds:
    @pytest.fixture(autouse=True)
    def jst(self, monkeypatch):
        monkeypatch.setattr(n3memory, '_LOCAL_TZ', JST)

    def test_date_only_is_local_midnight(self):
        assert n3memory._parse_time_bound('2026-11-13') == '2026-11-12T15:00:00+00:00'
        assert (n3memory._parse_time_bound('2026-11-13', is_until=True)
                == '2026-11-13T14:59:59.999999+00:00')

    def test_offset_honoured(self):
        assert n3memory._parse_time_bound('2026-11-13T09:00+00:00') == '2026-11-13T09:00:00+00:00'
        assert n3memory._parse_time_bound('2026-11-13T09:00Z') == '2026-11-13T09:00:00+00:00'

    def test_t_and_space_same(self):
        a = n3memory._parse_time_bound('2026-11-13T09:00:00')
        b = n3memory._parse_time_bound('2026-11-13 09:00:00')
        assert a == b == '2026-11-13T00:00:00+00:00'

    def test_morning_record_found_by_local_date(self, tmp_path):
        path = new_db(tmp_path)
        add(path, 'saved at 08:00 JST on the 13th', ts='2026-11-12T23:00:00.123456+00:00')
        since, until = n3memory._parse_time_range('2026-11-13', '2026-11-13')
        conn = db.get_connection(path)
        try:
            rows = db.get_all_memories(conn, since=since, until=until)
        finally:
            conn.close()
        assert len(rows) == 1

    @pytest.mark.parametrize('argv,msg', [
        (['--search', 'x', '--since', 'yesterday'], "--since: cannot read 'yesterday'"),
        (['--list', '--until', '2026-13-01'], "--until: cannot read '2026-13-01'"),
        (['--list', '--since', '2026-11-14', '--until', '2026-11-13'],
         '--since is later than --until'),
    ])
    def test_unreadable_exits_2(self, monkeypatch, capsys, argv, msg):
        monkeypatch.setattr('sys.argv', ['n3mc'] + argv)

        def no_config():
            raise AssertionError('must exit before doing anything')
        monkeypatch.setattr(n3memory, '_load_config', no_config)
        with pytest.raises(SystemExit) as ei:
            n3memory.main()
        assert ei.value.code == 2
        assert msg in capsys.readouterr().err


# --------------------------------------------------------------------------- DB corruption (v1.5.0)
class TestDbCorrupt:
    def _make_files(self):
        for sfx, data in (('', b'db'), ('-wal', b'wal'), ('-shm', b'shm')):
            with open(n3memory.DB_PATH + sfx, 'wb') as f:
                f.write(data)

    def test_two_corruptions_two_backups_with_sidecars(self):
        import re
        self._make_files()
        first = n3memory._handle_corrupt_db()
        self._make_files()
        second = n3memory._handle_corrupt_db()
        assert first != second
        pat = re.compile(r'^n3memory\.db\.corrupt-\d{14}(-\d+)?\.bak$')
        for b, in_db in ((first, b'db'), (second, b'db')):
            assert pat.match(os.path.basename(b))
            with open(b, 'rb') as f:
                assert f.read() == in_db
            with open(b + '-wal', 'rb') as f:
                assert f.read() == b'wal'
            assert os.path.exists(b + '-shm')
        assert not os.path.exists(n3memory.DB_PATH + '-wal')
        assert 'db_corrupt' in notices()
        assert second in notices()['db_corrupt']['message']

    def test_quick_check_once_per_process(self, monkeypatch):
        calls = []
        real_get = n3memory.get_connection

        class Spy:
            def __init__(self, c):
                self._c = c

            def execute(self, sql, *a):
                if 'quick_check' in sql:
                    calls.append(1)
                return self._c.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._c, name)

        monkeypatch.setattr(n3memory, 'get_connection', lambda p: Spy(real_get(p)))
        c = {'owner_id': 'o'}
        for i in range(3):
            n3memory._buffer_direct(f'chunk {i}', c)
        assert len(calls) == 1
        assert count(n3memory.DB_PATH) == 3

    def test_quick_check_failure_sets_db_aside(self, monkeypatch):
        real_get = n3memory.get_connection
        state = {'n': 0}

        class Cur:
            def fetchone(self):
                return ('*** in database main *** page 3 corrupt',)

        class Bad:
            def __init__(self, c):
                self._c = c

            def execute(self, sql, *a):
                if 'quick_check' in sql:
                    return Cur()
                return self._c.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._c, name)

        def get(p):
            state['n'] += 1
            conn = real_get(p)
            return Bad(conn) if state['n'] == 1 else conn
        monkeypatch.setattr(n3memory, 'get_connection', get)
        n3memory._buffer_direct('after corruption', {'owner_id': 'o'})
        backups = [p for p in os.listdir(n3memory.MEMORY_DIR) if '.corrupt-' in p]
        assert any(p.endswith('.bak') for p in backups)
        assert 'db_corrupt' in notices()
        assert count(n3memory.DB_PATH) == 1


# --------------------------------------------------------------------------- --init (v1.5.0)
PRO_HOOK = 'python "C:/N3MC-Pro_X/N3MemoryCore/n3mc_hook.py"'


class TestInit:
    @pytest.fixture
    def settings(self, tmp_path, monkeypatch):
        home = tmp_path / 'fake_user'
        (home / '.claude').mkdir(parents=True)
        monkeypatch.setenv('HOME', str(home))
        monkeypatch.setenv('USERPROFILE', str(home))
        monkeypatch.setattr(n3memory, '_settings_path', REAL_SETTINGS_PATH)
        path = home / '.claude' / 'settings.json'
        assert os.path.normcase(n3memory._settings_path()) == os.path.normcase(str(path))
        monkeypatch.setattr(n3memory.shutil, 'which',
                            lambda n: f'C:\\fake\\Scripts\\{n}.EXE')
        return path

    def own_cmds(self, data, event):
        return [h['command'] for e in data['hooks'][event] for h in e['hooks']
                if n3memory.is_own_hook(h['command'], event)]

    def test_pro_hook_kept_and_reported(self, settings, capsys):
        settings.write_text(json.dumps({'hooks': {'UserPromptSubmit': [
            {'hooks': [{'type': 'command', 'command': PRO_HOOK}]}]}}), encoding='utf-8')
        assert n3memory.cmd_init() == 0
        data = json.loads(settings.read_text(encoding='utf-8'))
        cmds = [h['command'] for e in data['hooks']['UserPromptSubmit'] for h in e['hooks']]
        assert PRO_HOOK in cmds
        assert self.own_cmds(data, 'UserPromptSubmit') == ['C:/fake/Scripts/n3mc-hook.EXE']
        assert 'Another N3MC hook is registered for UserPromptSubmit' in capsys.readouterr().out

    def test_own_forms_replaced_not_duplicated(self, settings):
        pkg_script = os.path.join(n3memory._PKG_DIR, 'n3mc_hook.py').replace('\\', '/')
        old = ['C:/old/Scripts/n3mc-hook.exe', '/usr/bin/n3mc-hook',
               '"C:/Python/python.exe" -m n3memorycore.n3mc_hook',
               f'python "{pkg_script}"']
        settings.write_text(json.dumps({'hooks': {
            'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': c}]} for c in old],
            'Stop': [{'hooks': [{'type': 'command',
                                 'command': '"C:/P/python.exe" -m n3memorycore.n3mc_stop_hook'}]}],
        }}), encoding='utf-8')
        assert n3memory.cmd_init() == 0
        data = json.loads(settings.read_text(encoding='utf-8'))
        assert self.own_cmds(data, 'UserPromptSubmit') == ['C:/fake/Scripts/n3mc-hook.EXE']
        assert self.own_cmds(data, 'Stop') == ['C:/fake/Scripts/n3mc-stop-hook.EXE']
        assert len(data['hooks']['UserPromptSubmit']) == 1

    def test_rerun_writes_nothing(self, settings, capsys):
        settings.write_text('{"model": "x"}', encoding='utf-8')
        assert n3memory.cmd_init() == 0
        first = settings.read_bytes()
        backups = sorted(p for p in os.listdir(settings.parent) if '.bak-n3mc-' in p)
        assert len(backups) == 1
        with open(settings.parent / backups[0], encoding='utf-8') as f:
            assert f.read() == '{"model": "x"}'
        capsys.readouterr()
        assert n3memory.cmd_init() == 0
        assert settings.read_bytes() == first
        assert sorted(p for p in os.listdir(settings.parent) if '.bak-n3mc-' in p) == backups
        assert 'no change' in capsys.readouterr().out

    def test_invalid_settings_untouched_exit_1(self, settings):
        settings.write_text('{"hooks": [,]}', encoding='utf-8')
        assert n3memory.cmd_init() == 1
        assert settings.read_text(encoding='utf-8') == '{"hooks": [,]}'
        settings.write_text('{"permissions": {"allow": "Bash(x)"}}', encoding='utf-8')
        assert n3memory.cmd_init() == 1
        assert settings.read_text(encoding='utf-8') == '{"permissions": {"allow": "Bash(x)"}}'
        assert not [p for p in os.listdir(settings.parent) if '.bak-n3mc-' in p]

    def test_main_exit_status_1(self, settings, monkeypatch):
        settings.write_text('not json', encoding='utf-8')
        monkeypatch.setattr('sys.argv', ['n3mc', '--init'])
        with pytest.raises(SystemExit) as ei:
            n3memory.main()
        assert ei.value.code == 1

    def test_permission_rules_added_existing_kept(self, settings):
        settings.write_text(json.dumps({'permissions': {'allow': ['Bash(git status)'],
                                                        'deny': ['Bash(rm *)']}}),
                            encoding='utf-8')
        assert n3memory.cmd_init() == 0
        perms = json.loads(settings.read_text(encoding='utf-8'))['permissions']
        assert perms['allow'][0] == 'Bash(git status)'
        assert perms['deny'] == ['Bash(rm *)']
        for rule in n3memory.PERMISSION_RULES:
            assert perms['allow'].count(rule) == 1
        assert 'Bash(n3mc --show-turn *)' in perms['allow']
        assert 'Bash(n3mc --recall-thread *)' in perms['allow']

    def test_is_own_hook_unit(self):
        own = n3memory.is_own_hook
        assert own('C:/x/Scripts/n3mc-hook.EXE', 'UserPromptSubmit')
        assert own('n3mc-stop-hook', 'Stop')
        assert own('"/usr/bin/python3" -m n3memorycore.n3mc_hook', 'UserPromptSubmit')
        assert not own(PRO_HOOK, 'UserPromptSubmit')
        assert not own('echo n3mc_hook', 'UserPromptSubmit')
        assert not own('n3mc-hook', 'Stop')


# --------------------------------------------------------------------------- housekeeping (v1.5.0)
class TestHousekeeping:
    def test_stop_housekeeping(self, tmp_path, monkeypatch, capsys):
        stop_in(tmp_path, monkeypatch)
        old = os.path.join(n3memory.MEMORY_DIR, 'turn_id_old.txt')
        new = os.path.join(n3memory.MEMORY_DIR, 'turn_id_new.txt')
        for p in (old, new):
            with open(p, 'w') as f:
                f.write('t')
        t = time.time() - 31 * 86400
        os.utime(old, (t, t))
        with open(n3memory.AUDIT_LOG, 'w', encoding='utf-8') as f:
            f.write('x' * 100)
        monkeypatch.setattr(n3memory, '_AUDIT_LOG_MAX_BYTES', 50)
        n3memory.cmd_stop({})
        assert not os.path.exists(old) and os.path.exists(new)
        assert os.path.exists(n3memory.AUDIT_LOG + '.1')
        assert not os.path.exists(n3memory.AUDIT_LOG)
        assert 'Physical save failed' not in capsys.readouterr().err

    def test_stop_never_prints_physical_save_warning(self, tmp_path, monkeypatch, capsys):
        stop_in(tmp_path, monkeypatch)

        def boom(path):
            raise OSError('disk full')
        monkeypatch.setattr(n3memory, '_ensure_behavior_md', boom)
        n3memory.cmd_stop({})
        err = capsys.readouterr().err
        assert 'disk full' in err
        assert 'Physical save failed' not in err


# --------------------------------------------------------------------------- model retry (v1.5.0)
class TestModelRetry:
    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        for attr, val in (('_model', None), ('_model_name', None),
                          ('_configured_model', None), ('_model_failed_at', None),
                          ('_model_failed_error', None)):
            monkeypatch.setattr(proc, attr, val)

    def test_no_retry_for_60_seconds(self, monkeypatch, capsys):
        calls = []

        def failing(name):
            calls.append(name)
            raise OSError('offline')
        monkeypatch.setattr(proc, '_load_model', failing)
        with pytest.raises(OSError):
            proc.get_model('some/model')
        with pytest.raises(RuntimeError):
            proc.get_model()
        assert len(calls) == 1
        assert 'could not be loaded' in capsys.readouterr().err
        monkeypatch.setattr(proc, '_model_failed_at', proc._model_failed_at - 61)
        with pytest.raises(OSError):
            proc.get_model()
        assert calls == ['some/model', 'some/model']

    def test_env_var_has_no_effect(self, monkeypatch):
        names = []
        monkeypatch.setenv('N3MC_EMBED_MODEL', 'bogus/model')
        monkeypatch.setattr(proc, '_load_model', lambda name: names.append(name) or object())
        proc.get_model()
        assert names == [proc.DEFAULT_EMBED_MODEL]


# --------------------------------------------------------------------------- packaging (v1.5.0)
class TestPackaging:
    def test_psutil_is_a_dependency(self):
        tomllib = pytest.importorskip('tomllib')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'pyproject.toml'), 'rb') as f:
            deps = tomllib.load(f)['project']['dependencies']
        assert any(d.split('>')[0].split('=')[0].strip().lower() == 'psutil' for d in deps)
