"""The jobs of an adopted build: enter, generate, fail and seal.

Specification sections 5.2 to 5.5. Each job is a short composition of pure
decisions (the functions above ``enter``) and four interfaces gathered in
``Effects``: the store, the Buildkite agent, git and the generator. The
command line builds the real ``Effects``; tests pass fakes.

What a job knows about its build comes from the environment (``job_from_env``)
and from the build meta-data written by earlier jobs of the same build:

- ``snapdep-arrived``: when the upload job ran (the shim of section 5.1);
- ``snapdep-mode``: ``plain``, ``reused`` or ``build``, set by enter;
- ``snapdep-key``, ``snapdep-plan``: the own key and the plan id, set by enter
  in build mode;
- ``snapdep-gate-keys``: JSON list of the steps that must pass, set when the
  real pipeline is uploaded.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable

import yaml

from . import annotation as notes
from .agent import MESSAGE, PIPELINE_FILTER, Agent, Generator
from .declaration import PATH, Declaration, Pipeline, generate_command, pipeline_entry
from .git import Git, github_repo
from .pipeline import (
    Pin,
    assign_keys,
    dump_pipeline,
    gate_keys,
    generate_step,
    inject_build_ids,
    missing_gates,
    own_plugin_reference,
    parse_pipeline,
    sentinel_step,
    steps_document,
    trigger_key,
    trigger_steps,
    with_sentinel,
)
from .planner import Root, dependencies_first, load_plan_for, make_plan
from .records import (
    REBUILD_ALL,
    BuildRef,
    Cause,
    FailedStep,
    Failure,
    Plan,
    Version,
    create_or_load_plan,
    epoch_from_iso,
    failure_record,
    find_version,
    holder_record,
    is_disabled,
    list_failures,
    record_version,
    store_ref,
    version_record,
    write_failure,
    write_holder,
)
from .store import Store, key_join

Env = Mapping[str, str]

ARRIVED = "snapdep-arrived"
MODE = "snapdep-mode"
KEY = "snapdep-key"
PLAN = "snapdep-plan"
GATE_KEYS = "snapdep-gate-keys"

PLAIN = "plain"
REUSED = "reused"
BUILD = "build"

REBUILD_SELF = "1"
PASSED = "passed"


class JobError(RuntimeError):
    """A job cannot run in this build as it is."""


@dataclass(frozen=True)
class Effects:
    """Everything a job may touch outside itself."""

    store: Store
    agent: Agent
    git: Git
    generator: Generator
    clock: Callable[[], float]
    log: Callable[[str], None]


# ---------------------------------------------------------------------------
# The job's view of its build, from the environment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """This build: its pipeline, branch, repository and identity, and the person's settings.

    ``plan_id``: ``SNAPDEP_PLAN`` (set on builds started by a trigger step), None
    for a root. ``rebuild``: ``""``, ``"1"`` or ``"all"`` (``SNAPDEP_REBUILD``).
    ``plugins``: ``BUILDKITE_PLUGINS``. ``selection``: the build asks for a
    part of its pipeline (``selection_requested``).
    """

    project: str
    branch: str
    repo: str | None
    build: BuildRef
    plan_id: str | None
    rebuild: str
    disabled: bool
    plugins: str | None
    selection: bool = False


def _required(env: Env, name: str) -> str:
    v = env.get(name)
    if not v:
        raise JobError(f"{name} is not set: run inside a Buildkite job")
    return v


def is_yaml_mapping(text: str) -> bool:
    """True when ``text`` parses as a non-empty YAML mapping; invalid YAML is not one."""
    try:
        return isinstance(yaml.safe_load(text), dict)
    except yaml.YAMLError:
        return False


def selection_requested(env: Env) -> bool:
    """Whether the build may select a part of its pipeline, as ``qdb_pipeline.selection`` reads it.

    ``QDB_PIPELINE_FILTER`` is set and not blank, or the build message is a
    YAML mapping. Whether the generator honours it is decided by running it
    without the selection (``enter``).
    """
    f = env.get(PIPELINE_FILTER)
    if f is not None:
        return bool(f.strip())
    return is_yaml_mapping(env.get(MESSAGE, ""))


def rebuild_setting(env: Env) -> str:
    """``SNAPDEP_REBUILD`` as ``""`` (none), ``"1"`` (this build) or ``"all"`` (the chain)."""
    v = env.get("SNAPDEP_REBUILD", "").strip().lower()
    if v in ("", "0"):
        return ""
    if v in (REBUILD_SELF, REBUILD_ALL):
        return v
    raise JobError(f"SNAPDEP_REBUILD={v!r}: expected 1 or all")


def job_from_env(env: Env) -> Job:
    """The ``Job`` described by the Buildkite environment ``env``."""
    project = _required(env, "BUILDKITE_PIPELINE_SLUG")
    return Job(
        project=project,
        branch=_required(env, "BUILDKITE_BRANCH"),
        repo=github_repo(env.get("BUILDKITE_REPO", "")),
        build=BuildRef(
            uuid=_required(env, "BUILDKITE_BUILD_ID"),
            number=int(env.get("BUILDKITE_BUILD_NUMBER", "0")),
            url=env.get("BUILDKITE_BUILD_URL", ""),
            pipeline=project,
        ),
        plan_id=env.get("SNAPDEP_PLAN") or None,
        rebuild=rebuild_setting(env),
        disabled=env.get("SNAPDEP_DISABLE", "") == "1",
        plugins=env.get("BUILDKITE_PLUGINS"),
        selection=selection_requested(env),
    )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def plain_reason(job: Job, entry: Pipeline | None) -> str | None:
    """Why this build runs in plain mode without asking the store; None when it does not."""
    if job.disabled:
        return "SNAPDEP_DISABLE=1"
    if entry is None:
        return f"{PATH} has no entry for {job.project}"
    return None


def partial_reason(
    gates: Sequence[str], doc: Mapping, unfiltered: Mapping | None = None
) -> str | None:
    """Why the generated pipeline ``doc`` cannot make a version; None when it can.

    A declared gate is missing (a filtered build); the pipeline generated
    without the build's selection (``unfiltered``, when the build asked for
    one) differs from ``doc``; or there is no step to gate a version on.
    """
    missing = missing_gates(gates, doc)
    if missing:
        return f"no step {', '.join(missing)}"
    if unfiltered is not None and unfiltered != doc:
        return f"{PIPELINE_FILTER} or the build message selects a part of the pipeline"
    if not gate_keys(gates, doc):
        return "the pipeline has no command step to gate a version on"
    return None


def sealed_before_plan(v: Version, p: Plan) -> bool:
    """True when ``v`` was sealed before plan ``p`` was made."""
    return epoch_from_iso(v.sealed_at) < epoch_from_iso(p.created_at)


def counts_as_built(v: Version | None, p: Plan, forced: bool) -> bool:
    """Whether version ``v`` of a key is used, under plan ``p``.

    Not when there is none, not when this build is ``forced``, and under
    ``rebuild: all`` only when it was sealed after the plan was made (built in
    this chain).
    """
    if v is None or forced:
        return False
    return not (p.rebuild == REBUILD_ALL and sealed_before_plan(v, p))


def self_forced(job: Job) -> bool:
    """``SNAPDEP_REBUILD=1``: this build builds even when its key has a version."""
    return job.rebuild == REBUILD_SELF


def stale_dependencies(p: Plan, project: str, versions: Mapping[str, Version | None]) -> list[str]:
    """Direct dependencies of ``project`` that are not opaque and have no usable version."""
    return [
        d
        for d in p.nodes[project].deps
        if not p.nodes[d].opaque and not counts_as_built(versions.get(d), p, False)
    ]


def shared_failure(
    failures: Sequence[Failure], arrived: float | None, own_build: str
) -> Failure | None:
    """The failure of the build this one waited behind at the gate, if there is one.

    The latest failure of another build recorded after this build arrived.
    ``failures`` are oldest first. Without an arrival time there is none.
    """
    if arrived is None:
        return None
    later = [f for f in failures if f.recorded_at > arrived and f.build.uuid != own_build]
    return later[-1] if later else None


def parse_arrived(v: str | None) -> float | None:
    """``snapdep-arrived`` as epoch seconds; None when it is not set."""
    if v is None or not v.strip():
        return None
    try:
        return float(v)
    except ValueError as exc:
        raise JobError(f"{ARRIVED}={v!r}: expected epoch seconds") from exc


def plan_actions(
    p: Plan,
    project: str,
    versions: Mapping[str, Version | None],
    forced: bool,
    stale: Sequence[str],
) -> dict[str, str]:
    """What will happen to each project of ``project``'s closure, for the plan annotation."""

    def action(q: str) -> str:
        n = p.nodes[q]
        v = versions.get(q)
        built = counts_as_built(v, p, forced and q == project)
        if n.opaque:
            return "not snapshot-managed: latest builds"
        if built and v is not None:
            return f"reuse [build {v.build.number}]({v.build.url})"
        if q == project:
            return "build in this build"
        if q in stale:
            return f"build through step `{trigger_key(q)}`"
        return "build in the chain"

    return {q: action(q) for q in dependencies_first(p, project)}


def failed_gates(gates: Sequence[str], outcomes: Mapping[str, str]) -> list[FailedStep]:
    """The gates whose outcome is not ``passed`` (``soft_failed`` is not passed)."""
    return [FailedStep(k, outcomes.get(k, "")) for k in gates if outcomes.get(k, "") != PASSED]


def pins_for(p: Plan, project: str, versions: Mapping[str, Version | None]) -> dict[str, Pin]:
    """Build id and ref of every direct dependency of ``project`` that has a version."""
    out = {}
    for d in p.nodes[project].deps:
        v = versions.get(d)
        if v is not None and not p.nodes[d].opaque:
            out[d] = Pin(v.build.uuid, v.store_ref)
    return out


def manifest_path(project: str, label: str, variant: str, build_id: str) -> str:
    """Where the artifacts plugin files the manifest of a variant of a build."""
    return key_join(
        project, store_ref(label), "variants", variant, "builds", build_id, "manifest.json"
    )


# ---------------------------------------------------------------------------
# Reading the store and the build
# ---------------------------------------------------------------------------


def lookup_versions(s: Store, p: Plan, projects: Sequence[str]) -> dict[str, Version | None]:
    """The version of every project of ``projects`` that is not opaque; None when there is none."""
    return {q: None if p.nodes[q].opaque else find_version(s, q, p.nodes[q].key) for q in projects}


def obtain_plan(job: Job, decl: Declaration, fx: Effects) -> Plan:
    """The plan of this build: loaded (``SNAPDEP_PLAN``), or made once and stored (a root)."""
    commit = fx.git.head_commit()
    if job.plan_id is not None:
        return load_plan_for(fx.store, job.plan_id, job.project, commit)
    if job.repo is None:
        raise JobError("BUILDKITE_REPO is not a GitHub repository")
    root = Root(
        project=job.project,
        repo=job.repo,
        branch=job.branch,
        commit=commit,
        build_id=job.build.uuid,
        build_number=job.build.number,
        build_url=job.build.url,
    )
    plan = make_plan(root, decl, fx.git, fx.store, job.rebuild == REBUILD_ALL, fx.clock())
    return create_or_load_plan(fx.store, plan)


def build_plan(job: Job, fx: Effects) -> Plan:
    """The plan enter stored for this build (meta-data ``snapdep-plan``)."""
    plan_id = fx.agent.meta_data_get(PLAN)
    if not plan_id:
        raise JobError(f"meta-data {PLAN} is not set although {MODE} is {BUILD}")
    return load_plan_for(fx.store, plan_id, job.project, fx.git.head_commit())


def build_entry(job: Job, decl: Declaration | None) -> Pipeline:
    entry = pipeline_entry(decl, job.project)
    if entry is None:
        raise JobError(f"{PATH} has no entry for {job.project}, but the build is in {BUILD} mode")
    return entry


def build_gates(fx: Effects) -> tuple[str, list[str] | None]:
    """Own key and gate keys of a build in build mode; gate keys None when not uploaded."""
    key = fx.agent.meta_data_get(KEY)
    if not key:
        raise JobError(f"meta-data {KEY} is not set although {MODE} is {BUILD}")
    raw = fx.agent.meta_data_get(GATE_KEYS)
    return key, None if raw is None else list(json.loads(raw))


# ---------------------------------------------------------------------------
# Shared steps
# ---------------------------------------------------------------------------


def run_plain(text: str | None, entry: Pipeline | None, reason: str, fx: Effects) -> int:
    """Plain mode: the generated pipeline unchanged, a sentinel without dependencies."""
    fx.log(f"plain mode: {reason}")
    if text is None:
        text = fx.generator.run(generate_command(entry or Pipeline(slug="")))
    fx.agent.meta_data_set(MODE, PLAIN)
    fx.agent.pipeline_upload(text)
    fx.agent.pipeline_upload(dump_pipeline(steps_document([sentinel_step([])])))
    return 0


def upload_real_pipeline(
    job: Job,
    entry: Pipeline,
    p: Plan,
    doc: Mapping,
    versions: Mapping[str, Version | None],
    fx: Effects,
) -> int:
    """Section 5.3 steps 3 and 4: inject the dependencies' builds, upload with the sentinel."""
    partial = partial_reason(entry.gates, doc)
    if partial is not None:
        raise JobError(f"the generated pipeline is not a version build: {partial}")
    own = p.nodes[job.project]
    injected = inject_build_ids(doc, job.project, pins_for(p, job.project, versions), own.deps)
    gates = gate_keys(entry.gates, doc)
    fx.agent.meta_data_set(GATE_KEYS, json.dumps(gates))
    fx.agent.pipeline_upload(dump_pipeline(with_sentinel(injected, gates)))
    fx.log(f"uploaded the pipeline; the gate is {', '.join(gates) or 'empty'}")
    return 0


# ---------------------------------------------------------------------------
# enter (5.2)
# ---------------------------------------------------------------------------


def enter(job: Job, decl: Declaration | None, fx: Effects) -> int:
    """The first gate job: plain, reuse, a shared failure, or build (with triggers first)."""
    entry = pipeline_entry(decl, job.project)
    reason = plain_reason(job, entry)
    if reason is None and is_disabled(fx.store):
        reason = "snapdep is disabled in the store"
    if reason is not None or entry is None or decl is None:
        return run_plain(None, entry, reason or "no declaration", fx)

    command = generate_command(entry)
    text = fx.generator.run(command)
    doc = assign_keys(parse_pipeline(text))
    full = assign_keys(parse_pipeline(fx.generator.run(command, True))) if job.selection else None
    partial = partial_reason(entry.gates, doc, full)
    if partial is not None:
        return run_plain(text, entry, f"not a version build: {partial}", fx)

    plan = obtain_plan(job, decl, fx)
    own = plan.nodes[job.project]
    versions = lookup_versions(fx.store, plan, dependencies_first(plan, job.project))
    stale = stale_dependencies(plan, job.project, versions)
    forced = self_forced(job)
    actions = plan_actions(plan, job.project, versions, forced, stale)
    fx.agent.annotate(notes.plan_annotation(plan, job.project, actions), "info", notes.PLAN_CONTEXT)

    v = versions[job.project]
    if counts_as_built(v, plan, forced) and v is not None:
        fx.agent.meta_data_set(MODE, REUSED)
        fx.agent.pipeline_upload(dump_pipeline(steps_document([sentinel_step([])])))
        fx.agent.annotate(notes.reuse_annotation(job.project, v), "info", notes.CONTEXT)
        fx.log(f"reused build {v.build.number} ({v.build.url}) for {own.key}")
        return 0

    arrived = parse_arrived(fx.agent.meta_data_get(ARRIVED))
    if arrived is None:
        fx.log(f"meta-data {ARRIVED} is not set: is the shim in pipeline.py?")
    failed = shared_failure(list_failures(fx.store, job.project, own.key), arrived, job.build.uuid)
    if failed is not None:
        note = notes.shared_failure_text(notes.trace_failure(fx.store, plan, failed))
        fx.agent.annotate(notes.preformatted(note), "error", notes.CONTEXT)
        fx.log(f"{own.key} failed in build {failed.build.number} ({failed.build.url})")
        return 1

    plugin = own_plugin_reference(job.plugins) if stale else ""
    write_holder(fx.store, holder_record(job.project, own.key, plan.id, job.build, fx.clock()))
    fx.agent.meta_data_set(MODE, BUILD)
    fx.agent.meta_data_set(KEY, own.key)
    fx.agent.meta_data_set(PLAN, plan.id)

    if stale:
        triggers = trigger_steps(plan, job.project, stale)
        keys = [t["key"] for t in triggers]
        fx.agent.pipeline_upload(
            dump_pipeline(steps_document([*triggers, generate_step(plugin, keys)]))
        )
        fx.log(f"building {', '.join(stale)} first")
        return 0
    return upload_real_pipeline(job, entry, plan, doc, versions, fx)


# ---------------------------------------------------------------------------
# generate (5.3)
# ---------------------------------------------------------------------------


def generate(job: Job, decl: Declaration | None, fx: Effects) -> int:
    """After the triggers: fail when a dependency has no version, else upload the pipeline."""
    entry = build_entry(job, decl)
    plan = build_plan(job, fx)
    own = plan.nodes[job.project]
    versions = lookup_versions(fx.store, plan, list(own.deps))
    missing = stale_dependencies(plan, job.project, versions)
    if missing:
        causes = [Cause(d, plan.nodes[d].key) for d in missing]
        f = failure_record(job.project, own.key, job.build, [], causes, fx.clock())
        write_failure(fx.store, f)
        traced = [notes.trace_missing(fx.store, plan, c) for c in causes]
        text = notes.dependency_failure_text(job.project, job.build.number, traced)
        fx.agent.annotate(notes.preformatted(text), "error", notes.CONTEXT)
        fx.log(f"no version for {', '.join(missing)}")
        return 1
    doc = assign_keys(parse_pipeline(fx.generator.run(generate_command(entry))))
    return upload_real_pipeline(job, entry, plan, doc, versions, fx)


# ---------------------------------------------------------------------------
# fail (5.4)
# ---------------------------------------------------------------------------


def fail(job: Job, decl: Declaration | None, fx: Effects) -> int:
    """Records the failure of this build when a gate did not pass. Always exits 0."""
    mode = fx.agent.meta_data_get(MODE)
    if mode != BUILD:
        fx.log(f"mode {mode or 'unset'}: nothing to record")
        return 0
    key, gates = build_gates(fx)
    if gates is None:
        fx.log("the pipeline was never uploaded: nothing to record")
        return 0
    failed = failed_gates(gates, {k: fx.agent.step_outcome(k) for k in gates})
    if not failed:
        fx.log("every gate passed: nothing to record")
        return 0
    v = find_version(fx.store, job.project, key)
    if v is not None and v.build.uuid == job.build.uuid:
        fx.log(f"this build is already the version of {key}: nothing to record")
        return 0
    f = failure_record(job.project, key, job.build, failed, [], fx.clock())
    write_failure(fx.store, f)
    fx.agent.annotate(
        notes.preformatted(notes.failed_steps_text(job.project, key, f)), "error", notes.CONTEXT
    )
    fx.log(f"recorded the failure of {key}: {', '.join(s.key for s in failed)}")
    return 0


# ---------------------------------------------------------------------------
# seal (5.5)
# ---------------------------------------------------------------------------


def missing_variants(s: Store, job: Job, label: str, produces: Sequence[str]) -> list[str]:
    """The variants in ``produces`` that have no manifest under this build."""
    return [
        v for v in produces if s.get(manifest_path(job.project, label, v, job.build.uuid)) is None
    ]


def seal(job: Job, decl: Declaration | None, fx: Effects) -> int:
    """Records this build as the version of its key when every gate passed."""
    mode = fx.agent.meta_data_get(MODE)
    if mode != BUILD:
        fx.log(f"mode {mode or 'unset'}: nothing to seal")
        return 0
    key, gates = build_gates(fx)
    if gates is None:
        raise JobError(f"meta-data {GATE_KEYS} is not set: the pipeline was never uploaded")
    if not gates:
        raise JobError(f"meta-data {GATE_KEYS} is empty: a build without gates is not a version")
    failed = failed_gates(gates, {k: fx.agent.step_outcome(k) for k in gates})
    if failed:
        fx.log(f"not a version: {', '.join(f'{s.key} {s.outcome}' for s in failed)}")
        return 0
    entry = build_entry(job, decl)
    plan = build_plan(job, fx)
    own = plan.nodes[job.project]
    if own.key != key:
        raise JobError(f"meta-data {KEY}={key} but the plan has {own.key}")
    absent = missing_variants(fx.store, job, own.label, entry.produces)
    if absent:
        raise JobError(f"not a version: no manifest of {', '.join(absent)} in this build")
    deps = {d: plan.nodes[d].key for d in own.deps}
    v = version_record(job.project, own, deps, job.build, gates, fx.clock())
    forced = self_forced(job) or plan.rebuild == REBUILD_ALL
    standing = record_version(fx.store, v, force=forced)
    if standing.build.uuid != job.build.uuid:
        fx.log(f"{key} was recorded first by build {standing.build.number} ({standing.build.url})")
        return 0
    fx.log(f"recorded build {job.build.number} as the version of {key}")
    return 0
