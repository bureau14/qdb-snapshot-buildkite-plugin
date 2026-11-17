from __future__ import annotations

import json

import pytest

from snapdep import cli
from snapdep.declaration import PATH
from snapdep.fakes import FakeGit, FixedGenerator, MemoryStore, RecordingAgent
from snapdep.jobs import Effects

DECL = """
schema: 1
lines: [master, 3.14.x]
pipelines:
  qdb-api-rest:
    depends:
      - {project: quasardb-build, repo: bureau14/quasardb}
"""
QDB_DECL = "schema: 1\nlines: [master, 3.14.x]\npipelines:\n  quasardb-build: {}\n"
QDB_HEAD = "1" * 40
CHECKOUT = "2" * 40


class CheckoutGit(FakeGit):
    """FakeGit usable where the command line expects RealGit."""

    def __init__(self, *args, **kwargs):
        super().__init__(
            heads={("bureau14/quasardb", "master"): QDB_HEAD},
            files={("bureau14/quasardb", QDB_HEAD, PATH): QDB_DECL},
            checkout=CHECKOUT,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def origin_repo(self):
        return "bureau14/qdb-api-rest"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    (tmp_path / ".buildkite").mkdir()
    (tmp_path / PATH).write_text(DECL)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "RealGit", CheckoutGit)
    return tmp_path


def test_plan_dry_run_prints_the_plan(checkout, capsys):
    env = {"BUILDKITE_PIPELINE_SLUG": "qdb-api-rest", "BUILDKITE_BRANCH": "master"}
    assert cli.main(["plan", "--dry-run"], env) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["id"] == "dry-run"
    assert plan["nodes"]["qdb-api-rest"]["commit"] == CHECKOUT
    assert plan["nodes"]["qdb-api-rest"]["repo"] == "bureau14/qdb-api-rest"
    assert plan["nodes"]["quasardb-build"]["commit"] == QDB_HEAD


def test_plan_flags_override_environment(checkout, capsys):
    env = {"BUILDKITE_REPO": "git@github.com:bureau14/qdb-api-rest.git"}
    argv = ["plan", "--dry-run", "--pipeline", "qdb-api-rest", "--branch", "master"]
    assert cli.main(argv, env) == 0
    assert json.loads(capsys.readouterr().out)["context"] == "master"


@pytest.mark.parametrize(
    "argv, env, message",
    [
        (["plan"], {}, "only --dry-run is implemented"),
        (["plan", "--dry-run"], {"BUILDKITE_BRANCH": "master"}, "no pipeline"),
        (["plan", "--dry-run"], {"BUILDKITE_PIPELINE_SLUG": "qdb-api-rest"}, "no branch"),
        (
            ["plan", "--dry-run", "--pipeline", "other", "--branch", "master"],
            {},
            "pipelines.other: missing",
        ),
    ],
)
def test_plan_errors_are_one_line_and_exit_1(checkout, capsys, argv, env, message):
    assert cli.main(argv, env) == 1
    err = capsys.readouterr().err
    assert err.startswith("snapdep plan: ") and message in err


def test_unknown_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["nope"], {})
    assert e.value.code == 2


def test_commands_are_registered_by_name():
    assert [c.name for c in cli.COMMANDS] == ["plan", "enter", "generate", "fail", "seal"]


JOB_ENV = {
    "BUILDKITE_PIPELINE_SLUG": "qdb-api-rest",
    "BUILDKITE_BRANCH": "master",
    "BUILDKITE_REPO": "https://github.com/bureau14/qdb-api-rest.git",
    "BUILDKITE_BUILD_ID": "uuid-1",
    "BUILDKITE_BUILD_NUMBER": "3",
}


@pytest.fixture
def job_effects(checkout, monkeypatch):
    """Fakes in place of the real store, agent and generator of a job."""
    agent = RecordingAgent()
    store = MemoryStore()

    def fake_effects(env, git):
        return Effects(store, agent, git, FixedGenerator("steps: []\n"), lambda: 0.0, print)

    monkeypatch.setattr(cli, "real_effects", fake_effects)
    return agent, store


def test_enter_runs_the_job_with_the_checkout_declaration(job_effects):
    agent, _ = job_effects
    assert cli.main(["enter"], {**JOB_ENV, "SNAPDEP_DISABLE": "1"}) == 0
    assert agent.meta_data["snapdep-mode"] == "plain"
    assert agent.uploads[0] == "steps: []\n"


def test_fail_and_seal_outside_build_mode_exit_0(job_effects):
    assert cli.main(["fail"], JOB_ENV) == 0
    assert cli.main(["seal"], JOB_ENV) == 0


def test_job_errors_are_one_line_and_exit_1(job_effects, capsys):
    agent, _ = job_effects
    agent.meta_data["snapdep-mode"] = "build"
    assert cli.main(["generate"], JOB_ENV) == 1
    assert capsys.readouterr().err.startswith("snapdep generate: meta-data snapdep-plan is not set")
    assert cli.main(["seal"], {"BUILDKITE_BRANCH": "x"}) == 1
    assert "BUILDKITE_PIPELINE_SLUG is not set" in capsys.readouterr().err
