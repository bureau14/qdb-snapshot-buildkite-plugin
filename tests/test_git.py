from __future__ import annotations

import pytest

from snapdep.git import (
    Completed,
    GitError,
    RealGit,
    choose_release_line,
    github_repo,
    parse_ls_remote,
    repo_url,
)

A = "a" * 40
B = "b" * 40


def test_repo_url():
    assert repo_url("bureau14/qdb-duck") == "https://github.com/bureau14/qdb-duck.git"


@pytest.mark.parametrize(
    "url, repo",
    [
        ("git@github.com:bureau14/qdb-api-rest.git", "bureau14/qdb-api-rest"),
        ("https://github.com/bureau14/qdb-api-rest.git", "bureau14/qdb-api-rest"),
        ("https://github.com/bureau14/qdb-api-rest", "bureau14/qdb-api-rest"),
        ("ssh://git@github.com/bureau14/qdb.duck.git\n", "bureau14/qdb.duck"),
        ("https://gitlab.com/a/b.git", None),
        ("", None),
    ],
)
def test_github_repo(url, repo):
    assert github_repo(url) == repo


def test_parse_ls_remote_matches_exact_ref_only():
    out = f"{A}\trefs/heads/x/refs/heads/master\n{B}\trefs/heads/master\n"
    assert parse_ls_remote(out, "refs/heads/master") == B
    assert parse_ls_remote(out, "refs/heads/feature") is None
    assert parse_ls_remote("", "refs/heads/master") is None


def test_choose_release_line_context_is_a_line():
    assert choose_release_line("3.14.x", ["master", "3.14.x"], {}) == "3.14.x"


def test_choose_release_line_most_recent_merge_base():
    lines = ["master", "3.14.x"]
    assert choose_release_line("f", lines, {"master": 100, "3.14.x": 200}) == "3.14.x"
    assert choose_release_line("f", lines, {"master": 300, "3.14.x": 200}) == "master"


def test_choose_release_line_tie_prefers_first_listed_and_unknown_is_master():
    assert choose_release_line("f", ["3.14.x", "master"], {"master": 5, "3.14.x": 5}) == "3.14.x"
    assert choose_release_line("f", ["3.14.x"], {"3.14.x": None}) == "master"
    assert choose_release_line("f", ["3.14.x"], {}) == "master"


class ScriptedRunner:
    """Answers git commands from a table keyed by the command's words after "git"."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((tuple(args), cwd))
        for prefix, result in self.answers:
            if tuple(args[1 : 1 + len(prefix)]) == prefix:
                return result
        return Completed(0, "", "")


def test_real_head_uses_ls_remote():
    run = ScriptedRunner([(("ls-remote",), Completed(0, f"{A}\trefs/heads/master\n", ""))])
    g = RealGit(cwd="/w", run=run)
    assert g.head("o/r", "master") == A
    assert g.head("o/r", "nope") is None
    assert run.calls[0][0] == (
        "git",
        "ls-remote",
        "https://github.com/o/r.git",
        "refs/heads/master",
    )


def test_real_head_raises_on_git_failure():
    run = ScriptedRunner([(("ls-remote",), Completed(128, "", "fatal: repository not found"))])
    with pytest.raises(GitError, match="repository not found"):
        RealGit(run=run).head("o/r", "master")


def test_real_read_file_absent_and_present():
    run = ScriptedRunner([(("ls-tree",), Completed(0, "", ""))])
    with RealGit(run=run) as g:
        assert g.read_file("o/r", A, ".buildkite/snapshot.yml") is None
    run = ScriptedRunner(
        [
            (("ls-tree",), Completed(0, ".buildkite/snapshot.yml\n", "")),
            (("show",), Completed(0, "schema: 1\n", "")),
        ]
    )
    with RealGit(run=run) as g:
        assert g.read_file("o/r", A, ".buildkite/snapshot.yml") == "schema: 1\n"
        assert g.read_file("o/r", B, ".buildkite/snapshot.yml") == "schema: 1\n"
    inits = [c for c, _ in run.calls if c[1] == "init"]
    fetches = [c for c, _ in run.calls if c[1] == "fetch"]
    assert len(inits) == 1, "one scratch repository per repository"
    assert fetches[0][-5:] == ("--depth", "1", "--filter=blob:none", "origin", A)


def release_line_git(fetch_fails=(), unreachable=False, unrelated=(), logs=None):
    """A ``RealGit`` over a remote whose lines master and 3.14.x exist.

    ``fetch_fails``: lines whose fetch fails. ``unreachable``: ls-remote fails
    too (the remote cannot be reached). ``unrelated``: lines without a common
    ancestor with HEAD. Merge-base times: master 100, 3.14.x 200.
    """
    times = {A: "100", B: "200"}
    tips = {"refs/heads/master": A, "refs/heads/3.14.x": B}
    state = {}

    def run(args, cwd):
        words = tuple(args[1:])
        if words[0] == "fetch":
            line = words[-1][len("refs/heads/") :]
            if line in fetch_fails:
                return Completed(128, "", "fatal: unable to access: 502")
            if words[-1] not in tips:
                return Completed(128, "", f"fatal: couldn't find remote ref {words[-1]}")
            state["fetched"] = line
            return Completed(0, "", "")
        if words[0] == "ls-remote":
            if unreachable:
                return Completed(128, "", "fatal: unable to access: 502")
            tip = tips.get(words[-1])
            return Completed(0, f"{tip}\t{words[-1]}\n" if tip else "", "")
        if words[0] == "merge-base":
            if state["fetched"] in unrelated:
                return Completed(1, "", "")
            return Completed(0, tips[f"refs/heads/{state['fetched']}"] + "\n", "")
        if words[0] == "show":
            return Completed(0, times[words[-1]] + "\n", "")
        return Completed(0, "", "")

    return RealGit(run=run, log=(logs.append if logs is not None else lambda m: None))


def test_real_release_line_from_merge_base_times():
    logs = []
    g = release_line_git(logs=logs)
    assert g.release_line("o/r", "3.14.x", ["master", "3.14.x"]) == "3.14.x"
    assert g.release_line("o/r", "feature", ["master", "3.14.x"]) == "3.14.x"
    assert logs == ["release line of feature: 3.14.x (merge-base times: master 100, 3.14.x 200)"]


def test_real_release_line_skips_a_line_that_does_not_exist_or_is_unrelated():
    g = release_line_git()
    assert g.release_line("o/r", "feature", ["gone", "master"]) == "master"
    g = release_line_git(unrelated=("3.14.x",))
    assert g.release_line("o/r", "feature", ["3.14.x", "master"]) == "master"


def test_real_release_line_raises_when_an_existing_line_cannot_be_fetched():
    with pytest.raises(GitError, match="502"):
        release_line_git(fetch_fails=("3.14.x",)).release_line("o/r", "f", ["master", "3.14.x"])
    with pytest.raises(GitError, match="502"):
        release_line_git(fetch_fails=("3.14.x",), unreachable=True).release_line(
            "o/r", "f", ["master", "3.14.x"]
        )
