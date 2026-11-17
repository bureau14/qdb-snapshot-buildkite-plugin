"""The records under ``_snapdep/v1/`` and how they are read and written.

Specification section 4. Every record is JSON with ``"schema": 1``. Records are
built with the typed constructors here, encoded with ``encode`` and decoded
with the ``*_from_dict`` readers, which reject records that miss a field.

Layout (keys relative to the store root, see ``snapdep.store``)::

    _snapdep/v1/plans/<root build uuid>.json                  created once
    _snapdep/v1/versions/<project>/<key>.json                 created once; forced overwrite
    _snapdep/v1/failures/<project>/<key>/<build uuid>.json    plain put
    _snapdep/v1/holders/<project>/<key>/<build uuid>.json     plain put
    _snapdep/v1/disabled                                      present: plain mode everywhere
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_json
from .store import Store, key_join

SCHEMA = 1
ROOT = "_snapdep/v1"
REBUILD_NONE = "none"
REBUILD_ALL = "all"


class RecordError(RuntimeError):
    """A record is missing where it must exist, or does not decode."""


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def plan_path(plan_id: str) -> str:
    return key_join(ROOT, "plans", f"{plan_id}.json")


def version_path(project: str, key: str) -> str:
    return key_join(ROOT, "versions", project, f"{key}.json")


def failures_prefix(project: str, key: str) -> str:
    return key_join(ROOT, "failures", project, key) + "/"


def failure_path(project: str, key: str, build_uuid: str) -> str:
    return failures_prefix(project, key) + f"{build_uuid}.json"


def holder_path(project: str, key: str, build_uuid: str) -> str:
    return key_join(ROOT, "holders", project, key, f"{build_uuid}.json")


DISABLED_PATH = key_join(ROOT, "disabled")


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def iso_utc(epoch: float) -> str:
    """``epoch`` seconds as UTC ISO 8601 with a "Z", to the millisecond."""
    t = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def epoch_from_iso(s: str) -> float:
    """Epoch seconds of a UTC ISO 8601 time written by ``iso_utc``."""
    try:
        t = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise RecordError(f"{s!r}: not a UTC time of the form 2026-01-31T12:00:00.000Z") from exc
    return t.replace(tzinfo=datetime.timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildRef:
    """A Buildkite build: its uuid, number, web URL and pipeline slug."""

    uuid: str
    number: int
    url: str
    pipeline: str


@dataclass(frozen=True)
class RootInfo:
    """The root build of a plan."""

    project: str
    build_number: int
    build_url: str
    branch: str


@dataclass(frozen=True)
class Node:
    """One project of a plan.

    ``ref``: the branch it was resolved on. ``label``: the branch name a build
    of it runs under. ``deps``: its direct dependencies (projects of the same
    plan); empty for an opaque node.
    """

    repo: str
    ref: str
    commit: str
    key: str
    label: str
    opaque: bool
    deps: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    """The snapshot of a root build's dependency closure; ``id`` is the root build uuid."""

    id: str
    root: RootInfo
    context: str
    rebuild: str
    created_at: str
    nodes: Mapping[str, Node] = field(default_factory=dict)


@dataclass(frozen=True)
class Version:
    """The passed build of a key. ``deps`` maps each direct dependency to its key."""

    project: str
    key: str
    commit: str
    label: str
    store_ref: str
    deps: Mapping[str, str]
    build: BuildRef
    gate_keys: tuple[str, ...]
    sealed_at: str


@dataclass(frozen=True)
class FailedStep:
    key: str
    outcome: str


@dataclass(frozen=True)
class Cause:
    """A dependency that had no version when it was needed."""

    project: str
    key: str


@dataclass(frozen=True)
class Failure:
    """A build of a key that did not become a version.

    ``recorded_at`` is UTC epoch seconds, ``recorded_at_iso`` the same instant
    as ISO 8601.
    """

    project: str
    key: str
    build: BuildRef
    failed_steps: tuple[FailedStep, ...]
    caused_by: tuple[Cause, ...]
    recorded_at: float
    recorded_at_iso: str


@dataclass(frozen=True)
class Holder:
    """A build that started the work for a key, under plan ``plan``."""

    project: str
    key: str
    plan: str
    build: BuildRef
    held_at: str


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------


def store_ref(label: str) -> str:
    """The ref under which the artifacts of a build labelled ``label`` are filed."""
    return f"refs/heads/{label}"


def version_record(
    project: str,
    n: Node,
    dep_keys: Mapping[str, str],
    b: BuildRef,
    gate_keys: list[str],
    now: float,
) -> Version:
    """The version of ``project`` as node ``n`` of a plan, built by ``b``."""
    return Version(
        project=project,
        key=n.key,
        commit=n.commit,
        label=n.label,
        store_ref=store_ref(n.label),
        deps=dict(dep_keys),
        build=b,
        gate_keys=tuple(gate_keys),
        sealed_at=iso_utc(now),
    )


def failure_record(
    project: str,
    key: str,
    b: BuildRef,
    failed_steps: list[FailedStep],
    caused_by: list[Cause],
    now: float,
) -> Failure:
    return Failure(
        project=project,
        key=key,
        build=b,
        failed_steps=tuple(failed_steps),
        caused_by=tuple(caused_by),
        recorded_at=round(now, 3),
        recorded_at_iso=iso_utc(now),
    )


def holder_record(project: str, key: str, plan_id: str, b: BuildRef, now: float) -> Holder:
    return Holder(project=project, key=key, plan=plan_id, build=b, held_at=iso_utc(now))


# ---------------------------------------------------------------------------
# Encoding and decoding
# ---------------------------------------------------------------------------


def _field(d: Mapping[str, Any], name: str, where: str) -> Any:
    if name not in d:
        raise RecordError(f"{where}: missing field {name!r}")
    return d[name]


def _check_schema(d: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(d, dict):
        raise RecordError(f"{where}: expected a JSON object")
    if d.get("schema") != SCHEMA:
        raise RecordError(f"{where}: unsupported schema {d.get('schema')!r}")
    return d


def build_to_dict(b: BuildRef) -> dict[str, Any]:
    return {"uuid": b.uuid, "number": b.number, "url": b.url, "pipeline": b.pipeline}


def build_from_dict(d: Mapping[str, Any], where: str) -> BuildRef:
    return BuildRef(
        uuid=_field(d, "uuid", where),
        number=int(_field(d, "number", where)),
        url=_field(d, "url", where),
        pipeline=_field(d, "pipeline", where),
    )


def node_to_dict(n: Node) -> dict[str, Any]:
    return {
        "repo": n.repo,
        "ref": n.ref,
        "commit": n.commit,
        "key": n.key,
        "label": n.label,
        "opaque": n.opaque,
        "deps": list(n.deps),
    }


def node_from_dict(d: Mapping[str, Any], where: str) -> Node:
    return Node(
        repo=_field(d, "repo", where),
        ref=_field(d, "ref", where),
        commit=_field(d, "commit", where),
        key=_field(d, "key", where),
        label=_field(d, "label", where),
        opaque=bool(_field(d, "opaque", where)),
        deps=tuple(_field(d, "deps", where)),
    )


def plan_to_dict(p: Plan) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "id": p.id,
        "root": {
            "project": p.root.project,
            "build_number": p.root.build_number,
            "build_url": p.root.build_url,
            "branch": p.root.branch,
        },
        "context": p.context,
        "rebuild": p.rebuild,
        "created_at": p.created_at,
        "nodes": {name: node_to_dict(n) for name, n in sorted(p.nodes.items())},
    }


def plan_from_dict(d: Any, where: str = "plan") -> Plan:
    d = _check_schema(d, where)
    r = _field(d, "root", where)
    nodes = _field(d, "nodes", where)
    return Plan(
        id=_field(d, "id", where),
        root=RootInfo(
            project=_field(r, "project", f"{where}.root"),
            build_number=int(_field(r, "build_number", f"{where}.root")),
            build_url=_field(r, "build_url", f"{where}.root"),
            branch=_field(r, "branch", f"{where}.root"),
        ),
        context=_field(d, "context", where),
        rebuild=_field(d, "rebuild", where),
        created_at=_field(d, "created_at", where),
        nodes={k: node_from_dict(v, f"{where}.nodes.{k}") for k, v in nodes.items()},
    )


def version_to_dict(v: Version) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "project": v.project,
        "key": v.key,
        "commit": v.commit,
        "label": v.label,
        "store_ref": v.store_ref,
        "deps": dict(v.deps),
        "build": build_to_dict(v.build),
        "gate_keys": list(v.gate_keys),
        "sealed_at": v.sealed_at,
    }


def version_from_dict(d: Any, where: str = "version") -> Version:
    d = _check_schema(d, where)
    return Version(
        project=_field(d, "project", where),
        key=_field(d, "key", where),
        commit=_field(d, "commit", where),
        label=_field(d, "label", where),
        store_ref=_field(d, "store_ref", where),
        deps=dict(_field(d, "deps", where)),
        build=build_from_dict(_field(d, "build", where), f"{where}.build"),
        gate_keys=tuple(_field(d, "gate_keys", where)),
        sealed_at=_field(d, "sealed_at", where),
    )


def failure_to_dict(f: Failure) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "project": f.project,
        "key": f.key,
        "build": build_to_dict(f.build),
        "failed_steps": [{"key": s.key, "outcome": s.outcome} for s in f.failed_steps],
        "caused_by": [{"project": c.project, "key": c.key} for c in f.caused_by],
        "recorded_at": f.recorded_at,
        "recorded_at_iso": f.recorded_at_iso,
    }


def failure_from_dict(d: Any, where: str = "failure") -> Failure:
    d = _check_schema(d, where)
    steps = _field(d, "failed_steps", where)
    causes = _field(d, "caused_by", where)
    return Failure(
        project=_field(d, "project", where),
        key=_field(d, "key", where),
        build=build_from_dict(_field(d, "build", where), f"{where}.build"),
        failed_steps=tuple(
            FailedStep(_field(s, "key", where), _field(s, "outcome", where)) for s in steps
        ),
        caused_by=tuple(
            Cause(_field(c, "project", where), _field(c, "key", where)) for c in causes
        ),
        recorded_at=float(_field(d, "recorded_at", where)),
        recorded_at_iso=_field(d, "recorded_at_iso", where),
    )


def holder_to_dict(h: Holder) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "project": h.project,
        "key": h.key,
        "plan": h.plan,
        "build": build_to_dict(h.build),
        "held_at": h.held_at,
    }


def holder_from_dict(d: Any, where: str = "holder") -> Holder:
    d = _check_schema(d, where)
    return Holder(
        project=_field(d, "project", where),
        key=_field(d, "key", where),
        plan=_field(d, "plan", where),
        build=build_from_dict(_field(d, "build", where), f"{where}.build"),
        held_at=_field(d, "held_at", where),
    )


def encode(d: Mapping[str, Any]) -> bytes:
    """Bytes of a record dictionary (canonical JSON)."""
    return canonical_json(d)


def decode(body: bytes, where: str) -> Any:
    """The JSON value in ``body``; ``RecordError`` naming ``where`` when it is not JSON."""
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecordError(f"{where}: not JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Reading and writing through a Store
# ---------------------------------------------------------------------------


def load_plan(s: Store, plan_id: str) -> Plan:
    """The plan ``plan_id``; ``RecordError`` when it does not exist."""
    path = plan_path(plan_id)
    o = s.get(path)
    if o is None:
        raise RecordError(f"{path}: plan {plan_id} does not exist")
    return plan_from_dict(decode(o.body, path), path)


def create_or_load_plan(s: Store, p: Plan) -> Plan:
    """Store ``p`` once and return the stored plan.

    When a plan with this id exists already (a retried enter job), that plan is
    returned and ``p`` is discarded, so every job of a build sees one snapshot.
    """
    if s.put(plan_path(p.id), encode(plan_to_dict(p)), if_none_match=True):
        return p
    return load_plan(s, p.id)


def find_version(s: Store, project: str, key: str) -> Version | None:
    """The version of ``key`` of ``project``, or None when the key has none.

    Raises (``StoreError``) when the lookup cannot be answered.
    """
    path = version_path(project, key)
    o = s.get(path)
    return None if o is None else version_from_dict(decode(o.body, path), path)


def record_version(s: Store, v: Version, force: bool = False) -> Version:
    """Record ``v`` as the version of its key and return the version that stands.

    Without ``force`` the record is created once: when another build recorded
    the key first, its version is returned (compare ``.build.uuid`` to see who
    won). A retried put that had in fact succeeded also returns ``v``'s own
    record this way. With ``force`` the record is overwritten.
    """
    path = version_path(v.project, v.key)
    body = encode(version_to_dict(v))
    if force:
        s.put(path, body)
        return v
    if s.put(path, body, if_none_match=True):
        return v
    standing = find_version(s, v.project, v.key)
    if standing is None:
        raise RecordError(f"{path}: create was refused but the record does not exist")
    return standing


def write_failure(s: Store, f: Failure) -> None:
    """Store failure record ``f`` (one per build and key; a rewrite replaces it)."""
    s.put(failure_path(f.project, f.key, f.build.uuid), encode(failure_to_dict(f)))


def list_failures(s: Store, project: str, key: str) -> list[Failure]:
    """Every failure record of ``key`` of ``project``, oldest first."""
    out = []
    for path in s.list_keys(failures_prefix(project, key)):
        o = s.get(path)
        if o is None:
            raise RecordError(f"{path}: listed but not readable")
        out.append(failure_from_dict(decode(o.body, path), path))
    return sorted(out, key=lambda f: (f.recorded_at, f.build.uuid))


def write_holder(s: Store, h: Holder) -> None:
    """Store holder record ``h``."""
    s.put(holder_path(h.project, h.key, h.build.uuid), encode(holder_to_dict(h)))


def is_disabled(s: Store) -> bool:
    """True when a person switched snapdep off (the ``disabled`` object exists)."""
    return s.get(DISABLED_PATH) is not None
