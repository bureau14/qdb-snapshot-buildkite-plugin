"""In-memory ``Git`` and ``Store`` for tests. No network, no subprocess.

``MemoryStore`` has the conditional semantics of the real store: a put with
``if_none_match`` fails when the key exists, a put with ``if_match`` fails
unless the current ETag is the one given. Every write gets a fresh ETag.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence

from .git import choose_release_line
from .store import StoredObject, StoreError, check_conditions


class FakeGit:
    """``Git`` from dictionaries.

    ``heads``: ``(repo, branch) -> commit``. ``files``: ``(repo, commit, path) ->
    text``; a commit that appears in neither ``heads`` nor ``files`` is unknown
    and ``read_file`` raises, as a fetch of an unknown commit does.
    ``checkout``: the commit of ``head_commit()``. ``merge_base_times``:
    ``line -> committer time`` of its merge-base with the checkout.

    ``calls`` records every ``head`` and ``release_line`` call, for tests of
    memoisation.
    """

    def __init__(
        self,
        heads: Mapping[tuple[str, str], str],
        files: Mapping[tuple[str, str, str], str] | None = None,
        checkout: str = "0" * 40,
        merge_base_times: Mapping[str, int] | None = None,
    ) -> None:
        self.heads = dict(heads)
        self.files = dict(files or {})
        self.checkout = checkout
        self.merge_base_times = dict(merge_base_times or {})
        self.calls: list[tuple[str, ...]] = []

    def _known_commits(self, repo: str) -> set[str]:
        from_heads = {c for (r, _), c in self.heads.items() if r == repo}
        from_files = {c for (r, c, _) in self.files if r == repo}
        return from_heads | from_files

    def head(self, repo: str, ref: str) -> str | None:
        self.calls.append(("head", repo, ref))
        return self.heads.get((repo, ref))

    def read_file(self, repo: str, commit: str, path: str) -> str | None:
        if commit not in self._known_commits(repo):
            raise RuntimeError(f"fake git: {repo} has no commit {commit}")
        return self.files.get((repo, commit, path))

    def head_commit(self) -> str:
        return self.checkout

    def release_line(self, repo: str, branch: str, lines: Sequence[str]) -> str:
        self.calls.append(("release_line", repo, branch))
        return choose_release_line(branch, lines, self.merge_base_times)


def _etag(body: bytes, generation: int) -> str:
    return '"' + hashlib.md5(body + str(generation).encode()).hexdigest() + '"'


class MemoryStore:
    """``Store`` in a dictionary, with the real store's conditional semantics.

    ``unreadable``: keys whose ``get`` raises ``StoreError``, to model a lookup
    that cannot be answered.
    """

    def __init__(
        self, objects: Mapping[str, bytes] | None = None, unreadable: Iterable[str] = ()
    ) -> None:
        self._generation = 0
        self.objects: dict[str, StoredObject] = {}
        self.unreadable = set(unreadable)
        for k, v in (objects or {}).items():
            self.put(k, v)

    def get(self, key: str) -> StoredObject | None:
        if key in self.unreadable:
            raise StoreError(f"get {key}: injected failure")
        return self.objects.get(key)

    def put(
        self, key: str, body: bytes, *, if_none_match: bool = False, if_match: str | None = None
    ) -> bool:
        check_conditions(if_none_match, if_match)
        current = self.objects.get(key)
        if if_none_match and current is not None:
            return False
        if if_match is not None and (current is None or current.etag != if_match):
            return False
        self._generation += 1
        self.objects[key] = StoredObject(body=bytes(body), etag=_etag(body, self._generation))
        return True

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def list_children(self, prefix: str) -> list[str]:
        if not prefix.endswith("/"):
            raise ValueError(f"list_children: prefix must end in '/': {prefix!r}")
        rest = (k[len(prefix) :] for k in self.objects if k.startswith(prefix))
        return sorted({r.split("/", 1)[0] for r in rest if "/" in r})
