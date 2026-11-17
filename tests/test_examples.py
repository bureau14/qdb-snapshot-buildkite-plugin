"""The files an adopting repository copies: the bootstrap and the shim.

The shim runs as a script against stand-ins for ``buildkite-agent`` and ``git``
put first on PATH; nothing real is called.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from snapdep.pipeline import BUILT, ENTER, FAIL, SEAL, plugin_name

EXAMPLES = Path(__file__).parent.parent / "examples"
COMMIT = "ab" * 20


def test_bootstrap_has_the_three_gate_jobs_in_one_group():
    doc = yaml.safe_load((EXAMPLES / "snapshot-bootstrap.yml").read_text())
    enter, fail, seal = doc["steps"]
    assert [s["key"] for s in (enter, fail, seal)] == [ENTER, FAIL, SEAL]
    for s in (enter, fail, seal):
        assert s["concurrency"] == 1
        assert s["concurrency_group"] == "snapdep/${BUILDKITE_PIPELINE_SLUG}/__COMMIT__"
        [plugin] = s["plugins"]
        [(source, config)] = plugin.items()
        assert plugin_name(source) == "bureau14/qdb-snapshot"
        assert source.endswith("#REPLACE-WITH-PLUGIN-COMMIT")
        assert config == {"step": s["key"].split("-")[1]}
    assert fail["depends_on"] == [
        {"step": ENTER, "allow_failure": True},
        {"step": BUILT, "allow_failure": True},
    ]
    assert seal["depends_on"] == [ENTER, BUILT]


def stand_in(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


@pytest.fixture
def checkout(tmp_path):
    bk = tmp_path / ".buildkite"
    bk.mkdir()
    shutil.copy(EXAMPLES / "pipeline-shim.py", bk / "pipeline.py")
    with open(bk / "pipeline.py", "a") as f:
        f.write('\nprint("the real pipeline")\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in(bin_dir / "buildkite-agent", f'echo "$@" >> "{tmp_path}/agent.log"')
    stand_in(bin_dir / "git", f"echo {COMMIT}")
    return tmp_path


def run_shim(checkout: Path, **env: str) -> str:
    e = {"PATH": f"{checkout / 'bin'}{os.pathsep}{os.environ['PATH']}", **env}
    p = subprocess.run(
        [sys.executable, str(checkout / ".buildkite" / "pipeline.py")],
        env=e,
        capture_output=True,
        text=True,
        check=True,
    )
    return p.stdout


def test_shim_prints_the_bootstrap_with_the_commit(checkout):
    shutil.copy(EXAMPLES / "snapshot-bootstrap.yml", checkout / ".buildkite")
    out = run_shim(checkout, BUILDKITE="true")
    assert "__COMMIT__" not in out and f"snapdep/${{BUILDKITE_PIPELINE_SLUG}}/{COMMIT}" in out
    assert "the real pipeline" not in out
    [line] = (checkout / "agent.log").read_text().splitlines()
    assert line.startswith("meta-data set snapdep-arrived ")
    assert float(line.split()[-1]) > 1.7e9


def test_shim_lets_the_generator_through(checkout):
    shutil.copy(EXAMPLES / "snapshot-bootstrap.yml", checkout / ".buildkite")
    assert run_shim(checkout, BUILDKITE="true", SNAPDEP_PHASE="generate") == "the real pipeline\n"
    assert run_shim(checkout) == "the real pipeline\n", "outside Buildkite"
    assert not (checkout / "agent.log").exists()


def test_shim_without_bootstrap_does_nothing(checkout):
    assert run_shim(checkout, BUILDKITE="true") == "the real pipeline\n"
