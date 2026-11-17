"""Read-only git access: the ``Git`` interface and its real implementation.

The planner learns everything it needs about other repositories through four
questions (specification section 3):

- ``head(repo, ref)``: the commit at the head of branch ``ref`` of a GitHub
  repository, or None when the branch does not exist;
- ``read_file(repo, commit, path)``: one file at one commit of a repository,
  without cloning it, or None when the file does not exist there;
- ``head_commit()``: the commit checked out in the current directory;
- ``release_line(repo, branch, lines)``: the release line ``branch`` belongs to.

``RealGit`` answers them by running git over HTTPS against
``https://github.com/<repo>.git``; ``snapdep.fakes.FakeGit`` answers them from
memory for tests. Neither writes to any remote.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Protocol

DEFAULT_LINE = "master"

_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitError(RuntimeError):
    """A git command failed for a reason other than "no such ref" or "no such file"."""


class Git(Protocol):
    """What the planner may ask about git. See the module docstring."""

    def head(self, repo: str, ref: str) -> str | None: ...

    def read_file(self, repo: str, commit: str, path: str) -> str | None: ...

    def head_commit(self) -> str: ...

    def release_line(self, repo: str, branch: str, lines: Sequence[str]) -> str: ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def repo_url(repo: str) -> str:
    """Return the HTTPS URL of GitHub repository ``repo`` ("owner/name")."""
    return f"https://github.com/{repo}.git"


def branch_ref(branch: str) -> str:
    """Return the full ref name of ``branch``."""
    return f"refs/heads/{branch}"


_GITHUB_URL = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
    r"([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def github_repo(url: str) -> str | None:
    """``owner/name`` of a GitHub clone URL (SSH or HTTPS); None for anything else."""
    m = _GITHUB_URL.match(url.strip())
    return m.group(1) if m else None


def parse_ls_remote(output: str, ref: str) -> str | None:
    """Return the commit of exactly ``ref`` in ``git ls-remote`` output, or None.

    ``git ls-remote <url> <pattern>`` matches patterns against the tail of ref
    names, so ``refs/heads/x`` also matches ``refs/heads/a/refs/heads/x``; only
    the exact name counts.
    """
    for row in output.splitlines():
        parts = row.split("\t")
        if len(parts) == 2 and parts[1] == ref and _SHA.match(parts[0]):
            return parts[0]
    return None


def choose_release_line(
    branch: str, lines: Sequence[str], merge_base_times: Mapping[str, int | None]
) -> str:
    """Return the release line ``branch`` belongs to.

    ``branch`` itself when it is one of ``lines``. Otherwise the line whose
    merge-base with the branch is the most recent (``merge_base_times`` maps a
    line to the committer time of that merge-base, None when unknown); on a tie
    the line listed first wins. ``master`` when no time is known.
    """
    if branch in lines:
        return branch
    times = [(merge_base_times.get(line), -i, line) for i, line in enumerate(lines)]
    known = [x for x in times if x[0] is not None]
    if not known:
        return DEFAULT_LINE
    return max(known)[2]


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Completed:
    """The outcome of one command."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], str], Completed]


def run_command(args: Sequence[str], cwd: str) -> Completed:
    """Run ``args`` in ``cwd`` without a terminal prompt; never raises on exit status."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    p = subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True, check=False)
    return Completed(p.returncode, p.stdout, p.stderr)


def _checked(c: Completed, args: Sequence[str]) -> str:
    if c.returncode != 0:
        raise GitError(f"{' '.join(args)} failed ({c.returncode}): {c.stderr.strip()}")
    return c.stdout


# ---------------------------------------------------------------------------
# The real implementation
# ---------------------------------------------------------------------------


class RealGit:
    """``Git`` over the git command line.

    ``cwd`` is the checkout of the current build. Files of other repositories
    are fetched into scratch repositories under a temporary directory, one per
    repository, removed by ``close()`` (or by using the object as a context
    manager). ``run`` executes one command; tests may replace it.
    """

    def __init__(self, cwd: str = ".", run: Runner = run_command) -> None:
        self._cwd = cwd
        self._run = run
        self._scratch: str | None = None
        self._repos: dict[str, str] = {}

    def __enter__(self) -> RealGit:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Remove the scratch repositories."""
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None
            self._repos = {}

    def _git(self, args: Sequence[str], cwd: str | None = None) -> Completed:
        return self._run(["git", *args], cwd or self._cwd)

    def head(self, repo: str, ref: str) -> str | None:
        """Commit at the head of branch ``ref`` of ``repo``; None when it does not exist."""
        full = branch_ref(ref)
        args = ["ls-remote", repo_url(repo), full]
        return parse_ls_remote(_checked(self._git(args), args), full)

    def _scratch_repo(self, repo: str) -> str:
        if repo not in self._repos:
            if self._scratch is None:
                self._scratch = tempfile.mkdtemp(prefix="snapdep-git-")
            d = os.path.join(self._scratch, repo.replace("/", "__"))
            os.makedirs(d)
            for args in (["init", "-q"], ["remote", "add", "origin", repo_url(repo)]):
                _checked(self._git(args, d), args)
            self._repos[repo] = d
        return self._repos[repo]

    def read_file(self, repo: str, commit: str, path: str) -> str | None:
        """Content of ``path`` at ``commit`` of ``repo``; None when the file is absent.

        Fetches only that commit, without blobs, then lists and shows the one
        file; git fetches its blob on demand.
        """
        d = self._scratch_repo(repo)
        fetch = ["fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", commit]
        _checked(self._git(fetch, d), fetch)
        ls = ["ls-tree", "--name-only", "FETCH_HEAD", "--", path]
        if not _checked(self._git(ls, d), ls).strip():
            return None
        show = ["show", f"FETCH_HEAD:{path}"]
        return _checked(self._git(show, d), show)

    def head_commit(self) -> str:
        """The commit checked out in ``cwd`` (``git rev-parse HEAD``)."""
        args = ["rev-parse", "HEAD"]
        return _checked(self._git(args), args).strip()

    def origin_repo(self) -> str | None:
        """``owner/name`` of the checkout's ``origin`` on GitHub; None when unknown.

        Not part of ``Git``: used by the command line to name the root's repository
        when ``BUILDKITE_REPO`` is not set.
        """
        c = self._git(["remote", "get-url", "origin"])
        return github_repo(c.stdout) if c.returncode == 0 else None

    def _merge_base_time(self, repo: str, line: str) -> int | None:
        fetch = ["fetch", "-q", "--no-tags", repo_url(repo), branch_ref(line)]
        if self._git(fetch).returncode != 0:
            return None
        base = self._git(["merge-base", "HEAD", "FETCH_HEAD"])
        if base.returncode != 0 or not base.stdout.strip():
            return None
        t = self._git(["show", "-s", "--format=%ct", base.stdout.strip()])
        return int(t.stdout.strip()) if t.returncode == 0 and t.stdout.strip() else None

    def release_line(self, repo: str, branch: str, lines: Sequence[str]) -> str:
        """The release line of ``branch`` in the checkout of ``repo``.

        Fetches each line from ``repo`` (into FETCH_HEAD only, no local ref is
        written) and compares merge-bases with HEAD; see ``choose_release_line``.
        A line that cannot be fetched or has no merge-base (for example in a
        shallow checkout) is skipped; ``master`` when none is left.
        """
        if branch in lines:
            return branch
        times = {line: self._merge_base_time(repo, line) for line in lines}
        return choose_release_line(branch, lines, times)
