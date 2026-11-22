"""Safe import path: files in the current folder must not run inside N3MC (no model needed)."""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "n3memorycore")
SHADOWS = ["argparse", "shlex", "queue", "fastapi", "pydantic", "sqlite_vec", "numpy", "uvicorn"]


def _entry_modules():
    out = []
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py"):
            src = open(os.path.join(PKG, name), encoding="utf-8").read()
            if re.search(r'if __name__ == "__main__":\s*\n\s*sys\.exit', src):
                out.append(name)
    return out


def _env(tmp):
    e = dict(os.environ)
    e.pop("PYTHONPATH", None)
    e["N3MC_HOME"] = os.path.join(str(tmp), "data")
    e["HOME"] = e["USERPROFILE"] = os.path.join(str(tmp), "home")
    return e


def test_there_are_entry_modules():
    mods = _entry_modules()
    assert "n3memory.py" in mods and len(mods) >= 3


@pytest.mark.parametrize("name", ["n3memory.py", "n3mc_hook.py", "n3mc_stop_hook.py"])
def test_entry_module_starts_with_the_safe_path_block(name):
    src = open(os.path.join(PKG, name), encoding="utf-8").read()
    body = src.split('"""', 2)[2]  # after the docstring
    stmts = [ln for ln in body.splitlines() if ln.strip()]
    assert stmts[0] == "import os as _os" and stmts[1] == "import sys as _sys"
    assert stmts[2] == 'if __name__ == "__main__":'
    assert "del _sys.path[0]" in body


def _make_shadows(folder):
    os.makedirs(folder, exist_ok=True)
    for m in SHADOWS:
        with open(os.path.join(folder, m + ".py"), "w") as f:
            f.write(f"open({os.path.join(str(folder), 'RAN_' + m)!r}, 'w').write('x')\nraise ImportError('shadow')\n")


def test_files_in_the_current_folder_do_not_run(tmp_path):
    work = tmp_path / "work"
    _make_shadows(work)
    r = subprocess.run([sys.executable, "-m", "n3memorycore.n3memory", "--help"], cwd=work,
                       env={**_env(tmp_path), "PYTHONPATH": ROOT}, capture_output=True, text=True)
    assert r.returncode == 0 and "usage" in r.stdout
    assert not [f for f in os.listdir(work) if f.startswith("RAN_")]


def test_without_the_block_they_would_run(tmp_path):
    work = tmp_path / "work"
    _make_shadows(work)
    subprocess.run([sys.executable, "-c", "import shlex"], cwd=work, env=_env(tmp_path), capture_output=True)
    assert os.path.exists(work / "RAN_shlex")


def test_a_source_checkout_run_in_place_still_works(tmp_path):
    r = subprocess.run([sys.executable, "-m", "n3memorycore.n3memory", "--help"], cwd=ROOT,
                       env=_env(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0


def test_children_are_started_with_dash_P_outside_a_checkout(tmp_path, monkeypatch):
    from n3memorycore.paths import python_cmd
    monkeypatch.chdir(tmp_path)
    assert python_cmd() == ([sys.executable, "-P"] if sys.version_info >= (3, 11) else [sys.executable])
    monkeypatch.chdir(ROOT)
    assert python_cmd() == [sys.executable]


def test_no_child_is_started_without_python_cmd():
    for dirpath, _d, files in os.walk(PKG):
        for f in files:
            if f.endswith(".py"):
                assert "[sys.executable," not in open(os.path.join(dirpath, f), encoding="utf-8").read()
