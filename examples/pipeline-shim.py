"""Snapshot shim for ``.buildkite/pipeline.py`` of an adopting repository.

Paste the code below at the very top of ``pipeline.py``, before any import
that needs the tools submodule. In the build's upload job it records when the
build arrived (meta-data ``snapdep-arrived``) and prints the gate jobs of
``.buildkite/snapshot-bootstrap.yml`` instead of the pipeline. The plugin's
enter job runs ``pipeline.py`` again with ``SNAPDEP_PHASE=generate`` and gets
the real pipeline.

Outside a Buildkite job (``BUILDKITE`` is not ``true``), and when the
bootstrap file does not exist, the shim does nothing.
"""

# --- qdb-snapshot shim: begin ------------------------------------------------
import os
import subprocess
import sys
import time

_SNAPDEP_BOOTSTRAP = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "snapshot-bootstrap.yml"
)

if (
    os.environ.get("SNAPDEP_PHASE") != "generate"
    and os.environ.get("BUILDKITE") == "true"
    and os.path.exists(_SNAPDEP_BOOTSTRAP)
):
    subprocess.run(
        ["buildkite-agent", "meta-data", "set", "snapdep-arrived", f"{time.time():.3f}"],
        check=True,
    )
    _snapdep_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=os.path.dirname(_SNAPDEP_BOOTSTRAP),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with open(_SNAPDEP_BOOTSTRAP, encoding="utf-8") as _snapdep_file:
        sys.stdout.write(_snapdep_file.read().replace("__COMMIT__", _snapdep_commit))
    sys.exit(0)
# --- qdb-snapshot shim: end --------------------------------------------------
