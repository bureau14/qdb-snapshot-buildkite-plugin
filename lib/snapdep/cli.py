"""The command line: ``python -m snapdep <command> [options]``.

Commands: ``plan --dry-run`` for people, and ``enter``, ``generate``, ``fail``
and ``seal``, the jobs of an adopted build (``snapdep.jobs``), run by the
plugin's command hook.

Each command is a ``Command``: a name, a help line, a function that adds its
options to an argparse sub-parser, and a function that runs it and returns the
exit status. ``COMMANDS`` lists them; to add a command, write its two functions
and append a ``Command`` to ``COMMANDS``. ``main`` handles the errors every
command shares (``EXPECTED_ERRORS``): it prints them as one line and exits 1.

Commands read the environment through the ``env`` mapping they are given, never
``os.environ`` directly, so that tests can call ``main(argv, env)``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable

from .agent import AgentError, GeneratorError, RealAgent, ShellGenerator
from .declaration import PATH, Declaration, DeclarationError, read_declaration
from .git import Git, GitError, RealGit, github_repo
from .jobs import Effects, Job, JobError, enter, fail, generate, job_from_env, seal
from .pipeline import PipelineError
from .planner import PlanError, Root, make_plan
from .records import RecordError, plan_to_dict
from .store import LazyStore, StoreError, open_store

Env = Mapping[str, str]

EXPECTED_ERRORS = (
    AgentError,
    DeclarationError,
    GeneratorError,
    GitError,
    JobError,
    PipelineError,
    PlanError,
    RecordError,
    StoreError,
)


@dataclass(frozen=True)
class Command:
    """A subcommand: ``configure`` adds its options, ``run`` returns its exit status."""

    name: str
    help: str
    configure: Callable[[argparse.ArgumentParser, Env], None]
    run: Callable[[argparse.Namespace, Env], int]


class UsageError(RuntimeError):
    """The command was called without something it needs."""


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def configure_plan(p: argparse.ArgumentParser, env: Env) -> None:
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan as JSON and write nothing (the only mode for now)",
    )
    p.add_argument(
        "--pipeline",
        default=env.get("BUILDKITE_PIPELINE_SLUG"),
        help="pipeline slug of the root (default: BUILDKITE_PIPELINE_SLUG)",
    )
    p.add_argument(
        "--branch",
        default=env.get("BUILDKITE_BRANCH"),
        help="branch of the root build, the context (default: BUILDKITE_BRANCH)",
    )
    p.add_argument(
        "--repo",
        default=github_repo(env.get("BUILDKITE_REPO", "")),
        help="GitHub repository owner/name of the root (default: from "
        "BUILDKITE_REPO, else the checkout's origin)",
    )
    p.add_argument("--declaration", default=PATH, help=f"path of the declaration (default: {PATH})")
    p.add_argument(
        "--rebuild-all",
        action="store_true",
        default=env.get("SNAPDEP_REBUILD") == "all",
        help="plan with rebuild all (default: SNAPDEP_REBUILD=all)",
    )


def root_from(args: argparse.Namespace, env: Env, git: RealGit) -> Root:
    """The root build described by the options, the environment and the checkout."""
    if not args.pipeline:
        raise UsageError("no pipeline: set BUILDKITE_PIPELINE_SLUG or pass --pipeline")
    if not args.branch:
        raise UsageError("no branch: set BUILDKITE_BRANCH or pass --branch")
    repo = args.repo or git.origin_repo()
    if not repo:
        raise UsageError("no GitHub repository: set BUILDKITE_REPO or pass --repo")
    return Root(
        project=args.pipeline,
        repo=repo,
        branch=args.branch,
        commit=git.head_commit(),
        build_id=env.get("BUILDKITE_BUILD_ID", "dry-run"),
        build_number=int(env.get("BUILDKITE_BUILD_NUMBER", "0")),
        build_url=env.get("BUILDKITE_BUILD_URL", ""),
    )


def run_plan(args: argparse.Namespace, env: Env) -> int:
    if not args.dry_run:
        raise UsageError("plan: only --dry-run is implemented; the enter step writes plans")
    decl = read_declaration(args.declaration)
    if decl is None:
        raise UsageError(f"{args.declaration}: not found; this checkout is not snapshot-managed")
    with RealGit() as git:
        root = root_from(args, env, git)
        store = LazyStore(lambda: open_store(env))
        plan = make_plan(root, decl, git, store, args.rebuild_all, time.time())
    print(json.dumps(plan_to_dict(plan), indent=2, sort_keys=True))
    return 0


PLAN = Command("plan", "compute the plan of the current checkout", configure_plan, run_plan)

# ---------------------------------------------------------------------------
# The jobs: enter, generate, fail, seal
# ---------------------------------------------------------------------------


def log_stderr(msg: str) -> None:
    print(f"[snapdep] {msg}", file=sys.stderr)


def real_effects(env: Env, git: Git) -> Effects:
    """The store, agent and generator of a Buildkite job; the store opens on first use."""
    return Effects(
        store=LazyStore(lambda: open_store(env)),
        agent=RealAgent(),
        git=git,
        generator=ShellGenerator(env=env),
        clock=time.time,
        log=log_stderr,
    )


def configure_job(p: argparse.ArgumentParser, env: Env) -> None:
    p.add_argument("--declaration", default=PATH, help=f"path of the declaration (default: {PATH})")


def job_command(name: str, help: str, job: Callable[[Job, Declaration | None, Effects], int]):
    """A ``Command`` that runs ``job`` in the current Buildkite job."""

    def run(args: argparse.Namespace, env: Env) -> int:
        j = job_from_env(env)
        decl = read_declaration(args.declaration)
        with RealGit() as git:
            return job(j, decl, real_effects(env, git))

    return Command(name, help, configure_job, run)


ENTER = job_command("enter", "first gate job: plan, reuse or build", enter)
GENERATE = job_command("generate", "upload the pipeline once the dependencies are built", generate)
FAIL = job_command("fail", "record the failure of this build", fail)
SEAL = job_command("seal", "record this build as the version of its key", seal)

# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

COMMANDS: list[Command] = [PLAN, ENTER, GENERATE, FAIL, SEAL]


def build_parser(commands: Sequence[Command], env: Env) -> argparse.ArgumentParser:
    """The argument parser with one sub-parser per command."""
    parser = argparse.ArgumentParser(prog="python -m snapdep")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for c in commands:
        p = sub.add_parser(c.name, help=c.help)
        c.configure(p, env)
        p.set_defaults(run=c.run)
    return parser


def main(argv: Sequence[str] | None = None, env: Env | None = None) -> int:
    """Parse ``argv``, run the command, return its exit status."""
    e = dict(os.environ) if env is None else env
    args = build_parser(COMMANDS, e).parse_args(argv)
    try:
        return args.run(args, e)
    except (UsageError, *EXPECTED_ERRORS) as exc:
        print(f"snapdep {args.command}: {exc}", file=sys.stderr)
        return 1
