"""The declaration: ``.buildkite/snapshot.yml`` of an adopting repository.

Specification section 2. The file names, per pipeline slug built from the
repository, the direct dependencies of that pipeline and what makes one of its
builds a version. It is parsed without running any code of the repository.

Every validation error is a ``DeclarationError`` whose message starts with the
file and the field, for example::

    .buildkite/snapshot.yml: pipelines.rest.depends[1].repo: expected "owner/name", got 'duck'
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

PATH = ".buildkite/snapshot.yml"
SCHEMA = 1

DEFAULT_GENERATE = (
    "python3 -m venv .env"
    " && .env/bin/python3 -m pip install -q -r .buildkite/requirements.txt"
    " && SNAPDEP_PHASE=generate .env/bin/python3 .buildkite/pipeline.py"
)

_TOP_FIELDS = ("schema", "lines", "pins", "pipelines")
_PIPELINE_FIELDS = ("depends", "gates", "produces", "generate")
_DEPENDENCY_FIELDS = ("project", "repo")

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")
_STEP_KEY = re.compile(r"^\S+$")
_REF = re.compile(r"^[^\s:~^?*\[\\]+$")


class DeclarationError(ValueError):
    """The declaration file cannot be read or does not validate."""


@dataclass(frozen=True)
class Dependency:
    """A direct dependency: the pipeline slug ``project`` built from GitHub ``repo``."""

    project: str
    repo: str


@dataclass(frozen=True)
class Pipeline:
    """The entry of one pipeline slug.

    ``gates``: step keys that must pass for a build to be a version; empty means
    every generated command step. ``produces``: variants that must be in the store
    for a version. ``generate``: the command that prints the pipeline; empty means
    ``DEFAULT_GENERATE`` (use ``generate_command``).
    """

    slug: str
    depends: tuple[Dependency, ...] = ()
    gates: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    generate: str = ""


@dataclass(frozen=True)
class Declaration:
    """A parsed and validated declaration file.

    ``path`` is where it was read from, for messages. ``pins`` maps a project to
    a git branch; only the root's pins are used.
    """

    path: str
    lines: tuple[str, ...]
    pins: Mapping[str, str] = field(default_factory=dict)
    pipelines: Mapping[str, Pipeline] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Field validators. Each takes the file path and the field path for messages.
# ---------------------------------------------------------------------------


def _fail(path: str, where: str, msg: str) -> DeclarationError:
    return DeclarationError(f"{path}: {where}: {msg}")


def _mapping(path: str, where: str, v: Any) -> Mapping[str, Any]:
    if not isinstance(v, dict):
        raise _fail(path, where, f"expected a mapping, got {type(v).__name__}")
    for k in v:
        if not isinstance(k, str):
            raise _fail(path, where, f"expected string keys, got {k!r}")
    return v


def _known_fields(path: str, where: str, m: Mapping[str, Any], known: tuple[str, ...]) -> None:
    unknown = sorted(set(m) - set(known))
    if unknown:
        raise _fail(
            path, where, f"unknown field(s) {', '.join(unknown)}; known: {', '.join(known)}"
        )


def _string(path: str, where: str, v: Any, pattern: re.Pattern[str], expected: str) -> str:
    if not isinstance(v, str) or not pattern.match(v):
        raise _fail(path, where, f"expected {expected}, got {v!r}")
    return v


def _strings(path: str, where: str, v: Any, pattern: re.Pattern[str], expected: str) -> tuple:
    if v is None:
        return ()
    if not isinstance(v, list):
        raise _fail(path, where, f"expected a list, got {type(v).__name__}")
    xs = tuple(_string(path, f"{where}[{i}]", x, pattern, expected) for i, x in enumerate(v))
    dupes = sorted({x for x in xs if xs.count(x) > 1})
    if dupes:
        raise _fail(path, where, f"duplicate entries {', '.join(dupes)}")
    return xs


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _parse_dependency(path: str, where: str, v: Any) -> Dependency:
    m = _mapping(path, where, v)
    _known_fields(path, where, m, _DEPENDENCY_FIELDS)
    for name in _DEPENDENCY_FIELDS:
        if name not in m:
            raise _fail(path, f"{where}.{name}", "missing")
    return Dependency(
        project=_string(path, f"{where}.project", m["project"], _SLUG, "a pipeline slug"),
        repo=_string(path, f"{where}.repo", m["repo"], _REPO, '"owner/name"'),
    )


def _parse_depends(path: str, where: str, slug: str, v: Any) -> tuple[Dependency, ...]:
    if v is None:
        return ()
    if not isinstance(v, list):
        raise _fail(path, where, f"expected a list, got {type(v).__name__}")
    deps = tuple(_parse_dependency(path, f"{where}[{i}]", x) for i, x in enumerate(v))
    seen: set[str] = set()
    for i, d in enumerate(deps):
        if d.project == slug:
            raise _fail(
                path, f"{where}[{i}].project", "a pipeline does not declare its own project"
            )
        if d.project in seen:
            raise _fail(path, f"{where}[{i}].project", f"{d.project} is declared twice")
        seen.add(d.project)
    return deps


def _parse_pipeline(path: str, slug: str, v: Any) -> Pipeline:
    where = f"pipelines.{slug}"
    m = _mapping(path, where, {} if v is None else v)
    _known_fields(path, where, m, _PIPELINE_FIELDS)
    generate = m.get("generate") or ""
    if not isinstance(generate, str):
        raise _fail(path, f"{where}.generate", f"expected a string, got {generate!r}")
    return Pipeline(
        slug=slug,
        depends=_parse_depends(path, f"{where}.depends", slug, m.get("depends")),
        gates=_strings(path, f"{where}.gates", m.get("gates"), _STEP_KEY, "a step key"),
        produces=_strings(path, f"{where}.produces", m.get("produces"), _SLUG, "a variant"),
        generate=generate,
    )


def _parse_pins(path: str, v: Any) -> Mapping[str, str]:
    m = _mapping(path, "pins", {} if v is None else v)
    for project, ref in m.items():
        _string(path, "pins", project, _SLUG, "a pipeline slug as key")
        _string(path, f"pins.{project}", ref, _REF, "a git branch name")
    return dict(m)


def _parse_lines(path: str, v: Any) -> tuple[str, ...]:
    if v is None:
        raise _fail(path, "lines", "missing")
    lines = _strings(path, "lines", v, _REF, "a git branch name")
    if not lines:
        raise _fail(path, "lines", "expected at least one release line")
    return lines


def _parse_pipelines(path: str, v: Any) -> Mapping[str, Pipeline]:
    m = _mapping(path, "pipelines", {} if v is None else v)
    for slug in m:
        _string(path, "pipelines", slug, _SLUG, "a pipeline slug as key")
    return {slug: _parse_pipeline(path, slug, entry) for slug, entry in m.items()}


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def parse_declaration(text: str, path: str = PATH) -> Declaration:
    """Parse and validate the declaration ``text`` read from ``path``.

    Raises ``DeclarationError`` naming ``path`` and the offending field.
    Unknown fields are errors, so that a misspelt field is not silently ignored.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DeclarationError(f"{path}: not valid YAML: {exc}") from exc
    m = _mapping(path, "(top level)", doc)
    _known_fields(path, "(top level)", m, _TOP_FIELDS)
    if m.get("schema") != SCHEMA:
        raise _fail(path, "schema", f"expected {SCHEMA}, got {m.get('schema')!r}")
    return Declaration(
        path=path,
        lines=_parse_lines(path, m.get("lines")),
        pins=_parse_pins(path, m.get("pins")),
        pipelines=_parse_pipelines(path, m.get("pipelines")),
    )


def read_declaration(path: str = PATH) -> Declaration | None:
    """Read and parse the declaration file at ``path``; None when it does not exist."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    return parse_declaration(text, path)


def pipeline_entry(d: Declaration | None, slug: str) -> Pipeline | None:
    """Return the entry of pipeline ``slug``; None when ``d`` is None or has no entry.

    None means the pipeline is not snapshot-managed (opaque).
    """
    if d is None:
        return None
    return d.pipelines.get(slug)


def generate_command(p: Pipeline) -> str:
    """Return the command that prints the pipeline of ``p``: its own or the default."""
    return p.generate or DEFAULT_GENERATE
