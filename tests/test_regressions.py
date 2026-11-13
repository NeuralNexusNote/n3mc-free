"""Layer 5: regression tests for the v1.4.0 fixes (spec §7).

No embedding model: vectors are deterministic fakes. Real data and real
servers are never touched (conftest._isolate_real_data).
"""
import io
import json
import math
import os
import random
import sqlite3
import uuid
import zlib
from datetime import datetime, timezone

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
        md = claude_dir / 'CLAUDE.md'
        original = ('# CLAUDE.md\r\n\r\nMy rules\r\n\r\n'
                    '# N3MemoryCore — recalled context for the current turn\r\n'
                    '@C:/Users/my user/.n3mc/.memory/memory_context.md\r\n')
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
        assert 'behavior_md_edited' in notices()


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
