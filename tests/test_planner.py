"""The planner on graphs that mirror the real one.

qdb-api-python -> qdb-api-rest -> qdb-duck -> quasardb-build
qdb-api-python -> quasardb-build
qdb-api-rest   -> quasardb-build
"""

from __future__ import annotations

import hashlib

import pytest
import yaml

from snapdep.canonical import opaque_key, version_key
from snapdep.declaration import PATH, parse_declaration
from snapdep.fakes import FakeGit, MemoryStore
from snapdep.planner import PlanError, Root, dependencies_first, load_plan_for, make_plan
from snapdep.records import RecordError, create_or_load_plan

PYTHON, REST, DUCK, QDB = "qdb-api-python", "qdb-api-rest", "qdb-duck", "quasardb-build"
REPOS = {
    PYTHON: "bureau14/qdb-api-python",
    REST: "bureau14/qdb-api-rest",
    DUCK: "bureau14/qdb-duck",
    QDB: "bureau14/quasardb",
}
EDGES = {PYTHON: [REST, QDB], REST: [DUCK, QDB], DUCK: [QDB], QDB: []}
LINES = ["master", "3.14.x"]
T0 = 1_763_380_800.0


def sha(*parts: str) -> str:
    return hashlib.sha1("/".join(parts).encode()).hexdigest()


def declaration(
    project: str, edges=None, repos=None, pins=None, lines=None, extra: dict | None = None
) -> str:
    edges = EDGES if edges is None else edges
    repos = REPOS if repos is None else repos
    entry = {"depends": [{"project": d, "repo": repos[d]} for d in edges[project]]}
    doc = {
        "schema": 1,
        "lines": lines or LINES,
        "pipelines": {project: entry, **(extra or {})},
    }
    if pins:
        doc["pins"] = pins
    return yaml.safe_dump(doc)


def world(branches, decls=None, merge_base_times=None, edges=None):
    """A FakeGit where ``branches[project]`` lists the branches of its repository.

    Every branch head carries the project's declaration unless ``decls`` maps
    ``(project, branch)`` to other text (None: no file).
    """
    heads, files = {}, {}
    for project, bs in branches.items():
        for b in bs:
            c = sha(project, b)
            heads[(REPOS[project], b)] = c
            text = (decls or {}).get((project, b), declaration(project, edges=edges))
            if text is not None:
                files[(REPOS[project], c, PATH)] = text
    return FakeGit(heads, files, merge_base_times=merge_base_times or {})


def root(branch: str, project: str = PYTHON, commit: str | None = None) -> Root:
    return Root(
        project=project,
        repo=REPOS[project],
        branch=branch,
        commit=commit or sha(project, branch),
        build_id="root-uuid",
        build_number=512,
        build_url="https://ci/qdb-api-python/512",
    )


def plan_of(git, branch="master", store=None, pins=None, project=PYTHON, decl_text=None, **kw):
    text = decl_text or declaration(project, pins=pins)
    decl = parse_declaration(text)
    return make_plan(root(branch, project), decl, git, store or MemoryStore(), False, T0, **kw)


def refs(p):
    return {k: n.ref for k, n in p.nodes.items()}


def labels(p):
    return {k: n.label for k, n in p.nodes.items()}


ALL_MASTER = {PYTHON: ["master"], REST: ["master"], DUCK: ["master"], QDB: ["master"]}


def test_all_on_master_keys_bottom_up():
    g = world(ALL_MASTER)
    p = plan_of(g)
    k_qdb = version_key(QDB, sha(QDB, "master"), {})
    k_duck = version_key(DUCK, sha(DUCK, "master"), {QDB: k_qdb})
    k_rest = version_key(REST, sha(REST, "master"), {DUCK: k_duck, QDB: k_qdb})
    k_py = version_key(PYTHON, sha(PYTHON, "master"), {REST: k_rest, QDB: k_qdb})
    assert {k: n.key for k, n in p.nodes.items()} == {
        QDB: k_qdb,
        DUCK: k_duck,
        REST: k_rest,
        PYTHON: k_py,
    }
    assert set(refs(p).values()) == {"master"}
    assert set(labels(p).values()) == {"master"}
    assert p.nodes[PYTHON].deps == (REST, QDB)
    assert p.nodes[QDB].deps == ()
    assert not any(n.opaque for n in p.nodes.values())
    assert (p.id, p.context, p.rebuild, p.root.build_number) == ("root-uuid", "master", "none", 512)
    assert p.created_at == "2025-11-17T12:00:00.000Z"


def test_heads_are_asked_once_per_repository_and_ref():
    g = world(ALL_MASTER)
    plan_of(g)
    heads = [c for c in g.calls if c[0] == "head"]
    assert len(heads) == len(set(heads))
    assert ("head", REPOS[QDB], "master") in heads
    assert ("head", REPOS[PYTHON], "master") not in heads, "the root's commit is the checkout"
    assert not [c for c in g.calls if c[0] == "release_line"], "not needed on a line"


def test_feature_branch_in_some_repositories_only():
    branches = {
        PYTHON: ["master", "feature"],
        REST: ["master"],
        DUCK: ["master"],
        QDB: ["master", "feature"],
    }
    g = world(branches, merge_base_times={"master": 200, "3.14.x": 100})
    p = plan_of(g, "feature")
    assert refs(p) == {PYTHON: "feature", REST: "master", DUCK: "master", QDB: "feature"}
    assert p.nodes[QDB].commit == sha(QDB, "feature")
    assert labels(p) == {PYTHON: "feature", REST: "feature", DUCK: "feature", QDB: "feature"}


def test_feature_branch_only_in_the_middle_keeps_line_labels_below():
    branches = {
        PYTHON: ["master", "feature"],
        REST: ["master", "feature"],
        DUCK: ["master"],
        QDB: ["master"],
    }
    g = world(branches, merge_base_times={"master": 200})
    p = plan_of(g, "feature")
    assert refs(p) == {PYTHON: "feature", REST: "feature", DUCK: "master", QDB: "master"}
    assert labels(p) == {PYTHON: "feature", REST: "feature", DUCK: "master", QDB: "master"}


def test_branch_cut_from_3_14_x_falls_back_to_3_14_x():
    branches = {
        PYTHON: ["master", "3.14.x", "fix"],
        REST: ["master", "3.14.x"],
        DUCK: ["master", "3.14.x"],
        QDB: ["master", "3.14.x"],
    }
    g = world(branches, merge_base_times={"master": 100, "3.14.x": 200})
    p = plan_of(g, "fix")
    assert refs(p) == {PYTHON: "fix", REST: "3.14.x", DUCK: "3.14.x", QDB: "3.14.x"}
    assert labels(p) == {PYTHON: "fix", REST: "3.14.x", DUCK: "3.14.x", QDB: "3.14.x"}
    assert len([c for c in g.calls if c[0] == "release_line"]) == 1


def test_line_missing_in_a_repository_falls_back_to_master():
    branches = {
        PYTHON: ["3.14.x"],
        REST: ["3.14.x"],
        DUCK: ["master"],
        QDB: ["3.14.x"],
    }
    p = plan_of(world(branches), "3.14.x")
    assert refs(p) == {PYTHON: "3.14.x", REST: "3.14.x", DUCK: "master", QDB: "3.14.x"}
    assert labels(p) == {PYTHON: "3.14.x", REST: "3.14.x", DUCK: "master", QDB: "3.14.x"}


def test_pin_applies_to_the_whole_closure():
    branch = "3.14.x-20260821-qdb-3.14.2"
    branches = {
        PYTHON: ["3.14.x", branch],
        REST: ["3.14.x"],
        DUCK: ["3.14.x"],
        QDB: ["3.14.x", "3.14.2-test-runner"],
    }
    g = world(branches, merge_base_times={"3.14.x": 300, "master": 100})
    p = plan_of(g, branch, pins={QDB: "3.14.2-test-runner"})
    assert refs(p) == {PYTHON: branch, REST: "3.14.x", DUCK: "3.14.x", QDB: "3.14.2-test-runner"}
    assert p.nodes[QDB].commit == sha(QDB, "3.14.2-test-runner")
    assert set(labels(p).values()) == {branch}


def test_pin_to_a_missing_branch_fails():
    g = world(ALL_MASTER)
    with pytest.raises(PlanError, match="pins.quasardb-build: branch nope does not exist"):
        plan_of(g, pins={QDB: "nope"})


def test_opaque_node_without_declaration_file():
    g = world(ALL_MASTER, decls={(DUCK, "master"): None})
    pointers = {
        "qdb-duck/refs/heads/master/variants/linux-haswell-release/LATEST_SUCCESSFUL": b"b-1\n",
        "qdb-duck/refs/heads/master/variants/linux-core2-release/LATEST_SUCCESSFUL": b"b-2",
        "qdb-duck/refs/heads/master/variants/linux-core2-release/builds/b-2/manifest.json": b"{}",
        "qdb-duck/refs/heads/feature/variants/x/LATEST_SUCCESSFUL": b"b-9",
    }
    p = plan_of(g, store=MemoryStore(pointers))
    duck = p.nodes[DUCK]
    assert duck.opaque and duck.deps == ()
    assert duck.commit == sha(DUCK, "master") and duck.label == "master"
    assert duck.key == opaque_key({"linux-haswell-release": "b-1", "linux-core2-release": "b-2"})
    k_qdb = p.nodes[QDB].key
    assert p.nodes[REST].key == version_key(REST, sha(REST, "master"), {DUCK: duck.key, QDB: k_qdb})


def test_opaque_node_without_entry_on_a_feature_branch():
    other = declaration(DUCK, extra={"something-else": {}})
    other_doc = yaml.safe_load(other)
    del other_doc["pipelines"][DUCK]
    branches = {
        PYTHON: ["master", "feature"],
        REST: ["master"],
        DUCK: ["master", "feature"],
        QDB: ["master"],
    }
    g = world(branches, decls={(DUCK, "feature"): yaml.safe_dump(other_doc)})
    p = plan_of(g, "feature")
    assert p.nodes[DUCK].opaque and p.nodes[DUCK].ref == "feature"
    assert labels(p) == {PYTHON: "feature", REST: "feature", DUCK: "feature", QDB: "master"}


def test_project_declared_in_two_repositories_names_both_paths():
    repos = dict(REPOS, **{QDB: "bureau14/quasardb-fork"})
    rest_text = declaration(REST, repos=repos)
    g = world(ALL_MASTER, decls={(REST, "master"): rest_text})
    g.heads[("bureau14/quasardb-fork", "master")] = sha("fork")
    with pytest.raises(PlanError) as e:
        plan_of(g)
    msg = str(e.value)
    assert "quasardb-build is declared in two repositories" in msg
    assert "qdb-api-python -> quasardb-build" in msg
    assert "qdb-api-python -> qdb-api-rest -> quasardb-build" in msg


def test_cycle_back_to_the_root_is_an_error():
    edges = dict(EDGES, **{QDB: [PYTHON]})
    g = world(ALL_MASTER, edges=edges)
    with pytest.raises(PlanError, match="dependency cycle"):
        plan_of(g, decl_text=declaration(PYTHON, edges=edges))


def test_root_repository_on_the_context_resolves_to_the_root_commit():
    tests = "quasardb-api-tests"
    repos = dict(REPOS, **{tests: REPOS[QDB]})
    g = world(ALL_MASTER)
    text = yaml.safe_dump(
        {
            "schema": 1,
            "lines": LINES,
            "pipelines": {tests: {"depends": [{"project": QDB, "repo": repos[QDB]}]}},
        }
    )
    checkout = "f" * 40
    g.files[(REPOS[QDB], checkout, PATH)] = declaration(QDB)
    r = Root(tests, repos[tests], "master", checkout, "root-uuid", 1, "")
    p = make_plan(r, parse_declaration(text), g, MemoryStore(), False, T0)
    assert p.nodes[QDB].commit == checkout, "not the remote head of master"
    assert p.nodes[tests].deps == (QDB,)


def test_root_without_entry_is_not_planned():
    g = world(ALL_MASTER)
    text = declaration(REST)
    with pytest.raises(PlanError, match="pipelines.qdb-api-python: missing"):
        plan_of(g, decl_text=text)


def test_invalid_dependency_declaration_names_repository_and_commit():
    g = world(ALL_MASTER, decls={(DUCK, "master"): "schema: 1\nlines: [master]\nbogus: 1\n"})
    with pytest.raises(Exception, match=r"bureau14/qdb-duck@[0-9a-f]{12}:.buildkite/snapshot.yml"):
        plan_of(g)


def test_rebuild_all_is_recorded():
    g = world(ALL_MASTER)
    decl = parse_declaration(declaration(PYTHON))
    p = make_plan(root("master"), decl, g, MemoryStore(), True, T0)
    assert p.rebuild == "all"


def test_child_loads_the_plan_and_checks_its_commit():
    g = world(ALL_MASTER)
    s = MemoryStore()
    p = create_or_load_plan(s, plan_of(g))
    assert load_plan_for(s, "root-uuid", REST, sha(REST, "master")) == p
    with pytest.raises(PlanError, match="plans qdb-api-rest at"):
        load_plan_for(s, "root-uuid", REST, "0" * 40)
    with pytest.raises(PlanError, match="has no node"):
        load_plan_for(s, "root-uuid", "unknown", "0" * 40)
    with pytest.raises(RecordError):
        load_plan_for(s, "other-uuid", REST, sha(REST, "master"))


def test_dependencies_first():
    p = plan_of(world(ALL_MASTER))
    assert dependencies_first(p, REST) == [QDB, DUCK, REST]
    assert dependencies_first(p, PYTHON) == [QDB, DUCK, REST, PYTHON]
