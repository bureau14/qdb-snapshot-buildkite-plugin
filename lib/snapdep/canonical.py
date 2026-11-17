"""Canonical JSON and the version keys (specification section 3).

A version key names what a build of a pipeline is made of: its own commit and
the keys of its direct dependencies. Two builds with the same key are
interchangeable, so the first passed build of a key is reused by everyone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

KEY_PREFIX = "v1-"
OPAQUE_PREFIX = "opaque-"
DIGEST_LENGTH = 32


def canonical_json(v: Any) -> bytes:
    """Serialise ``v`` as canonical JSON: sorted keys, no whitespace, UTF-8.

    Non-ASCII characters are written as themselves, not escaped, so the bytes
    are the UTF-8 encoding of the text.
    """
    text = json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def digest(v: Any) -> str:
    """Return the first 32 hex digits of the sha256 of the canonical JSON of ``v``."""
    return hashlib.sha256(canonical_json(v)).hexdigest()[:DIGEST_LENGTH]


def version_key(project: str, commit: str, deps: Mapping[str, str]) -> str:
    """Return the version key of ``project`` at ``commit``.

    ``deps`` maps each direct dependency's project to its key. The dependencies
    enter the key as a list of ``[project, key]`` pairs sorted by project.
    """
    pairs = [[d, deps[d]] for d in sorted(deps)]
    return KEY_PREFIX + digest({"project": project, "commit": commit, "deps": pairs})


def opaque_key(pointers: Mapping[str, str]) -> str:
    """Return the key of a dependency that is not snapshot-managed.

    ``pointers`` maps each variant of the dependency to the build id held by its
    ``LATEST_SUCCESSFUL`` pointer. The key changes whenever any pointer moves.
    """
    pairs = [[variant, pointers[variant]] for variant in sorted(pointers)]
    return OPAQUE_PREFIX + digest(pairs)


def is_opaque_key(k: str) -> bool:
    """True when ``k`` was made by ``opaque_key``."""
    return k.startswith(OPAQUE_PREFIX)
