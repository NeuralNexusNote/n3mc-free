"""Safe import path: files in the current folder do not run inside N3MC. No model needed."""
import ast
import os
import subprocess
import sys

import pytest

import n3memorycore
from n3memorycore.paths import python_cmd

PKG_DIR = os.path.dirname(os.path.abspath(n3memorycore.__file__))
REPO_ROOT = os.path.dirname(PKG_DIR)
SHADOW_NAMES = ["argparse", "shlex", "queue", "fastapi", "pydantic", "sqlite_vec", "numpy", "uvicorn"]


def _package_modules():
    out = []
    for dirpath, dirnames, filenames in os.walk(PKG_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _ends_with_main_guard(tree):
    last = tree.body[-1] if tree.body else None
    return (isinstance(last, ast.If) and isinstance(last.test, ast.Compare)
            and isinstance(last.test.left, ast.Name) and last.test.left.id == "__name__")


def _entry_modules():
    mods = []
    for path in _package_modules():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        if _ends_with_main_guard(tree):
            mods.append(path)
    return mods


def _child_env(tmp_path):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HOME"] = env["USERPROFILE"] = str(tmp_path / "home")
    env["N3MC_HOME"] = str(tmp_path / "n3mc")
    return env


@pytest.fixture
def shadow_folder(tmp_path):
    folder = tmp_path / "cwd"
    folder.mkdir()
    for name in SHADOW_NAMES:
        (folder / f"{name}.py").write_text(
            "import os\n"
            f"open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '{name}.ran'), 'w').close()\n"
            f"raise ImportError('shadow {name} ran')\n", encoding="utf-8")
    return folder


def test_there_are_entry_modules():
    mods = _entry_modules()
    assert os.path.join(PKG_DIR, "n3memory.py") in mods
    assert len(mods) >= 3


@pytest.mark.parametrize("path", _entry_modules(), ids=os.path.basename)
def test_entry_module_starts_with_the_safe_path_block(path):
    with open(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    body = tree.body
    assert isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)  # docstring
    first, second, third = body[1], body[2], body[3]
    assert isinstance(first, ast.Import) and first.names[0].name == "os" and first.names[0].asname == "_os"
    assert isinstance(second, ast.Import) and second.names[0].name == "sys" and second.names[0].asname == "_sys"
    assert isinstance(third, ast.If) and ast.unparse(third.test) == "__name__ == '__main__'"
    assert "del _sys.path[0]" in ast.get_source_segment(source, third)


def test_files_in_the_current_folder_do_not_run(shadow_folder, tmp_path):
    proc = subprocess.run([sys.executable, "-m", "n3memorycore.n3memory", "--help"], cwd=str(shadow_folder),
                          env=_child_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()
    assert not list(shadow_folder.glob("*.ran"))


def test_without_the_block_they_would_run(shadow_folder, tmp_path):
    subprocess.run([sys.executable, "-c", "import shlex"], cwd=str(shadow_folder), env=_child_env(tmp_path),
                   capture_output=True, text=True, timeout=60)
    assert (shadow_folder / "shlex.ran").exists()


def test_a_source_checkout_run_in_place_still_works(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "n3memorycore.n3memory", "--help"], cwd=REPO_ROOT,
                          env=_child_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def test_children_are_started_with_dash_P_outside_a_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = [sys.executable, "-P"] if sys.version_info >= (3, 11) else [sys.executable]
    assert python_cmd() == expected
    monkeypatch.chdir(REPO_ROOT)
    assert python_cmd() == [sys.executable]


def test_no_child_is_started_without_python_cmd():
    for path in _package_modules():
        with open(path, encoding="utf-8") as f:
            assert "[sys.executable," not in f.read(), path
