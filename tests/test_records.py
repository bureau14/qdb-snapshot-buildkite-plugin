from __future__ import annotations

import json

import pytest

from snapdep.fakes import MemoryStore
from snapdep.records import (
    DISABLED_PATH,
    BuildRef,
    Cause,
    FailedStep,
    Node,
    Plan,
    RecordError,
    RootInfo,
    create_or_load_plan,
    failure_from_dict,
    failure_record,
    failure_to_dict,
    find_version,
    holder_from_dict,
    holder_path,
    holder_record,
    holder_to_dict,
    is_disabled,
    iso_utc,
    list_failures,
    load_plan,
    plan_from_dict,
    plan_path,
    plan_to_dict,
    record_version,
    version_from_dict,
    version_path,
    version_record,
    version_to_dict,
    write_failure,
    write_holder,
)
from snapdep.store import StoreError

T0 = 1_763_380_800.25  # 2025-11-17T12:00:00.250Z


def build(uuid: str = "u-1", number: int = 7) -> BuildRef:
    return BuildRef(uuid, number, f"https://ci/b/{number}", "qdb-api-rest")


def node(label: str = "master") -> Node:
    return Node("bureau14/qdb-api-rest", "master", "c" * 40, "v1-k", label, False, ("qdb-duck",))


def plan(pid: str = "root-uuid", context: str = "master") -> Plan:
    return Plan(
        id=pid,
        root=RootInfo("qdb-api-rest", 512, "https://ci/b/512", context),
        context=context,
        rebuild="none",
        created_at=iso_utc(T0),
        nodes={"qdb-api-rest": node()},
    )


def test_iso_utc():
    assert iso_utc(T0) == "2025-11-17T12:00:00.250Z"


def test_paths():
    assert plan_path("u") == "_snapdep/v1/plans/u.json"
    assert version_path("p", "v1-k") == "_snapdep/v1/versions/p/v1-k.json"
    assert holder_path("p", "v1-k", "b") == "_snapdep/v1/holders/p/v1-k/b.json"
    assert DISABLED_PATH == "_snapdep/v1/disabled"


def test_round_trips():
    p = plan()
    assert plan_from_dict(json.loads(json.dumps(plan_to_dict(p)))) == p
    v = version_record("qdb-api-rest", node("sc-1"), {"qdb-duck": "v1-d"}, build(), ["a"], T0)
    assert v.store_ref == "refs/heads/sc-1"
    assert version_from_dict(version_to_dict(v)) == v
    f = failure_record(
        "qdb-api-rest",
        "v1-k",
        "root-uuid",
        build(),
        [FailedStep("t", "hard_failed")],
        [Cause("d", "k")],
        T0,
    )
    assert f.recorded_at == 1_763_380_800.25 and f.recorded_at_iso == iso_utc(T0)
    assert failure_from_dict(failure_to_dict(f)) == f
    h = holder_record("qdb-api-rest", "v1-k", "root-uuid", build(), T0)
    assert holder_from_dict(holder_to_dict(h)) == h


def test_failure_record_names_its_plan():
    f = failure_record("p", "v1-k", "root-uuid", build(), [], [Cause("d", "k")], T0)
    d = json.loads(json.dumps(failure_to_dict(f)))
    assert d["plan"] == "root-uuid"
    assert failure_from_dict(d).plan == "root-uuid"


def test_failure_record_without_a_plan_stays_readable():
    # A record written before the field existed.
    old = failure_to_dict(failure_record("p", "v1-k", "root-uuid", build(), [], [], T0))
    del old["plan"]
    f = failure_from_dict(json.loads(json.dumps(old)))
    assert f.plan is None and f.build == build()
    assert failure_to_dict(f) == old, "rewritten unchanged: no plan field is invented"


def test_plan_record_shape():
    d = plan_to_dict(plan())
    assert d["schema"] == 1
    assert set(d) == {"schema", "id", "root", "context", "rebuild", "created_at", "nodes"}
    assert set(d["nodes"]["qdb-api-rest"]) == {
        "repo",
        "ref",
        "commit",
        "key",
        "label",
        "opaque",
        "deps",
    }


def test_readers_reject_incomplete_records():
    d = version_to_dict(version_record("p", node(), {}, build(), [], T0))
    del d["commit"]
    with pytest.raises(RecordError, match="missing field 'commit'"):
        version_from_dict(d)
    with pytest.raises(RecordError, match="schema"):
        plan_from_dict({"schema": 2})


def test_plan_is_created_once_and_a_retry_loads_it():
    s = MemoryStore()
    first = create_or_load_plan(s, plan(context="master"))
    again = create_or_load_plan(s, plan(context="other"))
    assert again == first
    assert load_plan(s, "root-uuid").context == "master"
    with pytest.raises(RecordError, match="does not exist"):
        load_plan(s, "nope")


def test_version_create_once_and_forced():
    s = MemoryStore()
    assert find_version(s, "qdb-api-rest", "v1-k") is None
    mine = version_record("qdb-api-rest", node(), {}, build("u-1"), [], T0)
    theirs = version_record("qdb-api-rest", node(), {}, build("u-2", 8), [], T0 + 1)
    assert record_version(s, mine) == mine
    assert record_version(s, theirs).build.uuid == "u-1", "the first build of a key stands"
    assert record_version(s, theirs, force=True) == theirs
    assert find_version(s, "qdb-api-rest", "v1-k").build.uuid == "u-2"


def test_failed_lookup_is_not_no_record():
    s = MemoryStore(unreadable=[version_path("p", "v1-k")])
    with pytest.raises(StoreError):
        find_version(s, "p", "v1-k")


def test_failures_listed_oldest_first():
    s = MemoryStore()
    late = failure_record("p", "v1-k", "plan", build("u-2"), [], [], T0 + 10)
    early = failure_record("p", "v1-k", "plan", build("u-1"), [FailedStep("x", "failed")], [], T0)
    other = failure_record("p", "v1-other", "plan", build("u-3"), [], [], T0)
    for f in (late, early, other):
        write_failure(s, f)
    assert list_failures(s, "p", "v1-k") == [early, late]
    assert list_failures(s, "p", "v1-none") == []


def test_a_rewritten_failure_keeps_its_first_time():
    s = MemoryStore()
    first_try = failure_record("p", "v1-k", "plan", build(), [FailedStep("x", "failed")], [], T0)
    write_failure(s, first_try)
    again = failure_record("p", "v1-k", "plan", build(), [FailedStep("y", "failed")], [], T0 + 300)
    write_failure(s, again)
    (stored,) = list_failures(s, "p", "v1-k")
    assert stored.failed_steps == (FailedStep("y", "failed"),)
    assert stored.recorded_at == T0
    first = failure_record("p", "v1-k", "plan", build(), [], [], T0)
    assert stored.recorded_at_iso == first.recorded_at_iso


def test_holder_and_disabled():
    s = MemoryStore()
    write_holder(s, holder_record("p", "v1-k", "plan", build(), T0))
    assert s.list_keys("_snapdep/v1/holders/p/v1-k/") == ["_snapdep/v1/holders/p/v1-k/u-1.json"]
    assert not is_disabled(s)
    s.put(DISABLED_PATH, b"")
    assert is_disabled(s)


def test_garbage_record_is_an_error():
    s = MemoryStore({version_path("p", "v1-k"): b"not json"})
    with pytest.raises(RecordError, match="not JSON"):
        find_version(s, "p", "v1-k")
