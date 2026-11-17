from __future__ import annotations

import pytest

from snapdep.agent import (
    AgentError,
    GeneratorError,
    Outcome,
    RealAgent,
    ShellGenerator,
    generator_env,
    unfiltered_env,
)
from snapdep.fakes import FixedGenerator, RecordingAgent


class Script:
    """A runner that answers from a list and records every call with its input."""

    def __init__(self, *answers: Outcome) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, stdin):
        self.calls.append((list(args), stdin))
        return self.answers.pop(0)


OK = Outcome(0, "", "")


def test_meta_data_get_absent_is_none():
    run = Script(Outcome(100, "", ""))
    assert RealAgent(run).meta_data_get("snapdep-mode") is None
    assert run.calls == [(["buildkite-agent", "meta-data", "exists", "snapdep-mode"], None)]


def test_meta_data_get_present():
    run = Script(OK, Outcome(0, "build", ""))
    assert RealAgent(run).meta_data_get("snapdep-mode") == "build"
    assert run.calls[1] == (["buildkite-agent", "meta-data", "get", "snapdep-mode"], None)


def test_meta_data_get_drops_the_line_end_the_agent_prints():
    """``buildkite-agent meta-data get`` ends its output with a newline.

    Before the fix fail read the mode as "build\\n", took it for not
    ``build`` and recorded nothing (qdb-api-go build 447).
    """
    run = Script(OK, Outcome(0, "build\n", ""))
    assert RealAgent(run).meta_data_get("snapdep-mode") == "build"


def test_meta_data_get_failure_is_an_error_not_absent():
    with pytest.raises(AgentError, match="exited|failed"):
        RealAgent(Script(Outcome(1, "", "boom"))).meta_data_get("k")


def test_meta_data_set_passes_the_value_on_stdin():
    run = Script(OK)
    RealAgent(run).meta_data_set("snapdep-key", "v1-x")
    assert run.calls == [(["buildkite-agent", "meta-data", "set", "snapdep-key"], "v1-x")]


def test_step_outcome():
    run = Script(Outcome(0, "soft_failed\n", ""))
    assert RealAgent(run).step_outcome("build") == "soft_failed"
    assert run.calls[0][0] == ["buildkite-agent", "step", "get", "outcome", "--step", "build"]


def test_pipeline_upload_and_annotate_pass_the_document_on_stdin():
    run = Script(OK, OK)
    a = RealAgent(run)
    a.pipeline_upload("steps: []\n")
    a.annotate("**x**", "error", "snapdep")
    assert run.calls == [
        (["buildkite-agent", "pipeline", "upload"], "steps: []\n"),
        (["buildkite-agent", "annotate", "--style", "error", "--context", "snapdep"], "**x**"),
    ]


def test_upload_failure_raises():
    with pytest.raises(AgentError, match="pipeline upload failed \\(2\\): bad"):
        RealAgent(Script(Outcome(2, "", "bad"))).pipeline_upload("x")


def test_a_missing_agent_binary_is_an_agent_error():
    with pytest.raises(AgentError, match="failed \\(127\\)"):
        RealAgent(binary="/nonexistent/buildkite-agent").meta_data_get("k")


def test_annotate_rejects_unknown_style():
    with pytest.raises(ValueError):
        RealAgent(Script()).annotate("x", "loud", "c")


def test_generator_env_sets_the_phase():
    assert generator_env({"A": "1", "SNAPDEP_PHASE": "x"}) == {
        "A": "1",
        "SNAPDEP_PHASE": "generate",
    }


def test_generator_env_restores_the_jobs_pythonpath():
    hook = {"A": "1", "PYTHONPATH": "/plugin/lib", "SNAPDEP_JOB_PYTHONPATH": "/job"}
    assert generator_env(hook) == {"A": "1", "PYTHONPATH": "/job", "SNAPDEP_PHASE": "generate"}
    hook = {"A": "1", "PYTHONPATH": "/plugin/lib", "SNAPDEP_JOB_PYTHONPATH": ""}
    assert generator_env(hook) == {"A": "1", "SNAPDEP_PHASE": "generate"}


def test_shell_generator_returns_stdout_with_the_phase_set(tmp_path):
    g = ShellGenerator(cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"})
    assert g.run('echo "phase=$SNAPDEP_PHASE"; pwd >&2') == "phase=generate\n"
    with pytest.raises(GeneratorError, match="exited 3"):
        g.run("exit 3")


def test_unfiltered_env_drops_the_selection():
    env = {"A": "1", "QDB_PIPELINE_FILTER": "os: linux", "BUILDKITE_MESSAGE": "{os: linux}"}
    assert unfiltered_env(env) == {"A": "1", "BUILDKITE_MESSAGE": ""}


def test_shell_generator_runs_without_the_selection(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "QDB_PIPELINE_FILTER": "f", "BUILDKITE_MESSAGE": "m"}
    g = ShellGenerator(cwd=str(tmp_path), env=env)
    show = 'echo "${QDB_PIPELINE_FILTER-unset}|$BUILDKITE_MESSAGE"'
    assert g.run(show) == "f|m\n"
    assert g.run(show, unfiltered=True) == "unset|\n"


def test_recording_agent_and_fixed_generator():
    a = RecordingAgent(meta_data={"a": "1"}, outcomes={"s": "passed"})
    a.meta_data_set("b", "2")
    a.pipeline_upload("doc")
    a.annotate("body", "info", "ctx")
    assert (a.meta_data_get("a"), a.meta_data_get("b"), a.meta_data_get("c")) == ("1", "2", None)
    assert (a.step_outcome("s"), a.step_outcome("t")) == ("passed", "")
    assert a.uploads == ["doc"] and a.annotations == [("body", "info", "ctx")]
    g = FixedGenerator("steps: []")
    assert g.run("cmd") == "steps: []" and g.commands == ["cmd"]
