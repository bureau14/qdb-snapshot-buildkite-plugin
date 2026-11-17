# qdb-snapshot-buildkite-plugin

A Buildkite plugin that builds a pipeline against one fixed snapshot of the
pipelines it depends on.

Each adopting repository declares the direct dependencies of its pipelines in
`.buildkite/snapshot.yml`. A build fixes one snapshot of its dependency closure
(a "plan"), gives every pipeline of the closure a version key (a hash of its
commit and the keys of its dependencies), and looks the keys up in the artifact
store. A dependency that has no build for its key is built first, through a
synchronous trigger step; one that has is reused. Artifacts are then downloaded
from exactly the builds of the plan.

The plugin stores its records next to the artifacts of the
[qdb-artifacts](https://github.com/bureau14/qdb-artifacts-buildkite-plugin)
plugin, with the same configuration and credentials.

## Layout

- `plugin.yml`, `hooks/`: the Buildkite plugin.
- `lib/snapdep/`: the Python package (Python 3.9 or later); `python -m snapdep`.
- `tests/`: pytest, without network.

## Development

```bash
python3 -m venv .env/
.env/bin/python3 -m pip install -r requirements-dev.txt
.env/bin/ruff check . && .env/bin/ruff format --check .
.env/bin/python3 -m pytest
```

Print the plan of a checkout without writing anything:

```bash
PYTHONPATH=/path/to/plugin/lib python3 -m snapdep plan --dry-run \
  --pipeline <pipeline slug> --branch <branch>
```
