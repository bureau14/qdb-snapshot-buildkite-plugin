from __future__ import annotations

from plans import DUCK, PYTHON, QDB, REST, build, failed, key, objects, plan, sha, version

from snapdep.annotation import (
    dependency_failure_text,
    failed_steps_text,
    plan_annotation,
    preformatted,
    reuse_annotation,
    shared_failure_text,
    trace_failure,
    trace_missing,
)
from snapdep.fakes import MemoryStore
from snapdep.records import Cause, write_failure


def short(p: str) -> str:
    return sha(p)[:10]


def chain_store():
    """rest 88 failed because duck had no version; duck 31 failed because qdb 4250 failed."""
    s = MemoryStore(objects(plan()))
    qdb = failed(QDB, key(QDB), build(QDB, 4250), {"build-windows": "hard_failed"}, at=1.0)
    duck = failed(DUCK, key(DUCK), build(DUCK, 31), {}, [Cause(QDB, key(QDB))], at=2.0)
    rest = failed(REST, key(REST), build(REST, 88), {}, [Cause(DUCK, key(DUCK))], at=3.0)
    for f in (qdb, duck, rest):
        write_failure(s, f)
    return s


def test_dependency_failure_follows_caused_by_down_to_the_failed_steps():
    s = chain_store()
    missing = [trace_missing(s, plan(), Cause(REST, key(REST)))]
    assert dependency_failure_text(PYTHON, 512, missing) == "\n".join(
        [
            "Snapshot dependency failed",
            "",
            "qdb-api-python build 512 was not built, because:",
            f"  qdb-api-rest @ {short(REST)} ({key(REST)}) has no version.",
            "    FAILED in build 88: https://ci/qdb-api-rest/88",
            f"      caused by qdb-duck @ {short(DUCK)} ({key(DUCK)})",
            "        FAILED in build 31: https://ci/qdb-duck/31",
            f"          caused by quasardb-build @ {short(QDB)} ({key(QDB)})",
            "            FAILED in build 4250: https://ci/quasardb-build/4250",
            "              step build-windows: hard_failed",
            "",
            "To retry: retry the failed job in quasardb-build 4250. When it passes,",
            "rebuild this build. Everything that passed is reused.",
        ]
    )


def test_dependency_failure_without_a_failure_record():
    s = MemoryStore(objects(plan()))
    missing = [trace_missing(s, plan(), Cause(QDB, key(QDB)))]
    text = dependency_failure_text(REST, 77, missing)
    assert "no failure on record: its build was cancelled, was lost or did not run" in text
    assert text.endswith("To retry: rebuild this build. Everything that passed is reused.")
    assert "retry the step" not in text, "a retried trigger does not run generate again"


def test_latest_failure_of_a_key_is_shown():
    s = MemoryStore(objects(plan()))
    write_failure(s, failed(QDB, key(QDB), build(QDB, 1), {"a": "hard_failed"}, at=1.0))
    write_failure(s, failed(QDB, key(QDB), build(QDB, 2), {"b": "soft_failed"}, at=2.0))
    m = trace_missing(s, plan(), Cause(QDB, key(QDB)))
    assert m.failure.build.number == 2


def test_trace_stops_at_a_cycle_of_records():
    s = MemoryStore(objects(plan()))
    write_failure(s, failed(QDB, key(QDB), build(QDB, 1), {}, [Cause(DUCK, key(DUCK))]))
    write_failure(s, failed(DUCK, key(DUCK), build(DUCK, 1), {}, [Cause(QDB, key(QDB))]))
    m = trace_missing(s, plan(), Cause(QDB, key(QDB)))
    assert m.causes[0].project == DUCK and m.causes[0].causes == ()


def test_shared_failure_text():
    s = chain_store()
    f = failed(REST, key(REST), build(REST, 88), {"lint": "hard_failed"}, at=9.0)
    text = shared_failure_text(trace_failure(s, plan(), f))
    assert text.splitlines()[:5] == [
        "Snapshot build failed in another build",
        "",
        f"qdb-api-rest @ {short(REST)} ({key(REST)}) failed in build 88 while this build "
        "waited for it:",
        "  FAILED in build 88: https://ci/qdb-api-rest/88",
        "    step lint: hard_failed",
    ]
    assert "retry the failed job in qdb-api-rest 88" in text


def test_failed_steps_text():
    f = failed(REST, key(REST), build(REST, 88), {"lint": "hard_failed", "e2e": ""})
    assert failed_steps_text(REST, key(REST), f).splitlines()[2:] == [
        f"qdb-api-rest build 88 ({key(REST)}) did not pass these steps:",
        "  step lint: hard_failed",
        "  step e2e: did not run",
    ]


def test_plan_annotation_table():
    p = plan()
    text = plan_annotation(p, REST, {REST: "build in this build", QDB: "reuse"})
    lines = text.splitlines()
    assert lines[0].startswith("**Snapshot plan** `01aaaaaa-root-build`: root qdb-api-python")
    assert lines[3] == "| --- | --- | --- | --- | --- |"
    assert [line.split(" | ")[0] for line in lines[4:]] == [f"| {QDB}", f"| {DUCK}", f"| {REST}"]
    assert lines[4].endswith("| reuse |")


def test_reuse_annotation_links_the_build():
    v = version(plan(), REST, 70)
    assert "[build 70](https://ci/qdb-api-rest/70)" in reuse_annotation(REST, v)


def test_preformatted():
    assert preformatted("a\n  b") == "```\na\n  b\n```"
