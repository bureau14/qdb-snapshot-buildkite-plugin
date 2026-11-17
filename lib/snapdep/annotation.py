"""What the step commands tell people: the annotations of a build.

Specification sections 5.2 (the plan, reuse, a shared failure), 5.4 (failed
steps) and 5.6 (a dependency without a version). Every annotation is rendered
by a pure function from records; the only store access is ``trace_missing``,
which follows ``caused_by`` down the failure records, preferring at each
level the failure recorded under the plan of the build that asks.

Contexts: ``snapdep-plan`` for the plan table, ``snapdep`` for everything else,
so that a later annotation of a build replaces an earlier one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .pipeline import short
from .planner import dependencies_first
from .records import Cause, Failure, Plan, Version, list_failures
from .store import Store

PLAN_CONTEXT = "snapdep-plan"
CONTEXT = "snapdep"


def preformatted(text: str) -> str:
    """``text`` as a Markdown code block, so that its indentation survives."""
    return f"```\n{text}\n```"


# ---------------------------------------------------------------------------
# The plan (enter, step 4)
# ---------------------------------------------------------------------------


def plan_annotation(p: Plan, project: str, actions: Mapping[str, str]) -> str:
    """Markdown table of ``project``'s closure in ``p``: commit, key and what happens.

    ``actions`` maps each project of the closure to what will happen to it.
    """
    head = (
        f"**Snapshot plan** `{p.id}`: root {p.root.project} build {p.root.build_number}, "
        f"context `{p.context}`, rebuild {p.rebuild}"
    )
    rows = [
        f"| {q} | `{p.nodes[q].label}` | `{short(p.nodes[q].commit)}` | `{p.nodes[q].key}` "
        f"| {actions.get(q, '')} |"
        for q in dependencies_first(p, project)
    ]
    table = [
        "| Project | Branch | Commit | Key | What happens |",
        "| --- | --- | --- | --- | --- |",
    ]
    return "\n".join([head, "", *table, *rows])


def reuse_annotation(project: str, v: Version) -> str:
    """This build reuses version ``v`` of its key."""
    return (
        f"**Snapshot**: {project} `{v.key}` is already built: reused "
        f"[build {v.build.number}]({v.build.url}) of {v.build.pipeline} "
        f"(`{short(v.commit)}`, sealed {v.sealed_at})."
    )


# ---------------------------------------------------------------------------
# Failure chains
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Missing:
    """A key that has no version, with the failure that explains it.

    ``failure``: its most recent failure record, None when there is none.
    ``causes``: the same, for each dependency the failure names in ``caused_by``.
    """

    project: str
    key: str
    commit: str
    failure: Failure | None
    causes: tuple[Missing, ...] = ()


def _commit_of(p: Plan, project: str) -> str:
    n = p.nodes.get(project)
    return n.commit if n is not None else "?"


def trace_failure(s: Store, p: Plan, f: Failure, seen: frozenset = frozenset()) -> Missing:
    """``Missing`` for failure ``f``, its causes traced through the store."""
    here = seen | {(f.project, f.key)}
    causes = tuple(
        trace_missing(s, p, c, here) for c in f.caused_by if (c.project, c.key) not in here
    )
    return Missing(f.project, f.key, _commit_of(p, f.project), f, causes)


def trace_missing(s: Store, p: Plan, c: Cause, seen: frozenset = frozenset()) -> Missing:
    """``Missing`` for dependency ``c``: its failure, and so on down ``caused_by``.

    Its latest failure under plan ``p`` (the build of this chain), else its
    latest failure.
    """
    failures = list_failures(s, c.project, c.key)
    if not failures:
        return Missing(c.project, c.key, _commit_of(p, c.project), None)
    of_plan = [f for f in failures if f.plan == p.id]
    return trace_failure(s, p, (of_plan or failures)[-1], seen)


def _subject(m: Missing) -> str:
    return f"{m.project} @ {short(m.commit)} ({m.key})"


def failure_lines(m: Missing, indent: int) -> list[str]:
    """What ``m``'s failure record says, indented by ``indent`` spaces."""
    pad = " " * indent
    f = m.failure
    if f is None:
        return [f"{pad}no failure on record: its build was cancelled, was lost or did not run"]
    out = [f"{pad}FAILED in build {f.build.number}: {f.build.url}"]
    out += [f"{pad}  step {st.key}: {st.outcome or 'did not run'}" for st in f.failed_steps]
    for c in m.causes:
        out.append(f"{pad}  caused by {_subject(c)}")
        out += failure_lines(c, indent + 4)
    if not f.failed_steps and not m.causes:
        out.append(f"{pad}  (no failed step recorded)")
    return out


def failed_builds(m: Missing) -> list[tuple[str, int]]:
    """``(pipeline, number)`` of every build down ``m`` that has failed steps."""
    f = m.failure
    own = [(f.build.pipeline, f.build.number)] if f is not None and f.failed_steps else []
    below = [b for c in m.causes for b in failed_builds(c)]
    return list(dict.fromkeys(own + below))


def _builds_text(builds: Sequence[tuple[str, int]]) -> str:
    return " and ".join(f"{pipeline} {number}" for pipeline, number in builds)


def retry_lines(missing: Sequence[Missing]) -> list[str]:
    """How a person heals a build whose dependencies ``missing`` have no version.

    Not by retrying a step of this build: its generate job has ended, and a
    retried trigger step would not run it again. The failed jobs below are
    retried where they failed, then this build is built again; every version
    sealed meanwhile is reused.
    """
    builds = list(dict.fromkeys(b for m in missing for b in failed_builds(m)))
    if not builds:
        return ["To retry: rebuild this build. Everything that passed is reused."]
    return [
        f"To retry: retry the failed job in {_builds_text(builds)}. When it passes,",
        "rebuild this build. Everything that passed is reused.",
    ]


def dependency_failure_text(project: str, build_number: int, missing: Sequence[Missing]) -> str:
    """The failure annotation of section 5.6, as plain text.

    ``missing``: the direct dependencies of ``project`` that have no version.
    """
    lines = [
        "Snapshot dependency failed",
        "",
        f"{project} build {build_number} was not built, because:",
    ]
    for m in missing:
        lines.append(f"  {_subject(m)} has no version.")
        lines += failure_lines(m, 4)
    lines += ["", *retry_lines(missing)]
    return "\n".join(lines)


def shared_failure_text(m: Missing, *, same_chain: bool) -> str:
    """This build's key failed in the build of ``m.failure``, so this build does not build it.

    ``same_chain``: that build ran under this build's plan (a key is built at
    most once per chain); otherwise this build waited behind it at the gate.
    """
    f = m.failure
    assert f is not None
    builds = failed_builds(m) or [(f.build.pipeline, f.build.number)]
    when = "earlier in this chain" if same_chain else "while this build waited for it"
    return "\n".join(
        [
            "Snapshot build failed in another build",
            "",
            f"{_subject(m)} failed in build {f.build.number} {when}:",
            *failure_lines(m, 2),
            "",
            f"To retry: retry the failed job in {_builds_text(builds)}. When it passes,",
            "retry this job. Everything that passed is reused.",
        ]
    )


def failed_steps_text(project: str, key: str, f: Failure) -> str:
    """The annotation of the fail job: this build is not a version of its key."""
    return "\n".join(
        [
            "Snapshot: this build is not a version",
            "",
            f"{project} build {f.build.number} ({key}) did not pass these steps:",
            *[f"  step {st.key}: {st.outcome or 'did not run'}" for st in f.failed_steps],
        ]
    )
