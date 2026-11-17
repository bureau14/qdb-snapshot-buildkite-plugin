"""Plans, records and builds for the tests of the jobs.

The graph mirrors the real one (a diamond)::

    qdb-api-python -> qdb-api-rest -> qdb-duck -> quasardb-build
    qdb-api-python -> quasardb-build
    qdb-api-rest   -> quasardb-build
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from snapdep.records import (
    REBUILD_NONE,
    BuildRef,
    FailedStep,
    Node,
    Plan,
    RootInfo,
    Version,
    encode,
    failure_record,
    iso_utc,
    plan_path,
    plan_to_dict,
    version_path,
    version_record,
    version_to_dict,
)

PYTHON, REST, DUCK, QDB = "qdb-api-python", "qdb-api-rest", "qdb-duck", "quasardb-build"
REPOS = {
    PYTHON: "bureau14/qdb-api-python",
    REST: "bureau14/qdb-api-rest",
    DUCK: "bureau14/qdb-duck",
    QDB: "bureau14/quasardb",
}
EDGES = {PYTHON: (REST, QDB), REST: (DUCK, QDB), DUCK: (QDB,), QDB: ()}
PLAN_ID = "01aaaaaa-root-build"
OTHER_PLAN_ID = "01bbbbbb-other-root-build"  # the plan of another chain
T0 = 1_763_380_800.0
PLUGIN_COMMIT = "c0ffee" + "0" * 34
PLUGINS = json.dumps(
    [
        {"github.com/bureau14/qdb-artifacts-buildkite-plugin#master": {"download": {}}},
        {f"github.com/bureau14/qdb-snapshot-buildkite-plugin#{PLUGIN_COMMIT}": {"step": "enter"}},
    ]
)


def sha(project: str) -> str:
    return hashlib.sha1(project.encode()).hexdigest()


def key(project: str) -> str:
    return "v1-" + hashlib.sha1(("key/" + project).encode()).hexdigest()[:32]


def plan(
    rebuild: str = REBUILD_NONE,
    created: float = T0,
    opaque: Iterable[str] = (),
    label: str = "master",
) -> Plan:
    """The diamond as planned by a root build of qdb-api-python on ``label``."""
    op = set(opaque)
    nodes = {
        p: Node(
            repo=REPOS[p],
            ref="master",
            commit=sha(p),
            key=("opaque-" + "0" * 32) if p in op else key(p),
            label=label,
            opaque=p in op,
            deps=() if p in op else EDGES[p],
        )
        for p in EDGES
    }
    return Plan(
        id=PLAN_ID,
        root=RootInfo(PYTHON, 512, "https://ci/qdb-api-python/512", label),
        context=label,
        rebuild=rebuild,
        created_at=iso_utc(created),
        nodes=nodes,
    )


def build(project: str, number: int, uuid: str | None = None) -> BuildRef:
    return BuildRef(
        uuid or f"uuid-{project}-{number}", number, f"https://ci/{project}/{number}", project
    )


def version(p: Plan, project: str, number: int, sealed: float = T0 + 10) -> Version:
    """A version of ``project`` as planned in ``p``, built by build ``number``."""
    n = p.nodes[project]
    deps = {d: p.nodes[d].key for d in n.deps}
    return version_record(project, n, deps, build(project, number), [], sealed)


def objects(p: Plan, versions: Iterable[Version] = ()) -> Mapping[str, bytes]:
    """Store objects holding plan ``p`` and ``versions``."""
    out = {plan_path(p.id): encode(plan_to_dict(p))}
    for v in versions:
        out[version_path(v.project, v.key)] = encode(version_to_dict(v))
    return out


def failed(
    project: str,
    k: str,
    b: BuildRef,
    steps: Mapping[str, str],
    causes=(),
    at=T0 + 20,
    plan_id: str | None = OTHER_PLAN_ID,
):
    """A failure of build ``b``; by default under the plan of another chain."""
    return failure_record(
        project, k, plan_id, b, [FailedStep(s, o) for s, o in steps.items()], list(causes), at
    )
