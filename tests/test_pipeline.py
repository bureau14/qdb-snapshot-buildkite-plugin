from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from plans import DUCK, PLAN_ID, PLUGIN_COMMIT, PLUGINS, PYTHON, QDB, REST, plan, sha

from snapdep.pipeline import (
    ARTIFACTS_PLUGIN,
    BUILT,
    GENERATE,
    Pin,
    PipelineError,
    assign_keys,
    auto_key,
    command_step_keys,
    command_steps,
    dump_pipeline,
    gate_keys,
    generate_step,
    inject_build_ids,
    missing_gates,
    own_plugin_reference,
    parse_pipeline,
    plugin_name,
    sentinel_step,
    trigger_steps,
    with_sentinel,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Fixture file -> the pipeline slug it is generated for.
FIXTURE_SLUGS = {
    "qdb-api-go.yml": "qdb-api-go",
    "qdb-api-python.yml": PYTHON,
    "qdb-api-rest.yml": REST,
    "qdb-duck.yml": DUCK,
    "quasardb.yml": QDB,
}

MIXED = """
steps:
  - label: ":hammer: build (linux)"
    command: make
  - wait
  - group: Tests
    key: tests
    steps:
      - label: unit
        commands: [make test]
      - label: unit
        command: make test2
      - key: e2e
        command: make e2e
      - block: release?
  - trigger: other
  - label: plugins only
    plugins:
      - docker#v5: {image: x}
  - {type: command, label: typed, command: x}
  - input: something
"""


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_parse_accepts_a_bare_list_and_rejects_no_steps():
    assert parse_pipeline("- command: x\n") == {"steps": [{"command": "x"}]}
    with pytest.raises(PipelineError, match="no list of steps"):
        parse_pipeline("env: {}\n")
    with pytest.raises(PipelineError, match="not valid YAML"):
        parse_pipeline("steps: [\n")


def test_command_steps_descend_into_groups_and_skip_other_kinds():
    doc = parse_pipeline(MIXED)
    labels = [s.get("label") or s.get("key") for s in command_steps(doc)]
    assert labels == [":hammer: build (linux)", "unit", "unit", "e2e", "plugins only", "typed"]


def test_assign_keys_is_stable_unique_and_keeps_existing_keys():
    doc = parse_pipeline(MIXED)
    once = assign_keys(doc)
    assert command_step_keys(once) == [
        "snapdep-auto-0-hammer-build-linux",
        "snapdep-auto-1-unit",
        "snapdep-auto-2-unit",
        "e2e",
        "snapdep-auto-4-plugins-only",
        "snapdep-auto-5-typed",
    ]
    assert assign_keys(parse_pipeline(MIXED)) == once
    assert doc == parse_pipeline(MIXED), "the argument is left alone"


def test_assign_keys_avoids_a_key_already_taken():
    doc = {"steps": [{"label": "x", "command": "a"}, {"key": "snapdep-auto-0-x", "wait": None}]}
    assert command_step_keys(assign_keys(doc)) == ["snapdep-auto-0-x-2"]


def test_auto_key_without_a_label():
    assert auto_key("", 3) == "snapdep-auto-3-step"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_missing_gates_names_keys_the_pipeline_lacks():
    doc = assign_keys(parse_pipeline(MIXED))
    assert missing_gates(["e2e", "tests"], doc) == []
    assert missing_gates(["e2e", "build-windows"], doc) == ["build-windows"]
    assert missing_gates([], doc) == []


def test_gate_keys_default_to_every_command_step():
    doc = assign_keys(parse_pipeline(MIXED))
    assert gate_keys([], doc) == command_step_keys(doc)
    assert gate_keys(["e2e"], doc) == ["e2e"]


SOFT = """
steps:
  - {key: a, command: x, soft_fail: true}
  - {key: b, command: x, soft_fail: [{exit_status: "*"}]}
  - {key: c, command: x, soft_fail: [{exit_status: 2}]}
  - {key: d, command: x, soft_fail: false}
  - {key: e, command: x}
"""


def test_gate_keys_leave_out_steps_that_accept_any_failure():
    doc = parse_pipeline(SOFT)
    assert gate_keys([], doc) == ["c", "d", "e"]
    assert gate_keys(["a", "e"], doc) == ["a", "e"], "a declared gate is a gate"


def test_sentinel_with_and_without_gates():
    assert sentinel_step([]) == {
        "label": ":camera_with_flash: snapshot built",
        "key": BUILT,
        "command": "true",
    }
    assert sentinel_step(["a", "b"])["depends_on"] == ["a", "b"]


def test_with_sentinel_appends_it_last():
    doc = {"env": {"A": "1"}, "steps": [{"key": "a", "command": "x"}]}
    out = with_sentinel(doc, ["a"])
    assert out["env"] == {"A": "1"}
    assert out["steps"][-1]["key"] == BUILT
    assert len(doc["steps"]) == 1


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_triggers_on_the_diamond_wait_for_their_own_closure():
    p = plan()
    steps = trigger_steps(p, PYTHON, {REST, QDB})
    assert [s["key"] for s in steps] == ["snapdep-dep-quasardb-build", "snapdep-dep-qdb-api-rest"]
    assert "depends_on" not in steps[0]
    assert steps[1]["depends_on"] == ["snapdep-dep-quasardb-build"]


def test_trigger_of_rest_alone_waits_for_nothing():
    steps = trigger_steps(plan(), PYTHON, {REST})
    assert [s["key"] for s in steps] == ["snapdep-dep-qdb-api-rest"]
    assert "depends_on" not in steps[0]


def test_trigger_step_shape():
    p = plan(label="sc-1/feature")
    [s] = trigger_steps(p, REST, {DUCK})
    assert s == {
        "label": f":link: qdb-duck @ {sha(DUCK)[:10]}",
        "key": "snapdep-dep-qdb-duck",
        "trigger": DUCK,
        "soft_fail": True,
        "build": {
            "branch": "sc-1/feature",
            "commit": sha(DUCK),
            "message": "snapshot dependency of qdb-api-python build 512",
            "env": {"SNAPDEP_PLAN": PLAN_ID},
        },
    }


def test_trigger_message_never_contains_a_colon():
    p = plan()
    p = type(p)(**{**p.__dict__, "root": type(p.root)("a:b", 1, "", "x")})
    [s] = trigger_steps(p, REST, {DUCK})
    assert ":" not in s["build"]["message"]


def test_generate_step():
    s = generate_step("bureau14/qdb-snapshot#abc", ["snapdep-dep-x"])
    assert s == {
        "label": ":camera_with_flash: snapshot generate",
        "key": GENERATE,
        "depends_on": [{"step": "snapdep-dep-x", "allow_failure": True}],
        "plugins": [{"bureau14/qdb-snapshot#abc": {"step": "generate"}}],
    }


# ---------------------------------------------------------------------------
# Plugin references
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "bureau14/qdb-artifacts#master",
        "bureau14/qdb-artifacts",
        "github.com/bureau14/qdb-artifacts-buildkite-plugin#v1",
        "https://github.com/bureau14/qdb-artifacts-buildkite-plugin.git#x",
        "git@github.com:bureau14/qdb-artifacts-buildkite-plugin.git#x",
    ],
)
def test_plugin_name(source):
    assert plugin_name(source) == ARTIFACTS_PLUGIN


def test_own_plugin_reference_reuses_the_reference_it_was_called_with():
    assert own_plugin_reference(PLUGINS) == f"bureau14/qdb-snapshot#{PLUGIN_COMMIT}"
    short = json.dumps([{"bureau14/qdb-snapshot#abc": {"step": "enter"}}])
    assert own_plugin_reference(short) == "bureau14/qdb-snapshot#abc"


@pytest.mark.parametrize(
    "value, message",
    [
        (None, "not set"),
        ("[", "not JSON"),
        (json.dumps([{"bureau14/qdb-artifacts#x": {}}]), "does not name"),
        (json.dumps([{"bureau14/qdb-snapshot": {}}]), "expected one reference"),
        (
            json.dumps([{"bureau14/qdb-snapshot#a": {}}, {"bureau14/qdb-snapshot#b": {}}]),
            "expected one reference",
        ),
    ],
)
def test_own_plugin_reference_errors(value, message):
    with pytest.raises(PipelineError, match=message):
        own_plugin_reference(value)


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

PIN = Pin("01b-build", "refs/heads/master")


def downloads(doc):
    """(step key, entry) of every download entry of the artifacts plugin in ``doc``."""
    out = []
    for s in command_steps(doc):
        for p in s.get("plugins", []):
            for name, config in p.items() if isinstance(p, dict) else []:
                if plugin_name(name) == ARTIFACTS_PLUGIN and "download" in (config or {}):
                    out += [(s.get("key"), e) for e in config["download"]["projects"]]
    return out


def fixture(name: str):
    return assign_keys(parse_pipeline((FIXTURES / name).read_text()))


def test_every_fixture_is_present():
    assert sorted(p.name for p in FIXTURES.glob("*.yml")) == sorted(FIXTURE_SLUGS)


@pytest.mark.parametrize("name", sorted(FIXTURE_SLUGS))
def test_injection_on_real_pipelines(name):
    own = FIXTURE_SLUGS[name]
    doc = fixture(name)
    before = downloads(doc)
    declared = sorted({e["project_id"] for _, e in before if e.get("project_id", own) != own})
    pins = {d: Pin(f"build-of-{d}", f"refs/heads/line-of-{d}") for d in declared}
    out = inject_build_ids(doc, own, pins, declared)
    after = downloads(out)
    assert len(after) == len(before)
    for (k1, e1), (k2, e2) in zip(before, after):
        assert k1 == k2
        project = e1.get("project_id")
        if project in pins:
            assert e2["build_id"] == pins[project].build_id
            assert e2["git_ref"] == pins[project].git_ref
            assert e2["require_strict_ref_match"] is True
            assert not set(e2) & {"git-ref", "build-id", "require-strict-ref-match"}
            assert without_pin_options(e2) == without_pin_options(e1)
        else:
            assert e2 == e1
    assert strip_pins(out) == strip_pins(doc), "nothing else changes"
    assert yaml.safe_load(dump_pipeline(out)) == out


PIN_OPTIONS = {
    "build_id",
    "build-id",
    "git_ref",
    "git-ref",
    "require_strict_ref_match",
    "require-strict-ref-match",
}


def without_pin_options(e):
    return {k: v for k, v in e.items() if k not in PIN_OPTIONS}


def strip_pins(doc):
    out = copy.deepcopy(doc)
    for _, e in downloads(out):
        for k in PIN_OPTIONS:
            e.pop(k, None)
    return out


def test_fixtures_have_downloads_of_dependencies():
    projects = {
        name: sorted({e.get("project_id") for _, e in downloads(fixture(name))})
        for name in FIXTURE_SLUGS
    }
    assert projects["qdb-api-rest.yml"] == [DUCK, QDB]
    assert projects["qdb-api-python.yml"] == [REST, QDB]
    assert projects["quasardb.yml"] == []


HAND_MADE = f"""
steps:
  - group: g
    steps:
      - key: build
        command: make
        plugins:
          - bureau14/qdb-artifacts#feature:
              download:
                projects:
                  - {{variant: v, git_ref: refs/heads/x, files: [a], project_id: {REST}}}
                  - {{variant: v, git-ref: refs/heads/x, files: [a], project_id: {QDB},
                      build-id: old, require-strict-ref-match: false}}
                  - {{variant: v, git_ref: refs/heads/x, files: [a]}}
                  - {{variant: v, git_ref: refs/heads/x, files: [a], project_id: {DUCK}}}
          - other/plugin#v1:
              download:
                projects: [{{project_id: undeclared}}]
"""


def test_injection_own_project_no_project_id_opaque_and_hyphenated_options():
    doc = parse_pipeline(HAND_MADE)
    out = inject_build_ids(doc, REST, {QDB: PIN}, [QDB, DUCK])
    entries = [e for _, e in downloads(out)]
    assert entries[0] == {
        "variant": "v",
        "git_ref": "refs/heads/x",
        "files": ["a"],
        "project_id": REST,
    }
    assert entries[1] == {
        "variant": "v",
        "files": ["a"],
        "project_id": QDB,
        "build_id": PIN.build_id,
        "git_ref": PIN.git_ref,
        "require_strict_ref_match": True,
    }
    assert entries[2] == {"variant": "v", "git_ref": "refs/heads/x", "files": ["a"]}
    assert "build_id" not in entries[3], "a declared dependency without a pin (opaque)"
    assert out["steps"][0]["steps"][0]["plugins"][1] == doc["steps"][0]["steps"][0]["plugins"][1]


def test_injection_of_an_undeclared_project_names_the_step():
    doc = parse_pipeline(HAND_MADE)
    with pytest.raises(PipelineError, match="step build: downloads from qdb-api-rest"):
        inject_build_ids(doc, PYTHON, {QDB: PIN}, [QDB, DUCK])


def test_injection_with_plugins_as_a_mapping():
    doc = {
        "steps": [
            {
                "command": "x",
                "plugins": {
                    "bureau14/qdb-artifacts#master": {
                        "download": {"projects": [{"project_id": QDB, "git_ref": "r"}]}
                    }
                },
            }
        ]
    }
    out = inject_build_ids(doc, REST, {QDB: PIN}, [QDB])
    [entry] = out["steps"][0]["plugins"]["bureau14/qdb-artifacts#master"]["download"]["projects"]
    assert entry["build_id"] == PIN.build_id
