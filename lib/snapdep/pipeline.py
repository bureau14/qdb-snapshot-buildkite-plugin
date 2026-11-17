"""Buildkite pipelines as data: pure functions on the parsed YAML.

Specification sections 5.2 and 5.3. The generator of an adopting repository
prints a pipeline; the step commands parse it (``parse_pipeline``), read and
change it with the functions here, and print it again (``dump_pipeline``).
Nothing here does input or output; every function returns a new value and
leaves its arguments alone.

- step walking: ``command_steps`` descends into groups; ``assign_keys`` gives
  every command step without a key a stable one;
- the gate: ``missing_gates`` (is this a version build), ``gate_keys``,
  ``sentinel_step``;
- dependencies: ``trigger_steps`` and ``generate_step``;
- ``inject_build_ids``: the download entries of the artifacts plugin;
- plugin references: ``plugin_name``, ``own_plugin_reference``.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from .planner import dependencies_first
from .records import Plan

SNAPSHOT_PLUGIN = "bureau14/qdb-snapshot"
ARTIFACTS_PLUGIN = "bureau14/qdb-artifacts"

ENTER = "snapdep-enter"
GENERATE = "snapdep-generate"
BUILT = "snapdep-built"
FAIL = "snapdep-fail"
SEAL = "snapdep-seal"
DEP_PREFIX = "snapdep-dep-"
AUTO_PREFIX = "snapdep-auto-"

SHORT_COMMIT = 10

_NOT_COMMAND = ("wait", "block", "input", "trigger", "group")
_COMMAND_FIELDS = ("command", "commands", "plugins")
_KEY_FIELDS = ("key", "id", "identifier")


class PipelineError(ValueError):
    """A generated pipeline cannot be read or cannot be adopted as it is."""


def short(commit: str) -> str:
    """The short form of a commit shown to people."""
    return commit[:SHORT_COMMIT]


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------


def parse_pipeline(text: str) -> dict[str, Any]:
    """The pipeline document in ``text`` (YAML or JSON), with a ``steps`` list.

    A bare list of steps is accepted and returned as ``{"steps": [...]}``.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PipelineError(f"generated pipeline is not valid YAML: {exc}") from exc
    if isinstance(doc, list):
        doc = {"steps": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("steps"), list):
        raise PipelineError("generated pipeline has no list of steps")
    return doc


def dump_pipeline(doc: Mapping[str, Any]) -> str:
    """``doc`` as YAML, in the order of its keys."""
    return yaml.safe_dump(dict(doc), sort_keys=False, default_flow_style=False, width=4096)


def steps_document(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A pipeline document that holds ``steps``."""
    return {"steps": [dict(s) for s in steps]}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def step_key(s: Any) -> str | None:
    """The key of step ``s`` (``key``, or its aliases ``id`` and ``identifier``)."""
    if not isinstance(s, dict):
        return None
    for f in _KEY_FIELDS:
        if isinstance(s.get(f), str) and s[f]:
            return s[f]
    return None


def is_group(s: Any) -> bool:
    return isinstance(s, dict) and "group" in s and isinstance(s.get("steps"), list)


def is_command_step(s: Any) -> bool:
    """True for a command step: a mapping with a command or plugins, of no other kind."""
    if not isinstance(s, dict):
        return False
    if "type" in s:
        return s["type"] in ("command", "script")
    return not any(k in s for k in _NOT_COMMAND) and any(k in s for k in _COMMAND_FIELDS)


def all_steps(steps: Sequence[Any]) -> Iterator[Any]:
    """Every step of ``steps`` in order, a group before the steps inside it."""
    for s in steps:
        yield s
        if is_group(s):
            yield from all_steps(s["steps"])


def command_steps(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The command steps of ``doc`` in order, descending into groups."""
    return [s for s in all_steps(doc["steps"]) if is_command_step(s)]


def command_step_keys(doc: Mapping[str, Any]) -> list[str]:
    """The keys of the command steps of ``doc`` that have one, in order."""
    return [k for k in (step_key(s) for s in command_steps(doc)) if k]


def all_step_keys(doc: Mapping[str, Any]) -> list[str]:
    """The keys of every step of ``doc``, groups and non-command steps included."""
    return [k for k in (step_key(s) for s in all_steps(doc["steps"])) if k]


def step_name(s: Mapping[str, Any]) -> str:
    """How a step is named in messages: its key, else its label."""
    return step_key(s) or str(s.get("label") or s.get("name") or "(unnamed step)")


def auto_key(label: str, position: int) -> str:
    """The key given to the unkeyed command step at ``position`` with ``label``."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40].strip("-")
    return f"{AUTO_PREFIX}{position}-{slug or 'step'}"


def _unique(k: str, taken: Collection[str]) -> str:
    n, out = 2, k
    while out in taken:
        out, n = f"{k}-{n}", n + 1
    return out


def assign_keys(doc: Mapping[str, Any]) -> dict[str, Any]:
    """``doc`` with a key on every command step that has none.

    The key comes from the step's label and its position among the command
    steps (``auto_key``), so the same generated pipeline gets the same keys in
    every job of a build.
    """
    out = copy.deepcopy(dict(doc))
    taken = set(all_step_keys(out))
    for i, s in enumerate(command_steps(out)):
        if step_key(s) is None:
            k = _unique(auto_key(str(s.get("label") or s.get("name") or ""), i), taken)
            s["key"] = k
            taken.add(k)
    return out


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def missing_gates(gates: Sequence[str], doc: Mapping[str, Any]) -> list[str]:
    """The declared gates that ``doc`` does not contain; non-empty: not a version build."""
    keys = set(all_step_keys(doc))
    return [g for g in gates if g not in keys]


def accepts_any_failure(s: Mapping[str, Any]) -> bool:
    """True for a step whose every failure is a soft failure (``soft_fail: true`` or exit ``*``).

    Its build passes when it fails, so it cannot decide whether the build is a
    version.
    """
    v = s.get("soft_fail")
    if v is True or v == "true":
        return True
    return isinstance(v, list) and any(
        isinstance(e, dict) and str(e.get("exit_status")) == "*" for e in v
    )


def gate_keys(gates: Sequence[str], doc: Mapping[str, Any]) -> list[str]:
    """The steps that must pass for a version: the declared gates, else every command step.

    By default a step that accepts any failure (``accepts_any_failure``) is not
    a gate. ``doc`` must have keys on its command steps (``assign_keys``).
    """
    if gates:
        return list(gates)
    steps = (s for s in command_steps(doc) if not accepts_any_failure(s))
    return [k for k in (step_key(s) for s in steps) if k]


# The sentinel's command is `true`: its job fails only when the agent or a
# hook fails (a checkout that cannot reach GitHub). fail and seal read the
# gates' own outcomes, so retrying it never hides a failed gate.
SENTINEL_RETRY = {"automatic": [{"exit_status": "*", "limit": 2}]}


def sentinel_step(gates: Sequence[str]) -> dict[str, Any]:
    """The step that passes when every gate passed; without gates it passes at once."""
    s: dict[str, Any] = {"label": ":camera_with_flash: snapshot built", "key": BUILT}
    s["command"] = "true"
    if gates:
        s["depends_on"] = list(gates)
    s["retry"] = copy.deepcopy(SENTINEL_RETRY)
    return s


def with_sentinel(doc: Mapping[str, Any], gates: Sequence[str]) -> dict[str, Any]:
    """``doc`` followed by the sentinel on ``gates``, as one document."""
    out = copy.deepcopy(dict(doc))
    out["steps"] = [*out["steps"], sentinel_step(gates)]
    return out


# ---------------------------------------------------------------------------
# Dependencies: trigger steps and the generate step
# ---------------------------------------------------------------------------


def trigger_key(project: str) -> str:
    return DEP_PREFIX + project


def trigger_label(project: str, commit: str) -> str:
    return f":link: {project} @ {short(commit)}"


def trigger_message(p: Plan) -> str:
    """The message of a triggered build; never contains a colon."""
    m = f"snapshot dependency of {p.root.project} build {p.root.build_number}"
    return m.replace(":", " ")


def trigger_order(p: Plan, project: str, stale: Collection[str]) -> list[str]:
    """``stale`` dependencies of ``project`` in plan ``p``, each after its own dependencies."""
    return [q for q in dependencies_first(p, project) if q in stale and q != project]


def trigger_depends(p: Plan, dependency: str, stale: Collection[str]) -> list[str]:
    """Trigger keys that the trigger of ``dependency`` waits for.

    Those of the stale dependencies in ``dependency``'s own closure.
    """
    closure = dependencies_first(p, dependency)[:-1]
    return [trigger_key(q) for q in closure if q in stale]


def trigger_step(p: Plan, dependency: str, stale: Collection[str]) -> dict[str, Any]:
    """The synchronous trigger step that builds ``dependency`` as planned in ``p``."""
    n = p.nodes[dependency]
    s: dict[str, Any] = {
        "label": trigger_label(dependency, n.commit),
        "key": trigger_key(dependency),
        "trigger": dependency,
        "soft_fail": True,
    }
    depends = trigger_depends(p, dependency, stale)
    if depends:
        s["depends_on"] = depends
    s["build"] = {
        "branch": n.label,
        "commit": n.commit,
        "message": trigger_message(p),
        "env": {"SNAPDEP_PLAN": p.id},
    }
    return s


def trigger_steps(p: Plan, project: str, stale: Collection[str]) -> list[dict[str, Any]]:
    """One trigger step per stale dependency of ``project``, dependencies first."""
    return [trigger_step(p, d, stale) for d in trigger_order(p, project, stale)]


def generate_step(plugin_reference: str, trigger_keys: Sequence[str]) -> dict[str, Any]:
    """The step that generates the pipeline once the triggers have run.

    It runs however the triggers ended (``allow_failure``, the measured form):
    it is generate that turns a dependency without a version into a failure.
    """
    s: dict[str, Any] = {"label": ":camera_with_flash: snapshot generate", "key": GENERATE}
    if trigger_keys:
        s["depends_on"] = [{"step": k, "allow_failure": True} for k in trigger_keys]
    s["plugins"] = [{plugin_reference: {"step": "generate"}}]
    return s


# ---------------------------------------------------------------------------
# Plugin references
# ---------------------------------------------------------------------------


def plugin_name(source: str) -> str:
    """``owner/name`` of a plugin source, without its reference.

    ``bureau14/qdb-artifacts#master``, ``github.com/bureau14/qdb-artifacts-buildkite-plugin#x``
    and ``https://github.com/bureau14/qdb-artifacts-buildkite-plugin.git#x`` all give
    ``bureau14/qdb-artifacts``.
    """
    s = source.split("#", 1)[0].strip()
    s = re.sub(r"^(?:https?://|ssh://|git@)", "", s)
    s = re.sub(r"^github\.com[:/]", "", s)
    s = s[: -len(".git")] if s.endswith(".git") else s
    return s[: -len("-buildkite-plugin")] if s.endswith("-buildkite-plugin") else s


def plugin_reference(source: str) -> str | None:
    """The reference after ``#`` in a plugin source; None when there is none."""
    _, sep, ref = source.partition("#")
    return ref if sep and ref else None


def own_plugin_reference(plugins_json: str | None) -> str:
    """``bureau14/qdb-snapshot#<ref>`` with the reference this job was called with.

    ``plugins_json`` is ``BUILDKITE_PLUGINS``: a JSON list of one-entry mappings
    from plugin source to configuration.
    """
    if not plugins_json:
        raise PipelineError("BUILDKITE_PLUGINS is not set: run as the snapshot plugin")
    try:
        plugins = json.loads(plugins_json)
    except ValueError as exc:
        raise PipelineError(f"BUILDKITE_PLUGINS is not JSON: {exc}") from exc
    sources = [src for p in plugins if isinstance(p, dict) for src in p]
    refs = sorted({plugin_reference(s) or "" for s in sources if plugin_name(s) == SNAPSHOT_PLUGIN})
    if not refs:
        raise PipelineError(f"BUILDKITE_PLUGINS does not name {SNAPSHOT_PLUGIN}")
    if len(refs) > 1 or not refs[0]:
        raise PipelineError(f"{SNAPSHOT_PLUGIN}: expected one reference, got {refs}")
    return f"{SNAPSHOT_PLUGIN}#{refs[0]}"


def _plugin_entries(s: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """(source, configuration) of every plugin of step ``s``, list or mapping form."""
    plugins = s.get("plugins")
    if isinstance(plugins, dict):
        return list(plugins.items())
    if not isinstance(plugins, list):
        return []
    out: list[tuple[str, Any]] = []
    for p in plugins:
        if isinstance(p, str):
            out.append((p, None))
        elif isinstance(p, dict):
            out.extend(p.items())
    return out


# ---------------------------------------------------------------------------
# Injection into the artifacts plugin's downloads
# ---------------------------------------------------------------------------

# Option names of a download.projects entry of the artifacts plugin. Buildkite
# turns "-" into "_" in plugin variables, so a hyphenated spelling is the same
# option and is replaced too.
BUILD_ID = "build_id"
GIT_REF = "git_ref"
STRICT = "require_strict_ref_match"
PROJECT_ID = "project_id"


@dataclass(frozen=True)
class Pin:
    """What a download of one project is pinned to: a build and the ref it is filed under."""

    build_id: str
    git_ref: str


def _spellings(option: str) -> tuple[str, str]:
    return option, option.replace("_", "-")


def pin_entry(entry: Mapping[str, Any], pin: Pin) -> dict[str, Any]:
    """Download entry ``entry`` pinned to ``pin``, with strict ref matching on."""
    replaced = {*_spellings(BUILD_ID), *_spellings(GIT_REF), *_spellings(STRICT)}
    out = {k: v for k, v in entry.items() if k not in replaced}
    out[BUILD_ID] = pin.build_id
    out[GIT_REF] = pin.git_ref
    out[STRICT] = True
    return out


def _entry_project(entry: Mapping[str, Any]) -> str | None:
    for k in _spellings(PROJECT_ID):
        if entry.get(k):
            return str(entry[k])
    return None


def _inject_entry(
    entry: Any, own: str, pins: Mapping[str, Pin], declared: Collection[str], where: str
) -> Any:
    if not isinstance(entry, dict):
        return entry
    project = _entry_project(entry)
    if project is None or project == own:
        return entry
    if project not in declared:
        raise PipelineError(
            f"step {where}: downloads from {project}, which is not a declared dependency of {own}"
        )
    return pin_entry(entry, pins[project]) if project in pins else entry


def _inject_step(
    s: dict[str, Any], own: str, pins: Mapping[str, Pin], declared: Collection[str]
) -> None:
    for source, config in _plugin_entries(s):
        if plugin_name(source) != ARTIFACTS_PLUGIN or not isinstance(config, dict):
            continue
        download = config.get("download")
        if not isinstance(download, dict) or not isinstance(download.get("projects"), list):
            continue
        download["projects"] = [
            _inject_entry(e, own, pins, declared, step_name(s)) for e in download["projects"]
        ]


def inject_build_ids(
    doc: Mapping[str, Any], own: str, pins: Mapping[str, Pin], declared: Collection[str]
) -> dict[str, Any]:
    """``doc`` with the artifact downloads of its dependencies pinned.

    In every step, in every ``bureau14/qdb-artifacts`` plugin block, every
    ``download.projects`` entry whose ``project_id`` is in ``pins`` gets that
    pin's build id and git ref and strict ref matching. Entries without
    ``project_id``, entries for ``own`` and entries for declared projects
    without a pin (opaque dependencies) are left alone. An entry for any other
    project raises ``PipelineError`` naming the step.
    """
    out = copy.deepcopy(dict(doc))
    for s in all_steps(out["steps"]):
        if isinstance(s, dict):
            _inject_step(s, own, pins, declared)
    return out
