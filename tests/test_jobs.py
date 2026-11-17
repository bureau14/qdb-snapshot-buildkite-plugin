"""Every branch of enter, generate, fail and seal, with fakes.

Most builds here are qdb-api-rest builds started by a trigger step of a
qdb-api-python root (``SNAPDEP_PLAN`` set, the plan in the store). Its
generated pipeline is the qdb-api-rest fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from plans import (
    DUCK,
    PLAN_ID,
    PLUGIN_COMMIT,
    PLUGINS,
    PYTHON,
    QDB,
    REPOS,
    REST,
    T0,
    build,
    failed,
    key,
    objects,
    plan,
    sha,
    version,
)

from snapdep.agent import AgentError
from snapdep.declaration import PATH, parse_declaration
from snapdep.fakes import FakeGit, FixedGenerator, MemoryStore, RecordingAgent
from snapdep.jobs import (
    ARRIVED,
    GATE_KEYS,
    KEY,
    MODE,
    PLAN,
    Effects,
    JobError,
    counts_as_built,
    enter,
    fail,
    generate,
    job_from_env,
    seal,
    selection_requested,
    shared_failure,
)
from snapdep.pipeline import BUILT
from snapdep.planner import PlanError
from snapdep.records import (
    DISABLED_PATH,
    REBUILD_ALL,
    decode,
    failure_from_dict,
    failure_path,
    find_version,
    holder_path,
    list_failures,
    load_plan,
    version_path,
    write_failure,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "qdb-api-rest.yml").read_text()
REST_GATES = ["lint", "build-linux-haswell", "build-freebsd-haswell", "full-test-report"]
NOW = T0 + 100
ARRIVED_AT = T0 + 50
REST_BUILD = build(REST, 77, "uuid-rest-77")


def declaration(gates=(), produces=(), slug=REST, deps=(DUCK, QDB)):
    entry = {"depends": [{"project": d, "repo": REPOS[d]} for d in deps]}
    if gates:
        entry["gates"] = list(gates)
    if produces:
        entry["produces"] = list(produces)
    return parse_declaration(
        yaml.safe_dump({"schema": 1, "lines": ["master"], "pipelines": {slug: entry}})
    )


def environment(**extra):
    env = {
        "BUILDKITE_PIPELINE_SLUG": REST,
        "BUILDKITE_BRANCH": "master",
        "BUILDKITE_REPO": "git@github.com:bureau14/qdb-api-rest.git",
        "BUILDKITE_BUILD_ID": REST_BUILD.uuid,
        "BUILDKITE_BUILD_NUMBER": str(REST_BUILD.number),
        "BUILDKITE_BUILD_URL": REST_BUILD.url,
        "BUILDKITE_PLUGINS": PLUGINS,
        "SNAPDEP_PLAN": PLAN_ID,
    }
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


class World:
    """A build of qdb-api-rest: store, agent, git, generator and a fixed clock."""

    def __init__(self, store=None, meta=None, outcomes=None, output=FIXTURE, checkout=None):
        self.store = store if store is not None else MemoryStore(objects(plan()))
        self.agent = RecordingAgent({ARRIVED: str(ARRIVED_AT), **(meta or {})}, outcomes)
        self.git = FakeGit({}, checkout=checkout or sha(REST))
        self.generator = FixedGenerator(output)
        self.logs: list[str] = []
        self.fx = Effects(
            self.store, self.agent, self.git, self.generator, lambda: NOW, self.logs.append
        )

    def uploaded(self, i):
        return yaml.safe_load(self.agent.uploads[i])

    def step_keys(self, i):
        return [s.get("key") for s in self.uploaded(i)["steps"]]


def run(job_fn, w, decl=None, **env):
    d = decl or declaration()
    return job_fn(job_from_env(environment(**env)), lambda: d, w.fx)


def stored_versions(*vs):
    return MemoryStore(objects(plan(), vs))


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def test_counts_as_built():
    p, pa = plan(), plan(rebuild=REBUILD_ALL)
    v_old, v_new = version(p, QDB, 1, sealed=T0 - 5), version(p, QDB, 2, sealed=T0 + 5)
    assert counts_as_built(v_old, p, False)
    assert not counts_as_built(None, p, False)
    assert not counts_as_built(v_old, p, True)
    assert not counts_as_built(v_old, pa, False), "rebuild all: sealed before the plan"
    assert counts_as_built(v_new, pa, False), "rebuild all: built in this chain"


def test_shared_failure_is_the_latest_other_failure_after_arrival():
    early = failed(REST, key(REST), build(REST, 1), {"x": "hard_failed"}, at=T0)
    late = failed(REST, key(REST), build(REST, 2), {"x": "hard_failed"}, at=T0 + 60)
    own = failed(REST, key(REST), REST_BUILD, {"x": "hard_failed"}, at=T0 + 70)
    assert shared_failure([early, late, own], T0 + 50, REST_BUILD.uuid) == late
    assert shared_failure([early], T0 + 50, REST_BUILD.uuid) is None
    assert shared_failure([late], None, REST_BUILD.uuid) is None


def test_shared_failure_ignores_a_build_that_only_soft_failed():
    soft = failed(REST, key(REST), build(REST, 2), {"x": "soft_failed"}, at=T0 + 60)
    mixed = failed(REST, key(REST), build(REST, 3), {"x": "soft_failed", "y": ""}, at=T0 + 61)
    assert shared_failure([soft], T0 + 50, REST_BUILD.uuid) is None
    assert shared_failure([soft, mixed], T0 + 50, REST_BUILD.uuid) == mixed


def test_rebuild_setting_rejects_unknown_values():
    with pytest.raises(JobError, match="SNAPDEP_REBUILD"):
        job_from_env(environment(SNAPDEP_REBUILD="yes"))
    assert job_from_env(environment(SNAPDEP_REBUILD="0")).rebuild == ""


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, False),
        ({"QDB_PIPELINE_FILTER": "os: linux"}, True),
        ({"QDB_PIPELINE_FILTER": " ", "BUILDKITE_MESSAGE": "{os: linux}"}, False),
        ({"BUILDKITE_MESSAGE": "{os: linux}"}, True),
        ({"BUILDKITE_MESSAGE": "QDB-1 - fix the parser (#12)"}, False),
        ({"BUILDKITE_MESSAGE": "a: [unclosed"}, False),
    ],
)
def test_selection_requested_follows_the_pipeline_selection_rule(env, expected):
    assert selection_requested(env) is expected


def test_job_needs_the_buildkite_environment():
    with pytest.raises(JobError, match="BUILDKITE_BUILD_ID"):
        job_from_env({"BUILDKITE_PIPELINE_SLUG": REST, "BUILDKITE_BRANCH": "master"})


# ---------------------------------------------------------------------------
# enter: plain mode
# ---------------------------------------------------------------------------


def assert_plain(w):
    assert w.agent.meta_data[MODE] == "plain"
    assert w.agent.uploads[0] == FIXTURE, "the generated pipeline, unchanged"
    assert w.uploaded(1) == {
        "steps": [
            {
                "label": ":camera_with_flash: snapshot built",
                "key": "snapdep-built",
                "command": "true",
                "retry": {"automatic": [{"exit_status": "*", "limit": 2}]},
            }
        ]
    }
    assert len(w.agent.uploads) == 2
    assert KEY not in w.agent.meta_data


def test_enter_plain_by_variable_does_not_touch_the_store():
    w = World(store=MemoryStore(unreadable=[DISABLED_PATH]))
    assert run(enter, w, SNAPDEP_DISABLE="1") == 0
    assert_plain(w)


def test_enter_plain_by_the_disabled_object():
    w = World(store=MemoryStore({**objects(plan()), DISABLED_PATH: b""}))
    assert run(enter, w) == 0
    assert_plain(w)
    assert "disabled in the store" in w.logs[0]


def test_enter_plain_when_the_pipeline_has_no_entry():
    w = World()
    assert run(enter, w, declaration(slug="other", deps=())) == 0
    assert_plain(w)
    assert w.generator.commands[0].endswith(
        "SNAPDEP_PHASE=generate .env/bin/python3 .buildkite/pipeline.py"
    )


def test_enter_plain_without_a_declaration():
    w = World()
    assert enter(job_from_env(environment()), lambda: None, w.fx) == 0
    assert_plain(w)


def test_enter_not_a_version_build_when_a_gate_is_missing():
    w = World()
    assert run(enter, w, declaration(gates=["lint", "build-windows-haswell"])) == 0
    assert_plain(w)
    assert "not a version build: no step build-windows-haswell" in w.logs[0]
    assert w.generator.commands and len(w.generator.commands) == 1


FULL = FIXTURE + "- label: windows\n  key: build-windows-haswell\n  command: make\n"


@pytest.mark.parametrize(
    "selection",
    [{"QDB_PIPELINE_FILTER": "os: linux"}, {"BUILDKITE_MESSAGE": "{os: linux}"}],
)
def test_enter_not_a_version_build_when_the_build_selects_a_part(selection):
    p = plan()
    w = World(store=stored_versions(version(p, REST, 70), version(p, DUCK, 5), version(p, QDB, 9)))
    w.generator.unfiltered = FULL
    assert run(enter, w, **selection) == 0
    assert_plain(w)
    assert "selects a part of the pipeline" in w.logs[0]
    assert len(w.generator.commands) == 2


def test_enter_builds_when_the_selection_changes_nothing():
    p = plan()
    w = World(store=stored_versions(version(p, DUCK, 5), version(p, QDB, 9)))
    assert run(enter, w, BUILDKITE_MESSAGE="fix(rest): handle empty bodies") == 0
    assert w.agent.meta_data[MODE] == "build"
    assert len(w.generator.commands) == 2, "a mapping message is checked against no selection"


def test_enter_runs_the_generator_once_without_a_selection():
    p = plan()
    w = World(store=stored_versions(version(p, DUCK, 5), version(p, QDB, 9)))
    w.generator.unfiltered = FULL
    assert run(enter, w, BUILDKITE_MESSAGE="snapshot dependency of x build 3") == 0
    assert w.agent.meta_data[MODE] == "build"
    assert len(w.generator.commands) == 1


def test_enter_not_a_version_build_without_a_command_step():
    w = World(output="steps:\n  - wait\n  - trigger: other\n")
    assert run(enter, w) == 0
    assert w.agent.meta_data[MODE] == "plain"
    assert "no command step that can gate a version" in w.logs[0]


def test_seal_refuses_an_empty_gate_list():
    w = World(meta={MODE: "build", KEY: key(REST), PLAN: PLAN_ID, GATE_KEYS: "[]"})
    with pytest.raises(JobError, match="without gates is not a version"):
        run(seal, w)
    assert find_version(w.store, REST, key(REST)) is None


# ---------------------------------------------------------------------------
# enter: reuse and shared failure
# ---------------------------------------------------------------------------


def test_enter_reuses_an_existing_version():
    p = plan()
    w = World(store=stored_versions(version(p, REST, 70), version(p, DUCK, 5), version(p, QDB, 9)))
    assert run(enter, w) == 0
    assert w.agent.meta_data[MODE] == "reused"
    assert w.step_keys(0) == ["snapdep-built"] and len(w.agent.uploads) == 1
    plan_note, reuse_note = w.agent.annotations
    assert plan_note[2] == "snapdep-plan" and "reuse [build 70]" in plan_note[0]
    assert reuse_note[2] == "snapdep" and "(https://ci/qdb-api-rest/70)" in reuse_note[0]
    assert not w.store.list_keys("_snapdep/v1/holders/")


def test_enter_fails_with_the_failure_of_the_build_it_waited_behind():
    other = build(REST, 76, "uuid-rest-76")
    f = failed(REST, key(REST), other, {"build-linux-haswell": "hard_failed"}, at=ARRIVED_AT + 6)
    w = World()
    write_failure(w.store, f)
    assert run(enter, w) == 1
    body, style, context = w.agent.annotations[-1]
    assert (style, context) == ("error", "snapdep")
    assert "failed in build 76 while this build waited for it" in body
    assert "step build-linux-haswell: hard_failed" in body
    assert MODE not in w.agent.meta_data
    assert not w.agent.uploads


def test_enter_ignores_a_failure_from_before_it_arrived():
    other = build(REST, 76, "uuid-rest-76")
    p = plan()
    w = World(store=stored_versions(version(p, DUCK, 5), version(p, QDB, 9)))
    write_failure(w.store, failed(REST, key(REST), other, {"x": "hard_failed"}, at=ARRIVED_AT - 1))
    assert run(enter, w) == 0
    assert w.agent.meta_data[MODE] == "build"


def test_enter_builds_when_a_failure_is_within_the_clock_margin_of_arrival():
    other = build(REST, 76, "uuid-rest-76")
    p = plan()
    w = World(store=stored_versions(version(p, DUCK, 5), version(p, QDB, 9)))
    write_failure(w.store, failed(REST, key(REST), other, {"x": "hard_failed"}, at=ARRIVED_AT + 2))
    assert run(enter, w) == 0
    assert w.agent.meta_data[MODE] == "build"
    assert any("within the 5 s clock margin" in m for m in w.logs)


def test_enter_without_arrival_time_builds_and_says_so():
    p = plan()
    w = World(store=stored_versions(version(p, DUCK, 5), version(p, QDB, 9)), meta={ARRIVED: ""})
    write_failure(w.store, failed(REST, key(REST), build(REST, 1), {"x": "hard_failed"}))
    assert run(enter, w) == 0
    assert any("snapdep-arrived is not set" in m for m in w.logs)


# ---------------------------------------------------------------------------
# enter: build
# ---------------------------------------------------------------------------


def assert_build_mode(w):
    assert w.agent.meta_data[MODE] == "build"
    assert w.agent.meta_data[KEY] == key(REST)
    assert w.agent.meta_data[PLAN] == PLAN_ID
    assert w.store.get(holder_path(REST, key(REST), REST_BUILD.uuid)) is not None


def assert_real_pipeline(w, pins):
    doc = w.uploaded(0)
    assert len(w.agent.uploads) == 1
    assert doc["steps"][-1] == {
        "label": ":camera_with_flash: snapshot built",
        "key": "snapdep-built",
        "command": "true",
        "depends_on": REST_GATES,
        "retry": {"automatic": [{"exit_status": "*", "limit": 2}]},
    }
    assert json.loads(w.agent.meta_data[GATE_KEYS]) == REST_GATES
    for s in doc["steps"]:
        for p in s.get("plugins", []):
            for name, config in p.items():
                if name.startswith("bureau14/qdb-artifacts#"):
                    for e in config.get("download", {}).get("projects", []):
                        b = pins[e["project_id"]]
                        assert (e["build_id"], e["git_ref"]) == (b.uuid, "refs/heads/master")
                        assert e["require_strict_ref_match"] is True


def test_enter_with_nothing_stale_uploads_the_real_pipeline():
    p = plan()
    vd, vq = version(p, DUCK, 5), version(p, QDB, 9)
    w = World(store=stored_versions(vd, vq))
    assert run(enter, w) == 0
    assert_build_mode(w)
    assert_real_pipeline(w, {DUCK: vd.build, QDB: vq.build})


def test_enter_with_stale_dependencies_uploads_triggers_and_generate():
    w = World()
    assert run(enter, w) == 0
    assert_build_mode(w)
    doc = w.uploaded(0)
    assert [s["key"] for s in doc["steps"]] == [
        "snapdep-dep-quasardb-build",
        "snapdep-dep-qdb-duck",
        "snapdep-generate",
    ]
    qdb, duck, gen = doc["steps"]
    assert "depends_on" not in qdb
    assert duck["depends_on"] == ["snapdep-dep-quasardb-build"]
    assert gen["depends_on"] == [
        {"step": "snapdep-dep-quasardb-build", "allow_failure": True},
        {"step": "snapdep-dep-qdb-duck", "allow_failure": True},
    ]
    assert gen["plugins"] == [{f"bureau14/qdb-snapshot#{PLUGIN_COMMIT}": {"step": "generate"}}]
    assert duck["build"]["env"] == {"SNAPDEP_PLAN": PLAN_ID}
    assert GATE_KEYS not in w.agent.meta_data
    plan_note = w.agent.annotations[0][0]
    assert "build through step `snapdep-dep-qdb-duck`" in plan_note
    assert "| qdb-api-rest | `master` |" in plan_note and "build in this build" in plan_note


def test_enter_with_one_stale_dependency_triggers_only_that_one():
    p = plan()
    w = World(store=stored_versions(version(p, QDB, 9)))
    assert run(enter, w) == 0
    assert w.step_keys(0) == ["snapdep-dep-qdb-duck", "snapdep-generate"]
    assert "depends_on" not in w.uploaded(0)["steps"][0]


def test_enter_opaque_dependencies_are_never_stale_nor_pinned():
    p = plan(opaque=[QDB])
    vd = version(p, DUCK, 5)
    w = World(store=MemoryStore(objects(p, [vd])))
    assert run(enter, w) == 0
    doc = w.uploaded(0)
    qdb_entries = [
        e
        for s in doc["steps"]
        for pl in s.get("plugins", [])
        for name, c in pl.items()
        if name.startswith("bureau14/qdb-artifacts#")
        for e in c.get("download", {}).get("projects", [])
        if e["project_id"] == QDB
    ]
    assert qdb_entries and all("build_id" not in e for e in qdb_entries)


def test_enter_forced_by_rebuild_1_builds_although_a_version_exists():
    p = plan()
    vd, vq = version(p, DUCK, 5), version(p, QDB, 9)
    w = World(store=stored_versions(version(p, REST, 70), vd, vq))
    assert run(enter, w, SNAPDEP_REBUILD="1") == 0
    assert_build_mode(w)
    assert_real_pipeline(w, {DUCK: vd.build, QDB: vq.build})


def test_enter_rebuild_all_in_the_plan_rebuilds_what_was_sealed_before_it():
    p = plan(rebuild=REBUILD_ALL)
    old_rest = version(p, REST, 70, sealed=T0 - 100)
    old_duck = version(p, DUCK, 5, sealed=T0 - 100)
    new_qdb = version(p, QDB, 9, sealed=T0 + 30)
    w = World(store=MemoryStore(objects(p, [old_rest, old_duck, new_qdb])))
    assert run(enter, w) == 0
    assert_build_mode(w)
    assert w.step_keys(0) == ["snapdep-dep-qdb-duck", "snapdep-generate"]


def test_enter_rebuild_all_reuses_what_this_chain_built():
    p = plan(rebuild=REBUILD_ALL)
    w = World(store=MemoryStore(objects(p, [version(p, REST, 70, sealed=T0 + 30)])))
    assert run(enter, w) == 0
    assert w.agent.meta_data[MODE] == "reused"


def test_enter_refuses_a_plan_for_another_commit():
    w = World(checkout="f" * 40)
    with pytest.raises(PlanError, match="plans qdb-api-rest at"):
        run(enter, w)


# ---------------------------------------------------------------------------
# enter: a root build makes and stores the plan
# ---------------------------------------------------------------------------


def root_world(rebuild: str | None = None):
    def decl(project):
        return yaml.safe_dump(
            {
                "schema": 1,
                "lines": ["master"],
                "pipelines": {
                    project: {"depends": [{"project": d, "repo": REPOS[d]} for d in deps[project]]}
                },
            }
        )

    deps = {REST: [DUCK, QDB], DUCK: [QDB], QDB: []}
    heads = {(REPOS[p], "master"): sha(p) for p in (DUCK, QDB)}
    files = {(REPOS[p], sha(p), PATH): decl(p) for p in (DUCK, QDB)}
    w = World(store=MemoryStore())
    w.git = FakeGit(heads, files, checkout=sha(REST))
    w.fx = Effects(w.store, w.agent, w.git, w.generator, lambda: NOW, w.logs.append)
    return w


def test_enter_root_makes_the_plan_once_and_triggers():
    w = root_world()
    assert run(enter, w, SNAPDEP_PLAN=None) == 0
    stored = load_plan(w.store, REST_BUILD.uuid)
    assert stored.root.project == REST and stored.rebuild == "none"
    assert w.agent.meta_data[PLAN] == REST_BUILD.uuid
    assert w.step_keys(0) == [
        "snapdep-dep-quasardb-build",
        "snapdep-dep-qdb-duck",
        "snapdep-generate",
    ]
    trigger = w.uploaded(0)["steps"][0]
    assert trigger["build"]["env"] == {"SNAPDEP_PLAN": REST_BUILD.uuid}
    assert trigger["build"]["message"] == "snapshot dependency of qdb-api-rest build 77"


def test_enter_sets_build_mode_after_key_and_plan():
    p = plan()
    w = World(store=stored_versions(version(p, DUCK, 5), version(p, QDB, 9)))
    assert run(enter, w) == 0
    written = list(w.agent.meta_data)
    assert written.index(KEY) < written.index(MODE) and written.index(PLAN) < written.index(MODE)


def test_retried_root_enter_loads_the_stored_plan_without_resolving():
    w = root_world()
    assert run(enter, w, SNAPDEP_PLAN=None) == 0
    first = load_plan(w.store, REST_BUILD.uuid)
    retry = root_world()
    retry.store.objects = dict(w.store.objects)
    retry.git.heads = {}  # the heads moved away or GitHub cannot be reached
    assert run(enter, retry, SNAPDEP_PLAN=None, BUILDKITE_RETRY_COUNT="1") == 0
    assert not [c for c in retry.git.calls if c[0] == "head"]
    assert load_plan(retry.store, REST_BUILD.uuid) == first
    assert retry.step_keys(0) == w.step_keys(0)


class RejectingAgent(RecordingAgent):
    """An agent whose build has the step keys of an earlier upload already."""

    def pipeline_upload(self, document):
        raise AgentError(
            "buildkite-agent pipeline upload failed (1): pipeline upload rejected: The key "
            '"snapdep-built" has already been used by another step in this build'
        )


def test_enter_does_not_upload_again_after_an_earlier_try_did():
    p = plan()
    w = World(store=stored_versions(version(p, DUCK, 5), version(p, QDB, 9)))
    w.agent.meta_data["snapdep-uploaded-enter"] = "1"
    assert run(enter, w, BUILDKITE_RETRY_COUNT="1") == 0
    assert not w.agent.uploads and not w.generator.commands


def test_generate_does_not_upload_again_after_an_earlier_try_did():
    w = World(meta={MODE: "build", KEY: key(REST), PLAN: PLAN_ID, "snapdep-uploaded-generate": "1"})
    assert run(generate, w) == 0
    assert not w.agent.uploads


def test_retried_enter_takes_a_duplicate_key_rejection_as_uploaded():
    p = plan()
    w = World(store=stored_versions(version(p, DUCK, 5), version(p, QDB, 9)))
    w.agent = RejectingAgent(w.agent.meta_data)
    w.fx = Effects(w.store, w.agent, w.git, w.generator, lambda: NOW, w.logs.append)
    assert run(enter, w, BUILDKITE_RETRY_COUNT="1") == 0
    assert w.agent.meta_data["snapdep-uploaded-enter"] == "1"
    assert any("uploaded by an earlier try of enter" in m for m in w.logs)
    w.agent.meta_data.pop("snapdep-uploaded-enter")
    with pytest.raises(AgentError, match="already been used"):
        run(enter, w)


def test_enter_root_with_rebuild_all_plans_a_rebuild_of_the_chain():
    w = root_world()
    assert run(enter, w, SNAPDEP_PLAN=None, SNAPDEP_REBUILD="all") == 0
    assert load_plan(w.store, REST_BUILD.uuid).rebuild == REBUILD_ALL


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

BUILD_META = {MODE: "build", KEY: key(REST), PLAN: PLAN_ID}


def test_generate_injects_and_uploads_with_the_sentinel():
    p = plan()
    vd, vq = version(p, DUCK, 5), version(p, QDB, 9)
    w = World(store=stored_versions(vd, vq), meta=BUILD_META)
    assert run(generate, w) == 0
    assert_real_pipeline(w, {DUCK: vd.build, QDB: vq.build})


def test_generate_fails_when_a_dependency_has_no_version():
    p = plan()
    vq = version(p, QDB, 9)
    w = World(store=stored_versions(vq), meta=BUILD_META)
    duck_build = build(DUCK, 31)
    write_failure(
        w.store,
        failed(DUCK, key(DUCK), duck_build, {"build-linux-haswell-release": "hard_failed"}),
    )
    assert run(generate, w) == 1
    f = failure_from_dict(
        decode(w.store.get(failure_path(REST, key(REST), REST_BUILD.uuid)).body, "f")
    )
    assert [(c.project, c.key) for c in f.caused_by] == [(DUCK, key(DUCK))]
    assert f.failed_steps == () and f.plan == PLAN_ID
    body, style, context = w.agent.annotations[-1]
    assert (style, context) == ("error", "snapdep")
    assert "FAILED in build 31: https://ci/qdb-duck/31" in body
    assert not w.agent.uploads and not w.generator.commands


def test_generate_under_rebuild_all_does_not_take_a_version_from_before_the_plan():
    p = plan(rebuild=REBUILD_ALL)
    store = MemoryStore(
        objects(p, [version(p, DUCK, 5, sealed=T0 - 1), version(p, QDB, 9, sealed=T0 + 1)])
    )
    w = World(store=store, meta=BUILD_META)
    assert run(generate, w) == 1
    assert [c.project for c in list_failures(w.store, REST, key(REST))[0].caused_by] == [DUCK]


def test_generate_needs_build_mode_meta_data():
    w = World(meta={MODE: "build"})
    with pytest.raises(JobError, match="snapdep-plan"):
        run(generate, w)


# ---------------------------------------------------------------------------
# fail
# ---------------------------------------------------------------------------

GATED = {**BUILD_META, GATE_KEYS: json.dumps(["lint", "build"])}


def test_fail_all_passed_records_nothing():
    w = World(meta=GATED, outcomes={"lint": "passed", "build": "passed"})
    assert run(fail, w) == 0
    assert not list_failures(w.store, REST, key(REST)) and not w.agent.annotations


@pytest.mark.parametrize("outcome", ["hard_failed", "soft_failed", ""])
def test_fail_records_a_step_that_did_not_pass(outcome):
    w = World(meta=GATED, outcomes={"lint": "passed", "build": outcome})
    assert run(fail, w) == 0
    [f] = list_failures(w.store, REST, key(REST))
    assert [(s.key, s.outcome) for s in f.failed_steps] == [("build", outcome)]
    assert f.build == REST_BUILD and f.recorded_at == NOW
    assert f.plan == PLAN_ID, "the plan of the build, from its meta-data"
    body, style, _ = w.agent.annotations[-1]
    assert style == "error" and f"step build: {outcome or 'did not run'}" in body


@pytest.mark.parametrize("mode", ["plain", "reused", None])
def test_fail_outside_build_mode_does_nothing(mode):
    meta = {} if mode is None else {MODE: mode}
    w = World(meta=meta, outcomes={"build": "hard_failed"})
    assert run(fail, w) == 0
    assert not w.store.list_keys("_snapdep/v1/failures/")


def test_fail_does_not_record_when_this_build_is_the_version():
    p = plan()
    v = version(p, REST, 77)
    v = type(v)(**{**v.__dict__, "build": REST_BUILD})
    w = World(store=stored_versions(v), meta=GATED, outcomes={"build": "hard_failed"})
    assert run(fail, w) == 0
    assert not list_failures(w.store, REST, key(REST))


@pytest.mark.parametrize("outcome", ["hard_failed", "errored"])
def test_fail_records_a_failed_sentinel_after_passed_gates(outcome):
    # duck 141: every gate passed, the sentinel's job failed in the agent's
    # pre-checkout hook, seal never ran. Without a record the dependents said
    # "no failure on record: cancelled, lost or did not run".
    outcomes = {"lint": "passed", "build": "passed", BUILT: outcome}
    w = World(meta=GATED, outcomes=outcomes)
    assert run(fail, w) == 0
    [f] = list_failures(w.store, REST, key(REST))
    assert [(s.key, s.outcome) for s in f.failed_steps] == [(BUILT, outcome)]
    body, style, _ = w.agent.annotations[-1]
    assert style == "error" and f"step {BUILT}: {outcome}" in body


def test_fail_records_nothing_when_the_sentinel_did_not_run_after_passed_gates():
    # A build cancelled between the last gate and the sentinel: no record, so
    # whoever waits builds again (spec 5.7 rule 4).
    w = World(meta=GATED, outcomes={"lint": "passed", "build": "passed", BUILT: ""})
    assert run(fail, w) == 0
    assert not list_failures(w.store, REST, key(REST))


def test_fail_without_gate_keys_records_nothing():
    w = World(meta=BUILD_META)
    assert run(fail, w) == 0
    assert not list_failures(w.store, REST, key(REST))


# ---------------------------------------------------------------------------
# seal
# ---------------------------------------------------------------------------


def sealed(w):
    return find_version(w.store, REST, key(REST))


def test_seal_all_passed_records_the_version():
    w = World(meta=GATED, outcomes={"lint": "passed", "build": "passed"})
    assert run(seal, w) == 0
    v = sealed(w)
    assert v.build == REST_BUILD and v.gate_keys == ("lint", "build")
    assert v.deps == {DUCK: key(DUCK), QDB: key(QDB)}
    assert v.store_ref == "refs/heads/master" and v.commit == sha(REST)


@pytest.mark.parametrize("outcome", ["hard_failed", "soft_failed"])
def test_seal_does_not_record_when_a_gate_did_not_pass(outcome):
    w = World(meta=GATED, outcomes={"lint": "passed", "build": outcome})
    assert run(seal, w) == 0
    assert sealed(w) is None


def test_seal_outside_build_mode_does_nothing():
    w = World(meta={MODE: "reused"}, outcomes={"lint": "passed", "build": "passed"})
    assert run(seal, w) == 0
    assert sealed(w) is None


def test_seal_when_another_build_recorded_the_key_first():
    p = plan()
    other = version(p, REST, 70)
    w = World(
        store=stored_versions(other), meta=GATED, outcomes={"lint": "passed", "build": "passed"}
    )
    assert run(seal, w) == 0
    assert sealed(w) == other
    assert "recorded first by build 70" in w.logs[-1]


@pytest.mark.parametrize(
    "env, plan_rebuild", [({"SNAPDEP_REBUILD": "1"}, "none"), ({}, REBUILD_ALL)]
)
def test_seal_forced_overwrites(env, plan_rebuild):
    p = plan(rebuild=plan_rebuild)
    other = version(p, REST, 70)
    w = World(
        store=MemoryStore(objects(p, [other])),
        meta=GATED,
        outcomes={"lint": "passed", "build": "passed"},
    )
    assert run(seal, w, **env) == 0
    assert sealed(w).build == REST_BUILD


def test_seal_checks_produced_variants():
    outcomes = {"lint": "passed", "build": "passed"}
    w = World(meta=GATED, outcomes=outcomes)
    decl = declaration(produces=["linux-haswell-release"])
    with pytest.raises(JobError, match="no manifest of linux-haswell-release"):
        run(seal, w, decl)
    assert sealed(w) is None
    (f,) = list_failures(w.store, REST, key(REST))
    assert f.plan == PLAN_ID
    assert [(s.key, s.outcome) for s in f.failed_steps] == [
        ("variant linux-haswell-release", "no manifest")
    ]
    assert "variant linux-haswell-release: no manifest" in w.agent.annotations[-1][0]
    manifest = (
        f"{REST}/refs/heads/master/variants/linux-haswell-release/builds/"
        f"{REST_BUILD.uuid}/manifest.json"
    )
    w.store.put(manifest, b"{}")
    assert run(seal, w, decl) == 0
    assert sealed(w).build == REST_BUILD


def test_seal_refuses_a_key_that_is_not_the_plans():
    w = World(meta={**GATED, KEY: "v1-other"}, outcomes={"lint": "passed", "build": "passed"})
    with pytest.raises(JobError, match="the plan has"):
        run(seal, w)
    assert w.store.get(version_path(REST, "v1-other")) is None


def test_root_python_closure_in_plan_annotation_lists_every_project():
    p = plan()
    w = World(store=MemoryStore(objects(p)), checkout=sha(PYTHON))
    decl = declaration(slug=PYTHON, deps=(REST, QDB))
    env = environment(BUILDKITE_PIPELINE_SLUG=PYTHON)
    assert enter(job_from_env(env), lambda: decl, w.fx) == 0
    note = w.agent.annotations[0][0]
    for project in (QDB, DUCK, REST, PYTHON):
        assert f"| {project} |" in note
    assert "build in the chain" in note, "qdb-duck is built by qdb-api-rest's build"
