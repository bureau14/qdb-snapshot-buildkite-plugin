from __future__ import annotations

from snapdep.canonical import canonical_json, is_opaque_key, opaque_key, version_key

C1 = "1" * 40
C2 = "2" * 40


def test_canonical_json_sorts_keys_and_has_no_whitespace():
    assert canonical_json({"b": 1, "a": [1, {"d": None, "c": True}]}) == (
        b'{"a":[1,{"c":true,"d":null}],"b":1}'
    )


def test_canonical_json_is_utf8_not_escaped():
    assert canonical_json({"k": chr(0xE9)}) == b'{"k":"\xc3\xa9"}'


def test_version_key_without_dependencies():
    # sha256('{"commit":"111...1","deps":[],"project":"quasardb-build"}')
    assert version_key("quasardb-build", C1, {}) == "v1-fdf8f768f22318b1f46a33c384f76a03"


def test_version_key_sorts_dependencies_by_project():
    deps = {
        "quasardb-build": "v1-fdf8f768f22318b1f46a33c384f76a03",
        "qdb-duck": "v1-" + "a" * 32,
    }
    assert version_key("qdb-api-rest", C2, deps) == "v1-a2075e366014c5cae592fcd00b345234"


def test_version_key_changes_with_commit_and_with_any_dependency_key():
    base = version_key("p", C1, {"d": "v1-x"})
    assert version_key("p", C2, {"d": "v1-x"}) != base
    assert version_key("p", C1, {"d": "v1-y"}) != base
    assert version_key("q", C1, {"d": "v1-x"}) != base


def test_opaque_key_fixed_value_and_order_independence():
    pointers = {
        "windows-haswell-release": "01a1e86b-0000-4682-853f-2996bdca34a5",
        "linux-haswell-release": "01a1e86b-b56b-4682-853f-2996bdca34a5",
    }
    k = opaque_key(pointers)
    assert k == "opaque-ca25a63cae579268497c5c2ed9638674"
    assert opaque_key(dict(reversed(list(pointers.items())))) == k
    assert is_opaque_key(k)
    assert not is_opaque_key(version_key("p", C1, {}))


def test_opaque_key_of_no_pointers():
    assert opaque_key({}) == "opaque-4f53cda18c2baa0c0354bb5f9a3ecbe5"


def test_opaque_key_moves_with_any_pointer():
    a = opaque_key({"x": "b1", "y": "b2"})
    assert opaque_key({"x": "b1", "y": "b3"}) != a
