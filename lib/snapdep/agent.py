"""The Buildkite agent and the pipeline generator, as interfaces.

The step commands (``snapdep.steps``) talk to the build they run in only
through an ``Agent``:

- ``meta_data_get(key)`` / ``meta_data_set(key, value)``: build meta-data;
  ``get`` returns None when the key is not set;
- ``step_outcome(key)``: the outcome of a step of this build (``passed``,
  ``hard_failed``, ``soft_failed``, ``neutral``, or empty when it did not run);
- ``pipeline_upload(document)``: upload a pipeline (YAML or JSON text) into this
  build, with Buildkite's usual interpolation, as the upload job does today;
- ``annotate(body, style, context)``: create or replace an annotation.

They obtain the pipeline of their repository through a ``Generator``: it runs
the generator command in the checkout and returns what it printed.

``RealAgent`` shells out to ``buildkite-agent``; ``ShellGenerator`` runs the
command with bash. ``snapdep.fakes`` has a recording agent and a fixed
generator for tests.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Protocol

ANNOTATION_STYLES = ("success", "info", "warning", "error")

# ``buildkite-agent meta-data exists`` exits with this status when the key is not set.
META_DATA_ABSENT = 100


class AgentError(RuntimeError):
    """A ``buildkite-agent`` call failed."""


class GeneratorError(RuntimeError):
    """The generator command failed."""


class Agent(Protocol):
    """What the step commands may do to their build. See the module docstring."""

    def meta_data_get(self, key: str) -> str | None: ...

    def meta_data_set(self, key: str, value: str) -> None: ...

    def step_outcome(self, key: str) -> str: ...

    def pipeline_upload(self, document: str) -> None: ...

    def annotate(self, body: str, style: str, context: str) -> None: ...


class Generator(Protocol):
    """Runs a generator command in the checkout and returns its standard output."""

    def run(self, command: str) -> str: ...


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """The result of one command."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], "str | None"], Outcome]


def run_process(args: Sequence[str], stdin: str | None) -> Outcome:
    """Run ``args`` with ``stdin`` as input; never raises on exit status.

    Standard error is captured, so that a failure can be reported in one line.
    A command that cannot be started exits 127, as in a shell.
    """
    try:
        p = subprocess.run(list(args), input=stdin, capture_output=True, text=True, check=False)
    except OSError as exc:
        return Outcome(127, "", str(exc))
    return Outcome(p.returncode, p.stdout, p.stderr)


def _checked(o: Outcome, args: Sequence[str]) -> str:
    if o.returncode != 0:
        raise AgentError(f"{' '.join(args[:3])} failed ({o.returncode}): {o.stderr.strip()}")
    return o.stdout


# ---------------------------------------------------------------------------
# The real agent
# ---------------------------------------------------------------------------


class RealAgent:
    """``Agent`` over the ``buildkite-agent`` command of the job.

    ``run`` executes one command; tests may replace it.
    """

    def __init__(self, run: Runner = run_process, binary: str = "buildkite-agent") -> None:
        self._run = run
        self._bin = binary

    def _agent(self, args: Sequence[str], stdin: str | None = None) -> Outcome:
        return self._run([self._bin, *args], stdin)

    def meta_data_get(self, key: str) -> str | None:
        exists = ["meta-data", "exists", key]
        o = self._agent(exists)
        if o.returncode == META_DATA_ABSENT:
            return None
        _checked(o, [self._bin, *exists])
        get = ["meta-data", "get", key]
        return _checked(self._agent(get), [self._bin, *get])

    def meta_data_set(self, key: str, value: str) -> None:
        args = ["meta-data", "set", key]
        _checked(self._agent(args, value), [self._bin, *args])

    def step_outcome(self, key: str) -> str:
        args = ["step", "get", "outcome", "--step", key]
        return _checked(self._agent(args), [self._bin, *args]).strip()

    def pipeline_upload(self, document: str) -> None:
        args = ["pipeline", "upload"]
        _checked(self._agent(args, document), [self._bin, *args])

    def annotate(self, body: str, style: str, context: str) -> None:
        if style not in ANNOTATION_STYLES:
            raise ValueError(f"annotate: unknown style {style!r}")
        args = ["annotate", "--style", style, "--context", context]
        _checked(self._agent(args, body), [self._bin, *args])


# ---------------------------------------------------------------------------
# The real generator
# ---------------------------------------------------------------------------

GENERATE_PHASE = {"SNAPDEP_PHASE": "generate"}


def generator_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment of the generator: ``env`` with ``SNAPDEP_PHASE=generate``."""
    return {**env, **GENERATE_PHASE}


class ShellGenerator:
    """``Generator`` that runs the command with ``bash -c`` in ``cwd``.

    Standard error goes to the job log; standard output is returned.
    """

    def __init__(self, cwd: str = ".", env: Mapping[str, str] | None = None) -> None:
        self._cwd = cwd
        self._env = generator_env(os.environ if env is None else env)

    def run(self, command: str) -> str:
        p = subprocess.run(
            ["bash", "-c", command],
            cwd=self._cwd,
            env=self._env,
            stdout=subprocess.PIPE,
            text=True,
            check=False,
        )
        if p.returncode != 0:
            raise GeneratorError(f"generator exited {p.returncode}: {command}")
        return p.stdout
