"""hooks/command under bash: the choice of Python and the placement of the virtualenv.

The hook is copied into a temporary plugin directory and sourced, which defines
its functions without running ``main``; each test calls one function.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent / "hooks" / "command"


@pytest.fixture
def plugin(tmp_path):
    (tmp_path / "hooks").mkdir()
    shutil.copy(HOOK, tmp_path / "hooks" / "command")
    return tmp_path


def run_hook(plugin: Path, script: str, env: dict[str, str] | None = None):
    """Source the hook in ``plugin`` and run ``script`` after it."""
    hook = plugin / "hooks" / "command"
    return subprocess.run(
        ["bash", "-c", f'source "{hook}"; {script}'],
        env={"PATH": os.environ["PATH"], **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def stub(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def test_find_python3_checks_the_agent_python_version(plugin, tmp_path):
    old = stub(tmp_path / "python3.8", "exit 1")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    path = f"{bin_dir}:{os.environ['PATH']}"
    r = run_hook(plugin, "find_python3", {"QDB_CICD_AGENT_PYTHON3": str(old), "PATH": path})
    assert r.stdout.strip() == "python3", "a Python older than 3.9 is skipped"
    r = run_hook(plugin, "find_python3", {"QDB_CICD_AGENT_PYTHON3": sys.executable})
    assert r.stdout.strip() == sys.executable


def make_venv(d: Path, marker: bool) -> Path:
    d.mkdir()
    if marker:
        (d / ".snapdep-ready").touch()
    return d


def test_place_venv_moves_a_complete_virtualenv_in(plugin):
    tmp = make_venv(plugin / ".venv-a", True)
    r = run_hook(plugin, f'place_venv "{sys.executable}" "{tmp}"')
    assert r.returncode == 0, r.stdout + r.stderr
    assert (plugin / ".venv" / ".snapdep-ready").exists() and not tmp.exists()


def test_place_venv_keeps_the_virtualenv_another_job_placed(plugin):
    other = make_venv(plugin / ".venv", True)
    (other / "from-other-job").touch()
    tmp = make_venv(plugin / ".venv-a", True)
    r = run_hook(plugin, f'place_venv "{sys.executable}" "{tmp}"')
    assert r.returncode == 0, r.stdout + r.stderr
    assert (other / "from-other-job").exists() and not tmp.exists()


def test_ensure_venv_never_removes_an_existing_directory(plugin):
    other = make_venv(plugin / ".venv", False)
    (other / "being-moved-in").touch()
    build = 'build_venv() { mkdir -p "$2/bin"; touch "$2/.snapdep-ready"; }'
    r = run_hook(plugin, f'{build}; ensure_venv "{sys.executable}"')
    assert r.returncode == 1
    assert "is not a complete virtualenv" in r.stdout
    assert (other / "being-moved-in").exists()
    assert not list(plugin.glob(".venv-*")), "the job's own directory is removed"
