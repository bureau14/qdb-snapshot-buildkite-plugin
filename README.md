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

## How a build runs

The upload job prints three gate jobs instead of the pipeline. They share the
concurrency group `snapdep/<pipeline>/<commit>`, so two builds of one commit
pass the gate one after the other.

- `snapshot` (enter): makes the plan (or loads the plan of the build that
  triggered this one) and annotates the build with it. When the key of this
  build already has a version, the build reuses it and ends. Otherwise the
  dependencies without a version are built first through trigger steps, and the
  real pipeline is uploaded with the build ids of the dependencies written into
  the download entries of qdb-artifacts (`build_id`, `git_ref`,
  `require_strict_ref_match: true`).
- `snapshot failed` (fail): when a step of the gate did not pass, records the
  failure of this build.
- `snapshot seal` (seal): when every step of the gate passed, records this build
  as the version of its key.

A dependency that failed fails the build that asked for it, with an annotation
that follows the failure down the chain to the job that failed and says what to
retry.

## Adopting a repository

1. Write `.buildkite/snapshot.yml`:

   ```yaml
   schema: 1
   lines: [master, 3.14.x] # release lines of this repository
   pipelines:
     qdb-api-rest: # one entry per pipeline slug built from this repository
       depends:
         - project: quasardb-build # pipeline slug of the dependency
           repo: bureau14/quasardb # its GitHub repository
         - project: qdb-duck
           repo: bureau14/qdb-duck
       gates: [] # optional: steps that must pass; default every command step
       produces: [] # optional: variants that must be in the store
       generate: "" # optional: command that prints the pipeline
   ```

   Declare every other pipeline the generated steps download from. Downloads
   from the pipeline's own project are never declared. The file has the same
   content on every release line.

2. Copy [`examples/snapshot-bootstrap.yml`](examples/snapshot-bootstrap.yml) to
   `.buildkite/snapshot-bootstrap.yml` and replace `REPLACE-WITH-PLUGIN-COMMIT`
   with the 40-hex commit of this repository to use. Every job of the plugin
   then uses that same commit: the generate step it uploads reuses the reference
   the job was called with.

3. Paste the shim of [`examples/pipeline-shim.py`](examples/pipeline-shim.py) at
   the top of `.buildkite/pipeline.py`, before any import that needs the tools
   submodule. The pipeline settings in Buildkite stay as they are.

4. Give every generated step a `timeout_in_minutes`. A step without one can hold
   the gate of its commit for as long as it hangs.

The default generator command is
`python3 -m venv .env && .env/bin/python3 -m pip install -q -r .buildkite/requirements.txt && SNAPDEP_PHASE=generate .env/bin/python3 .buildkite/pipeline.py`.
It runs in the checkout of the enter and generate jobs.

## Variables a person can set

| Variable / object                   | Effect                                                                                                                                 |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `SNAPDEP_REBUILD=1`                 | This build builds its key again even when it has a version, and overwrites the version. Its dependencies are reused.                   |
| `SNAPDEP_REBUILD=all`               | Every pipeline of the chain is built again, once: a version sealed after this build's plan was made counts as built.                   |
| `SNAPDEP_DISABLE=1`                 | Plain mode: the pipeline runs as it would without the plugin. Nothing is planned, reused or recorded.                                  |
| `_snapdep/v1/disabled` in the store | Plain mode for every build of every pipeline, as long as the object exists.                                                            |
| `ARTIFACTS_*`, `AWS_DEFAULT_REGION` | Store configuration and credentials, read exactly as the qdb-artifacts plugin reads them (environment first, then its SSM parameters). |

Set by the plugin, not by people: `SNAPDEP_PLAN` (the plan id, on builds started
by a trigger step) and `SNAPDEP_PHASE=generate` (tells `pipeline.py` to print
the real pipeline).

A build that runs only a part of its pipeline is not a version build: it runs
in plain mode, never reuses a version and is never recorded as one. That is the
case when a declared gate is not in the generated pipeline, when the generated
pipeline has no command step, and when the build selects a part of the pipeline
with `QDB_PIPELINE_FILTER` or a YAML mapping as build message (as
`qdb_pipeline.selection` reads them) and the generator, run again without that
selection, prints a different pipeline.

Build meta-data written by the jobs: `snapdep-arrived`, `snapdep-mode`
(`plain`, `reused` or `build`), `snapdep-key`, `snapdep-plan`,
`snapdep-gate-keys`, and `snapdep-uploaded-enter` and
`snapdep-uploaded-generate`: set once that job's documents are in the build, so
that a retry of the job does not upload them again.

## Layout

- `plugin.yml`, `hooks/command`: the Buildkite plugin. The hook creates a
  virtualenv inside the plugin directory and runs `python -m snapdep <step>`.
  Linux agents only.
- `lib/snapdep/`: the Python package (Python 3.9 or later); `python -m snapdep`.
- `examples/`: the bootstrap and the shim an adopting repository copies.
- `tests/`: pytest, without network. `tests/fixtures/` holds the shape of real
  generated pipelines (reduced by `tests/fixtures/sanitise.py`).

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
