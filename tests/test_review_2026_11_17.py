"""Regression tests for the code review of 2026-11-17 (spec §4, §5).

2. No chunk is longer than max_chars, overlap and separator included.
3. A hook command written with backslashes and no quotes is recognized.

No embedding model. Real data and real settings are never touched
(conftest._isolate_real_data; settings.json lives in a temporary home).
"""
import json
import os

import pytest

from n3memorycore import n3memory
from n3memorycore.core.processor import chunk_text

from tests.test_regressions import REAL_SETTINGS_PATH


def letters(chunks):
    return ''.join(ch for c in chunks for ch in c if not ch.isspace())


class TestChunkLengthBound:
    def test_review_case(self):
        chunks = chunk_text('x' * 10 + '\n\n' + 'y' * 400)
        assert max(len(c) for c in chunks) <= 400
        joined = letters(chunks)
        assert joined.count('x') >= 10 and joined.count('y') >= 400

    @pytest.mark.parametrize('max_chars', [400, 120, 50])
    def test_paragraphs(self, max_chars):
        paras = [('p%d ' % i) * (5 + (i * 7) % (max_chars // 3)) for i in range(40)]
        text = '\n\n'.join(p.strip() for p in paras)
        chunks = chunk_text(text, max_chars=max_chars, overlap=40)
        assert chunks and max(len(c) for c in chunks) <= max_chars

    @pytest.mark.parametrize('max_chars', [400, 120])
    def test_sentences(self, max_chars):
        text = ' '.join('Sentence number %d has %s.' % (i, 'w' * (i % 60)) for i in range(80))
        chunks = chunk_text(text, max_chars=max_chars, overlap=40)
        assert max(len(c) for c in chunks) <= max_chars

    def test_japanese(self):
        text = ''.join('これは%d番目の文で、長さを少しずつ変えています%s。' % (i, 'あ' * (i % 50))
                       for i in range(60))
        chunks = chunk_text(text)
        assert max(len(c) for c in chunks) <= 400
        # every sentence still appears in some chunk
        for i in range(60):
            assert any('これは%d番目の文' % i in c for c in chunks)

    def test_overlap_kept_when_it_fits(self):
        text = '\n\n'.join(['a' * 300, 'b' * 150])
        chunks = chunk_text(text)
        assert chunks == ['a' * 300, 'a' * 40 + '\n\n' + 'b' * 150]


class TestOwnHookBackslash:
    @pytest.mark.parametrize('cmd,event', [
        (r'C:\Tools\n3mc-hook.exe', 'UserPromptSubmit'),
        (r'C:\Tools\n3mc-stop-hook.exe', 'Stop'),
        (r'C:\Python312\python.exe -m n3memorycore.n3mc_hook', 'UserPromptSubmit'),
    ])
    def test_recognized(self, cmd, event):
        assert n3memory.is_own_hook(cmd, event)

    def test_others_not_recognized(self):
        assert not n3memory.is_own_hook(r'C:\Tools\other-hook.exe', 'UserPromptSubmit')
        assert not n3memory.is_own_hook(r'C:\Tools\n3mc-hook.exe', 'Stop')
        assert not n3memory.is_own_hook(
            r'python C:\N3MC-Pro_X\N3MemoryCore\n3mc_hook.py', 'UserPromptSubmit')


class TestInitReplacesBackslashEntry:
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
        monkeypatch.setattr(n3memory.sysconfig, 'get_path',
                            lambda *a, **k: str(tmp_path / 'no_scripts'))
        return path

    def test_no_second_entry(self, settings):
        settings.write_text(json.dumps({'hooks': {
            'UserPromptSubmit': [{'hooks': [{'type': 'command',
                                             'command': r'C:\Tools\n3mc-hook.exe'}]}],
            'Stop': [{'hooks': [{'type': 'command',
                                 'command': r'C:\Tools\n3mc-stop-hook.exe'}]}],
        }}), encoding='utf-8')
        assert n3memory.cmd_init() == 0
        data = json.loads(settings.read_text(encoding='utf-8'))
        for event in ('UserPromptSubmit', 'Stop'):
            cmds = [h['command'] for e in data['hooks'][event] for h in e['hooks']]
            assert len(cmds) == 1, cmds
            assert '\\' not in cmds[0] and n3memory.is_own_hook(cmds[0], event)
