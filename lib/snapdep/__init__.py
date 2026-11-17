"""snapdep: snapshot dependencies between Buildkite pipelines.

Modules, in dependency order:

- ``canonical``    canonical JSON and the version keys
- ``declaration``  the ``.buildkite/snapshot.yml`` file of a repository
- ``git``          read-only git access (interface, real implementation)
- ``store``        object store access (interface, error policy, S3/R2 implementation)
- ``records``      the records under ``_snapdep/v1/`` and how they are read and written
- ``planner``      the plan of a root build, and loading it in a child build
- ``fakes``        in-memory git and store for tests
- ``cli``          ``python -m snapdep <command>``
"""

from __future__ import annotations

SCHEMA = 1
