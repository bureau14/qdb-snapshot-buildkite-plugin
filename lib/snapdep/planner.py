"""The planner: one snapshot of a root build's dependency closure.

Specification section 3. ``make_plan`` walks the closure of the root's pipeline
breadth first, choosing for every dependency a branch (``choose_ref``), the
commit at its head, and its declaration at that commit; then computes keys and
labels bottom up. It talks to the outside only through a ``Git`` and a
``Store`` (the store is read for the pointers of opaque nodes only) and writes
nothing. ``load_plan_for`` is the read side for a build started by a trigger
step.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable

from .canonical import opaque_key, version_key
from .declaration import PATH, Declaration, Dependency, parse_declaration, pipeline_entry
from .git import DEFAULT_LINE, Git
from .records import REBUILD_ALL, REBUILD_NONE, Node, Plan, RootInfo, iso_utc, load_plan
from .store import Store, key_join

POINTER = "LATEST_SUCCESSFUL"


class PlanError(RuntimeError):
    """The closure cannot be planned, or a loaded plan does not fit this build."""


@dataclass(frozen=True)
class Root:
    """The root build: its pipeline, GitHub repository, branch (the context) and commit."""

    project: str
    repo: str
    branch: str
    commit: str
    build_id: str
    build_number: int
    build_url: str


# ---------------------------------------------------------------------------
# Refs: which branch of a dependency the plan takes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """A project after the walk: where it is and what it depends on.

    ``lines`` are the release lines of its repository from its declaration; None
    for an opaque node. ``path`` is how the walk first reached it, root first.
    """

    project: str
    repo: str
    ref: str
    commit: str
    lines: tuple[str, ...] | None
    depends: tuple[Dependency, ...]
    path: tuple[str, ...]

    @property
    def opaque(self) -> bool:
        return self.lines is None


Heads = Callable[[str, str], "str | None"]


def memoised_heads(git: Git, seed: Mapping[tuple[str, str], str]) -> Heads:
    """``git.head`` asked at most once per repository and ref; ``seed`` answers first."""
    memo: dict[tuple[str, str], str | None] = dict(seed)

    def head(repo: str, ref: str) -> str | None:
        if (repo, ref) not in memo:
            memo[(repo, ref)] = git.head(repo, ref)
        return memo[(repo, ref)]

    return head


def memoised(f: Callable[[], str]) -> Callable[[], str]:
    """``f`` evaluated once, on first call."""
    box: list[str] = []

    def once() -> str:
        if not box:
            box.append(f())
        return box[0]

    return once


def choose_ref(
    d: Dependency,
    pins: Mapping[str, str],
    context: str,
    context_line: Callable[[], str],
    head: Heads,
) -> tuple[str, str]:
    """The branch and commit the plan takes for dependency ``d``.

    A pin wins; else the context branch when ``d.repo`` has it; else the
    context's release line; else ``master`` when the repository lacks that line.
    """
    if d.project in pins:
        ref = pins[d.project]
        commit = head(d.repo, ref)
        if commit is None:
            raise PlanError(f"pins.{d.project}: branch {ref} does not exist in {d.repo}")
        return ref, commit
    commit = head(d.repo, context)
    if commit is not None:
        return context, commit
    line = context_line()
    for ref in dict.fromkeys([line, DEFAULT_LINE]):
        commit = head(d.repo, ref)
        if commit is not None:
            return ref, commit
    raise PlanError(f"{d.project}: {d.repo} has neither {context}, {line} nor {DEFAULT_LINE}")


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def _show_path(p: Sequence[str]) -> str:
    return " -> ".join(p)


def read_dependency(git: Git, repo: str, commit: str) -> Declaration | None:
    """The declaration of ``repo`` at ``commit``; None when the file does not exist."""
    text = git.read_file(repo, commit, PATH)
    if text is None:
        return None
    return parse_declaration(text, f"{repo}@{commit[:12]}:{PATH}")


def _check_same_repo(seen: Resolved, d: Dependency, path: tuple[str, ...]) -> None:
    if seen.repo != d.repo:
        raise PlanError(
            f"{d.project} is declared in two repositories: {seen.repo} via "
            f"{_show_path(seen.path)}, and {d.repo} via {_show_path(path)}"
        )


def _check_same_commit(seen: Resolved, d: Dependency, commit: str, path: tuple[str, ...]) -> None:
    if seen.commit != commit:
        raise PlanError(
            f"{d.project} is reached at two commits: {seen.repo}@{seen.commit} via "
            f"{_show_path(seen.path)}, and {d.repo}@{commit} via {_show_path(path)}"
        )


def walk(
    root: Root, decl: Declaration, git: Git, head: Heads, context_line: Callable[[], str]
) -> dict[str, Resolved]:
    """Resolve every project of the root's closure, breadth first."""
    entry = pipeline_entry(decl, root.project)
    if entry is None:
        raise PlanError(f"{decl.path}: pipelines.{root.project}: missing; not snapshot-managed")
    if root.project in decl.pins:
        raise PlanError(f"{decl.path}: pins.{root.project}: the root's own project is not pinned")
    first = Resolved(
        root.project,
        root.repo,
        root.branch,
        root.commit,
        decl.lines,
        entry.depends,
        (root.project,),
    )
    out = {root.project: first}
    queue = [first]
    while queue:
        n = queue.pop(0)
        for d in n.depends:
            path = (*n.path, d.project)
            if d.project in out:
                _check_same_repo(out[d.project], d, path)
            ref, commit = choose_ref(d, decl.pins, root.branch, context_line, head)
            if d.project in out:
                _check_same_commit(out[d.project], d, commit, path)
                continue
            dep_decl = read_dependency(git, d.repo, commit)
            dep_entry = pipeline_entry(dep_decl, d.project)
            r = Resolved(
                project=d.project,
                repo=d.repo,
                ref=ref,
                commit=commit,
                lines=None if dep_entry is None or dep_decl is None else dep_decl.lines,
                depends=() if dep_entry is None else dep_entry.depends,
                path=path,
            )
            out[d.project] = r
            queue.append(r)
    return out


# ---------------------------------------------------------------------------
# Bottom up: keys and labels
# ---------------------------------------------------------------------------


def bottom_up(nodes: Mapping[str, Resolved], start: str) -> list[str]:
    """Every project reachable from ``start``, each after its dependencies.

    Raises ``PlanError`` naming the cycle when there is one.
    """
    order: list[str] = []
    done: set[str] = set()

    def visit(p: str, stack: tuple[str, ...]) -> None:
        if p in done:
            return
        if p in stack:
            raise PlanError(f"dependency cycle: {_show_path((*stack[stack.index(p) :], p))}")
        for d in nodes[p].depends:
            visit(d.project, (*stack, p))
        done.add(p)
        order.append(p)

    visit(start, ())
    return order


def read_pointers(s: Store, project: str, ref: str) -> dict[str, str]:
    """Variant -> build id of every ``LATEST_SUCCESSFUL`` pointer of ``project`` on ``ref``."""
    prefix = key_join(project, "refs/heads", ref, "variants") + "/"
    out = {}
    for variant in s.list_children(prefix):
        o = s.get(prefix + key_join(variant, POINTER))
        if o is not None:
            out[variant] = o.body.decode("utf-8").strip()
    return out


def compute_keys(nodes: Mapping[str, Resolved], order: Sequence[str], s: Store) -> dict[str, str]:
    """The key of every project in ``order`` (dependencies first)."""
    keys: dict[str, str] = {}
    for p in order:
        n = nodes[p]
        if n.opaque:
            keys[p] = opaque_key(read_pointers(s, p, n.ref))
        else:
            keys[p] = version_key(p, n.commit, {d.project: keys[d.project] for d in n.depends})
    return keys


def is_release_line(n: Resolved, root_lines: Sequence[str]) -> bool:
    """Whether ``n`` was resolved on a release line of its repository.

    An opaque node has no declared lines; the root's lines stand in for them.
    """
    return n.ref in (root_lines if n.lines is None else n.lines)


def compute_labels(
    nodes: Mapping[str, Resolved], order: Sequence[str], context: str, root_lines: Sequence[str]
) -> dict[str, str]:
    """The branch name each project of ``order`` (dependencies first) is built under.

    The context branch when anything in its closure, itself included, is off a
    release line; otherwise its own ref.
    """
    off_line: dict[str, bool] = {}
    labels: dict[str, str] = {}
    for p in order:
        n = nodes[p]
        off = not is_release_line(n, root_lines) or any(off_line[d.project] for d in n.depends)
        off_line[p] = off
        labels[p] = context if off else n.ref
    return labels


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def make_plan(
    root: Root, decl: Declaration, git: Git, s: Store, rebuild_all: bool, now: float
) -> Plan:
    """The plan of ``root`` from its declaration ``decl``; nothing is written.

    The root's own repository and branch resolve to the root's commit, so every
    project built from the root's repository on the context branch is planned
    at the commit being built.
    """
    head = memoised_heads(git, {(root.repo, root.branch): root.commit})
    line = memoised(lambda: git.release_line(root.repo, root.branch, decl.lines))
    nodes = walk(root, decl, git, head, line)
    order = bottom_up(nodes, root.project)
    keys = compute_keys(nodes, order, s)
    labels = compute_labels(nodes, order, root.branch, decl.lines)
    return Plan(
        id=root.build_id,
        root=RootInfo(root.project, root.build_number, root.build_url, root.branch),
        context=root.branch,
        rebuild=REBUILD_ALL if rebuild_all else REBUILD_NONE,
        created_at=iso_utc(now),
        nodes={
            p: Node(
                repo=nodes[p].repo,
                ref=nodes[p].ref,
                commit=nodes[p].commit,
                key=keys[p],
                label=labels[p],
                opaque=nodes[p].opaque,
                deps=tuple(d.project for d in nodes[p].depends),
            )
            for p in order
        },
    )


def load_plan_for(s: Store, plan_id: str, project: str, commit: str) -> Plan:
    """The plan ``plan_id`` for a build of ``project`` at ``commit`` started by a trigger.

    Raises ``PlanError`` when the plan has no node for ``project`` or plans it
    at another commit; ``RecordError`` when the plan does not exist.
    """
    p = load_plan(s, plan_id)
    n = p.nodes.get(project)
    if n is None:
        raise PlanError(f"plan {plan_id} has no node {project}")
    if n.commit != commit:
        raise PlanError(f"plan {plan_id} plans {project} at {n.commit}, this build is at {commit}")
    return p


def dependencies_first(p: Plan, project: str) -> list[str]:
    """Every project of ``project``'s closure in plan ``p``, itself last, each after its deps."""
    order: list[str] = []

    def visit(q: str) -> None:
        if q in order:
            return
        for d in p.nodes[q].deps:
            visit(d)
        order.append(q)

    visit(project)
    return order
